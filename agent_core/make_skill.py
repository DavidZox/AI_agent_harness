"""MakeSkillMixin：/make_skill——把使用者引導 Agent 做對的一段對話整理成「流程技能」的規格（草擬、驗證、預覽、註冊）。

做一個技能光知道用了哪些技能不夠：很多步驟跟使用者當時的對話有關——要先跟使用者確認的地方、要看某個回傳裡的
某個欄位再決定怎麼走、使用者中途補充或糾正的做法。所以草擬 session 拿到的是整段對話紀錄（transcript：使用者的話、
AI 的想法／回覆／action、當時進上下文的工具回傳）加上操作軌跡，由它推論出下次做同一件事的步驟。

不產生任何腳本：規格的步驟只有四種——執行（呼叫既有技能的腳本）、確認（問使用者）、檢查（看回傳決定怎麼走）、
告知（整理結果給使用者）。新的腳本仍由使用者另外寫好，再產生對應的規格。程式負責：挑範圍、驗證（引用的技能與腳本
都要真的存在、佔位符都要宣告）、用範本排版、寫入索引；使用者在預覽後核准。規格的 frontmatter 是 type: Procedure，
載入時 run_tool 會告訴模型「這個技能沒有自己的腳本，照步驟一步一步做」。
"""
import json
import os
import re
import time
from .config import (
    DEFAULT_SKILL_CATEGORY,
    MAKE_SKILL_MAX_PREDICT,
    MAKE_SKILL_TRANSCRIPT_TOKENS,
    NON_READONLY_SKILLS,
    NUM_CTX,
    PROCEDURE_DOC_TYPE,
    SKILL_NAME_RE,
)
from .schemas import MAKE_SKILL_SCHEMA

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SCRIPT_RE = re.compile(r"^(?:scripts/)?([A-Za-z0-9_]+\.py)$")
PSEUDO_SCRIPTS = {"result_recall_cmd.py": "result_recall"}   # run_tool 攔截的偽技能：沒有實體檔案，但可以寫進步驟
KIND_HINTS = {"確認": "在 reply 問使用者、action 填 null，等使用者回答再做下一步",
              "檢查": "依前面步驟的回傳判斷，不用執行新的腳本",
              "告知": "在 reply 整理給使用者"}


class MakeSkillMixin:

    def _skill_categories(self):
        """SKILLS.md 裡的分類（## 標題），依出現順序。"""
        cats = []
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("## "):
                        cats.append(line[3:].strip())
        except OSError:
            pass
        return cats

    def validate_new_skill_name(self, name):
        """新技能名稱的規則：英數底線、字母開頭、不與現有技能或腳本衝突。合法回傳 None，否則回傳原因。"""
        if not SKILL_NAME_RE.match(name or ""):
            return "技能名稱只能用英文字母、數字與底線，須以字母開頭、2～41 字（例如 check_lidar_scan）。"
        if os.path.exists(os.path.join(self.tools_dir, f"{name}.md")) or \
                os.path.exists(os.path.join(self.base_path, "scripts", f"{name}_cmd.py")):
            return f"技能 {name} 已存在（tools/{name}.md 或 scripts/{name}_cmd.py），請換一個名稱。"
        return None

    # ---------- 交給草擬 session 的材料 ----------
    def _skill_syntax(self, skill):
        """tools/<skill>.md 的「# 語法」一節（草擬模型照它寫 command，不用猜參數順序）；讀不到就空字串。"""
        try:
            with open(os.path.join(self.tools_dir, f"{skill}.md"), "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return ""
        m = re.search(r"^# 語法\n(.*?)(?=^# |\Z)", text, re.S | re.M)
        return m.group(1).strip()[:800] if m else ""

    def _render_transcript(self, entries):
        """對話紀錄 → 純文字（同被壓縮的對話片段存檔的格式：[user]／[assistant]（想法、回覆、action）／[harness …]）。
        超過 MAKE_SKILL_TRANSCRIPT_TOKENS 時先把工具回傳縮成頭尾（使用者與 AI 的話不動），還不夠才從最舊的訊息開始省略。
        回傳 (文字, 省略的訊息數)。"""
        budget = int(MAKE_SKILL_TRANSCRIPT_TOKENS * self.chars_per_token)

        def render(items, clip):
            msgs = []
            for e in items:
                content = e["content"]
                if clip and not self._is_user_turn(e) and e["role"] == "user" and len(content) > clip:
                    head, tail = content[:clip * 6 // 10], content[-(clip * 4 // 10):]
                    content = f"{head}\n（中間省略 {len(content) - len(head) - len(tail)} 字）\n{tail}"
                msgs.append({"role": e["role"], "content": content})
            return self._render_messages_for_archive(msgs)

        items, clip = list(entries), None
        text = render(items, clip)
        for clip in (2000, 1000, 500, 250):
            if len(text) <= budget:
                break
            text = render(items, clip)
        dropped = 0
        while len(text) > budget and len(items) > 1:
            items.pop(0)
            dropped += 1
            text = render(items, clip)
        if dropped:
            text = f"（對話太長，最早的 {dropped} 則訊息沒有列出）\n\n{text}"
        return text, dropped

    def _make_skill_prompts(self, name, transcript_text, entries, steps, plans, previous=None, feedback=None):
        categories = self._skill_categories() or [DEFAULT_SKILL_CATEGORY]
        system_prompt = f"""你是機器人維運 Agent 框架的「技能規格撰寫者」。使用者剛剛在對話裡一步步引導 Agent 把一件事做對，現在要把這段過程整理成一個可以重複使用的「流程技能」（名稱：{name}）。之後在別的對話裡，負責決策的小模型會載入這份規格、照步驟一步一步做——它看不到這段對話，只看得到你寫的規格。

你會拿到：可用的技能清單、這段對話用到的技能的語法、實際執行過的腳本（操作軌跡）、使用者在這段對話說的話、整段對話紀錄（使用者的話、AI 的想法與回覆、當時進上下文的工具回傳）。請從對話推論「下次要做同一件事該怎麼做」，不只是列出用過哪些技能：
0. requirements 先寫：使用者在對話裡提出的每一個要求、條件、偏好與糾正都列一項——said 照抄使用者的原話，rule 寫成這個技能要怎麼遵守，step 填之後在 steps 裡落實它的步驟編號（從 1 起算；只能寫成注意事項的填 0）。只列使用者自己說的，不要把 AI 的做法當成使用者的要求；AI 當時沒做到的要求更要列出來，下次要做對。
1. steps 依序寫，每一步選一種 kind：
   - 執行：呼叫既有技能的腳本。command 寫腳本路徑與參數（例如 scripts/ROS2_topic_echo_cmd.py {{container}} {{topic}} 5），只能用操作軌跡或語法裡出現過的腳本；skill 填它所屬的技能名稱；on_failure 寫這一步失敗時怎麼辦（對話裡遇過的錯誤與修正優先）。
   - 確認：要先問使用者、取得資訊或得到同意才能繼續的地方（對話裡使用者補充過的資訊、做會改變狀態的動作之前、使用者說過「要先問我」的地方）。instruction 寫要問什麼。
   - 檢查：看前面步驟的回傳裡的某個資訊，依條件決定下一步（例如「第 2 步的回傳裡 /scan 沒有資料 → 做第 4 步；有資料 → 跳到第 6 步」）。instruction 寫看哪個資訊、判斷條件、接下來怎麼走。
   - 告知：把結果整理給使用者，instruction 寫要告訴使用者哪些具體資訊。
   instruction 都要寫成具體的做法；確認、檢查、告知步驟的 command、skill、on_failure 給空字串。
2. 只保留完成這件事需要的步驟；走錯又修正的過程寫進 on_failure 或 pitfalls，不要照抄成步驟。requirements 裡的每一項都要落實：要先問使用者的地方要有確認步驟，有條件的地方要有檢查步驟（例如「電量低於 20% 的車不要派任務」→ 派任務之前檢查電量，低於門檻就不派、告知使用者）。
3. 下次會變的值（容器、機器人、站點、topic、路徑、關鍵字）在 command 與 instruction 裡寫成 {{名稱}} 佔位符，並在 inputs 宣告：name（英文 snake_case）、description、source（從哪裡取得，例如「使用者提供」「第 1 步的回傳」）、example（對話裡實際出現的值，照抄）。固定不變的值直接寫。
4. 不可以發明不存在的技能或腳本；需要的功能沒有對應的腳本時，寫成確認或告知步驟（例如請使用者手動處理），並在 pitfalls 註明。
5. purpose：這個技能做什麼、什麼情境用（兩三句）；success_criteria：怎樣算做完（一句）；pitfalls：注意事項（每則一句，沒有就給空陣列）。
6. 最後寫 title（12 字內的中文短標題）、description（SKILLS.md 索引的一行：什麼情境該用這個技能，40 字內）、category（從現有分類選一個：{"、".join(categories)}；都不合適就填「{DEFAULT_SKILL_CATEGORY}」）。
全部使用繁體中文；名稱、路徑、參數照原文。只輸出 JSON。"""

        skills_index = ""
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                skills_index = "\n".join(ln for ln in f.read().splitlines() if ln.startswith(("## ", "- [")))
        except OSError:
            pass
        used = list(dict.fromkeys(r["skill"] for r in steps if r.get("skill")))
        lines = [f"技能名稱：{name}", "", "【可用的技能（SKILLS.md）】", skills_index or "（讀不到）", ""]
        lines.append("【這段對話用到的技能的語法】")
        syntax = [(sk, self._skill_syntax(sk)) for sk in used]
        lines += [f"- {sk}：\n{text}" for sk, text in syntax if text] or ["（這段對話沒有執行過技能）"]
        said = [" ".join(t.split()) for t in (self._user_text_of(e) for e in entries) if t]
        lines += ["", "【使用者在這段對話說的話（依時間，每一句的要求都要反映在 requirements）】"]
        lines += [f"{i}. {t}" for i, t in enumerate(said, 1)] or ["（沒有）"]
        lines += ["", "【操作軌跡：這段對話裡實際執行的腳本，依時間順序】"]
        for r in steps:
            mark = "成功" if r["status"] == "PASS" else "失敗"
            skill = f"（技能 {r['skill']}）" if r.get("skill") else ""
            head = " ".join(str(r.get("output_head") or "").split())[:200]
            lines.append(f"- #{r['id']} {mark}：{r['command']}{skill} → 輸出開頭：{head}")
        if not steps:
            lines.append("（沒有執行過腳本）")
        if plans:
            lines += ["", "【使用者核准的計畫】"] + plans
        lines += ["", "【對話紀錄】", transcript_text]
        if feedback:
            lines += ["", "【使用者對上一版草稿的修改意見，請依意見重擬】", feedback.strip(),
                      "", "【上一版草稿（JSON）】", json.dumps(previous, ensure_ascii=False)]
        lines += ["", "請輸出 JSON。"]
        return system_prompt, "\n".join(lines)

    # ---------- 草擬與驗證 ----------
    def _parse_step_command(self, command):
        """執行步驟的 command → (腳本檔名, 參數字串, 錯誤)。容忍「EXECUTE:」前綴、反引號與不帶 scripts/ 的寫法；
        腳本必須是 scripts/ 裡真的有的 *_cmd.py（或 result_recall 這類偽技能），底線開頭的共用模組不算。"""
        text = str(command or "").strip().strip("`").strip()
        if text.upper().startswith("EXECUTE:"):
            text = text[len("EXECUTE:"):].strip()
        if not text:
            return None, "", "執行步驟沒有寫指令（command）"
        first, _, rest = text.partition(" ")
        m = _SCRIPT_RE.match(first)
        if not m:
            return None, "", f"「{first}」不是腳本路徑（要寫 scripts/<名稱>_cmd.py）"
        script = m.group(1)
        if script in PSEUDO_SCRIPTS:
            return script, rest.strip(), None
        if script.startswith("_") or not script.endswith("_cmd.py") or \
                not os.path.exists(os.path.join(self.base_path, "scripts", script)):
            return None, "", f"腳本 scripts/{script} 不存在（只能用既有技能的腳本）"
        return script, rest.strip(), None

    def _normalize_skill_draft(self, name, data, steps, plans, scope):
        """把模型填的 JSON 對照實際的技能與腳本做驗證與收斂。errors 會擋下註冊（引用不存在的腳本、沒有步驟）；
        warnings 只提醒（技能名稱對不上已改正、佔位符沒宣告已補上、對話裡沒用過的技能…）。"""
        errors, warnings = [], []
        get = data.get if isinstance(data, dict) else (lambda k, d=None: d)
        clean = lambda v, n: " ".join(str(v or "").split())[:n]
        skill_map = self._script_skill_map()

        inputs, seen = [], set()
        for p in (get("inputs") or []):
            if not isinstance(p, dict):
                continue
            pname = re.sub(r"[^a-z0-9_]", "_", clean(p.get("name"), 40).lower()).strip("_")
            if pname and pname[0].isdigit():
                pname = f"p_{pname}"
            if not pname or pname in seen:
                continue
            seen.add(pname)
            inputs.append({"name": pname, "description": clean(p.get("description"), 120) or pname,
                           "source": clean(p.get("source"), 80), "example": clean(p.get("example"), 120)})

        steps_out = []
        for k, s in enumerate(get("steps") or [], 1):
            if not isinstance(s, dict):
                continue
            kind = s.get("kind") if s.get("kind") in ("執行", "確認", "檢查", "告知") else "告知"
            instruction = clean(s.get("instruction"), 300)
            step = {"kind": kind, "instruction": instruction, "skill": "", "command": "", "on_failure": ""}
            if kind == "執行":
                script, args, err = self._parse_step_command(s.get("command"))
                if err:
                    errors.append(f"第 {k} 步（執行）：{err}")
                    step["command"] = clean(s.get("command"), 300)
                else:
                    step["command"] = f"scripts/{script}" + (f" {args}" if args else "")
                    mapped = PSEUDO_SCRIPTS.get(script) or skill_map.get(script)
                    claimed = clean(s.get("skill"), 40)
                    if mapped and claimed and claimed != mapped:
                        warnings.append(f"第 {k} 步：scripts/{script} 屬於技能 {mapped}（模型寫成 {claimed}），已改正")
                    step["skill"] = mapped or claimed
                step["on_failure"] = clean(s.get("on_failure"), 200)
            elif clean(s.get("command"), 300):
                warnings.append(f"第 {k} 步（{kind}）不該有指令，已忽略：{clean(s.get('command'), 80)}")
            if not instruction and kind != "執行":
                warnings.append(f"第 {k} 步（{kind}）沒有寫要做什麼")
            steps_out.append(step)
        if not steps_out:
            errors.append("草稿沒有任何步驟")
        elif not any(s["kind"] == "執行" for s in steps_out):
            warnings.append("沒有任何執行步驟：這份規格只有確認／檢查／告知，請確認這是想要的")

        # 佔位符：指令與做法裡用到的 {名稱} 都要在 inputs 宣告；沒宣告的補上，宣告了沒用到的提醒
        declared = {p["name"] for p in inputs}
        used = set()
        for s in steps_out:
            used.update(_PLACEHOLDER_RE.findall(s["command"] + " " + s["instruction"]))
        for pname in sorted(used - declared):
            inputs.append({"name": pname, "description": pname, "source": "（草擬模型沒有說明）", "example": ""})
            warnings.append(f"佔位符 {{{pname}}} 沒有在「需要的資訊」裡宣告，已補上（請補說明與範例）")
        for pname in sorted(declared - used):
            warnings.append(f"「需要的資訊」裡的 {pname} 沒有被任何步驟用到")

        categories = self._skill_categories()
        category = clean(get("category"), 40)
        if category not in categories:
            if category and category != DEFAULT_SKILL_CATEGORY:
                warnings.append(f"分類「{category}」不在 SKILLS.md 裡，改放「{DEFAULT_SKILL_CATEGORY}」")
            category = DEFAULT_SKILL_CATEGORY

        skills_used = list(dict.fromkeys(s["skill"] for s in steps_out if s["kind"] == "執行" and s["skill"]))
        executed = {r["skill"] for r in steps if r.get("skill")}
        untried = [sk for sk in skills_used if sk not in executed and sk not in PSEUDO_SCRIPTS.values()]
        if untried:
            warnings.append(f"技能 {'、'.join(untried)} 在這段對話裡沒有實際執行過，請確認步驟寫得對")
        requirements = []
        for r in (get("requirements") or []):
            if not isinstance(r, dict) or not clean(r.get("rule"), 150):
                continue
            step_no = r.get("step") if isinstance(r.get("step"), int) else 0
            if step_no < 0 or step_no > len(steps_out):
                warnings.append(f"使用者的要求「{clean(r.get('rule'), 40)}」指到不存在的第 {step_no} 步，改列為注意事項")
                step_no = 0
            requirements.append({"said": clean(r.get("said"), 150), "rule": clean(r.get("rule"), 150), "step": step_no})
        pitfalls = list(dict.fromkeys(t for t in (clean(x, 150) for x in (get("pitfalls") or [])) if t))[:8]
        description = clean(get("description"), 100) or "由 make_skill 依對話整理的流程技能"
        return {
            "name": name, "title": clean(get("title"), 30) or name, "description": description,
            "category": category, "purpose": clean(get("purpose"), 400) or description,
            "requirements": requirements, "inputs": inputs, "steps": steps_out,
            "success_criteria": clean(get("success_criteria"), 200),
            "pitfalls": pitfalls, "errors": errors, "warnings": warnings,
            "skills_used": skills_used, "non_readonly": [sk for sk in skills_used if sk in NON_READONLY_SKILLS],
            "created": time.strftime("%Y-%m-%d %H:%M"), "model": self.skill_model, "revision": 0,
            "source": {"session": self.session_id, "step_ids": [r["id"] for r in steps], "plans": plans, **scope},
        }

    def draft_skill_from_conversation(self, name, entries, steps, plans, previous=None, feedback=None):
        """呼叫草擬模型（獨立一次性 session，不碰 self.messages）產生流程技能草稿 dict。失敗拋 ValueError。"""
        transcript_text, dropped = self._render_transcript(entries)
        system_prompt, user_prompt = self._make_skill_prompts(name, transcript_text, entries, steps, plans, previous, feedback)
        res = self._timed_chat(
            "skill_draft",
            model=self.skill_model,
            messages=[{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
            format=MAKE_SKILL_SCHEMA,
            options={'temperature': 0.2, 'num_ctx': NUM_CTX, 'num_predict': MAKE_SKILL_MAX_PREDICT},
            think=False,
        )
        raw = res['message']['content'].strip()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError(f"模型沒有回傳合法的 JSON 草稿（開頭：{raw[:120]!r}），請再試一次或換 AGENT_SKILL_MODEL。")
        scope = {"messages": len(entries), "user_turns": sum(1 for e in entries if self._is_user_turn(e)),
                 "dropped": dropped}
        draft = self._normalize_skill_draft(name, data, steps, plans, scope)
        draft["raw"] = data
        draft["_scope"] = (entries, steps, plans)  # 修改意見重擬時用同一段對話
        return draft

    # ---------- 範本渲染：規格文件 / 索引行 / 預覽 ----------
    def render_skill_doc(self, draft):
        """tools/<name>.md：frontmatter（type: Procedure）＋用途／需要的資訊／步驟／成功判準／異常與注意事項。"""
        d = draft
        lines = [
            "---", f"type: {PROCEDURE_DOC_TYPE}", f"title: {d['title']}", f"description: {d['description']}",
            "version: 0.1.0", f"dependencies: {json.dumps(d['skills_used'], ensure_ascii=False)}",
            f"source: make_skill {d['created']}（依對話與工具回傳整理的流程技能；沒有自己的腳本，步驟呼叫既有技能；可直接編輯）",
            "---", "", "# 用途", d["purpose"], "",
        ]
        if d.get("requirements"):
            lines.append("# 使用者的要求（必須遵守）")
            for r in d["requirements"]:
                where = f"（第 {r['step']} 步）" if r["step"] else ""
                lines.append(f"* {r['rule']}{where}" + (f"——使用者說：「{r['said']}」" if r["said"] else ""))
            lines.append("")
        lines.append("# 需要的資訊")
        for p in d["inputs"]:
            extra = "；".join(x for x in (f"來源：{p['source']}" if p["source"] else "",
                                          f"例：`{p['example']}`" if p["example"] else "") if x)
            lines.append(f"* `{{{p['name']}}}`：{p['description']}" + (f"（{extra}）" if extra else ""))
        if not d["inputs"]:
            lines.append("不需要額外的資訊。")
        else:
            lines.append("開始前先從使用者的話或前面的對話取得；拿不到的先問使用者，不要用猜的。")
        lines += ["", "# 步驟", "這個技能沒有自己的腳本：從第 1 步開始照順序做，一輪只做一步。"]
        for i, s in enumerate(d["steps"], 1):
            if s["kind"] == "執行":
                lines.append(f"{i}. 【執行】{s['instruction']}".rstrip())
                lines.append(f"   `EXECUTE: {s['command']}`" + (f"（技能 {s['skill']}）" if s["skill"] else ""))
                if s["on_failure"]:
                    lines.append(f"   失敗時：{s['on_failure']}")
            else:
                lines.append(f"{i}. 【{s['kind']}】{s['instruction']}（{KIND_HINTS[s['kind']]}）")
        lines += ["", "# 成功判準", d["success_criteria"] or "所有步驟完成，並已把結果告知使用者。", "", "# 異常與注意事項"]
        lines += [f"* {p}" for p in d["pitfalls"]]
        if d["non_readonly"]:
            lines.append(f"* 會改變系統狀態的步驟（{'、'.join(d['non_readonly'])}）執行前，系統照常會請使用者確認。")
        if d["skills_used"]:
            lines.append(f"* 各步驟的參數規則與其他異常見底層技能的規格：{'、'.join(d['skills_used'])}。")
        return "\n".join(lines) + "\n"

    def skill_index_line(self, draft):
        return f"- [{draft['name']}](tools/{draft['name']}.md) — {draft['description']}"

    def skill_draft_preview(self, draft):
        """給使用者看的預覽：讀了多少對話、需要修正的問題、提醒，最後附完整規格文件。"""
        d, src = draft, draft["source"]
        steps = f"操作軌跡 {len(src['step_ids'])} 步" + (f"（#{src['step_ids'][0]}～#{src['step_ids'][-1]}）" if src["step_ids"] else "")
        lines = [f"🧩 技能草稿 {d['name']}：{d['title']}（流程技能，沒有自己的腳本）"
                 + (f"（第 {d['revision']} 次重擬）" if d.get("revision") else ""),
                 f"分類：{d['category']}｜索引描述：{d['description']}",
                 f"讀了：對話 {src['messages']} 則（使用者說了 {src['user_turns']} 句）、{steps}"
                 + (f"；對話太長，最早的 {src['dropped']} 則沒有交給草擬模型" if src.get("dropped") else "")]
        if d["errors"]:
            lines.append("❌ 需要修正才能註冊（請送出修改意見重擬）：")
            lines += [f"  - {e}" for e in d["errors"]]
        notes = list(d["warnings"])
        if d["non_readonly"]:
            notes.append(f"含會改變狀態的步驟（{'、'.join(d['non_readonly'])}）：之後執行時照常會請使用者確認。")
        if notes:
            lines.append("⚠️ 提醒：")
            lines += [f"  - {n}" for n in notes]
        paths = d.get("paths") or {}
        if paths:
            lines.append(f"草稿檔案：{os.path.relpath(os.path.dirname(paths['doc']), self.script_dir)}/（{d['name']}.md、draft.json）")
        lines.append("── 規格文件預覽（tools/%s.md）──" % d["name"])
        lines.append(self.render_skill_doc(d).rstrip())
        return "\n".join(lines)

    # ---------- 草稿檔案：寫入 / 註冊 / 丟棄 ----------
    def write_skill_draft(self, draft):
        """寫到 skills_system/drafts/<name>/：<name>.md 與 draft.json（供稽核與修改意見重擬）。"""
        d = os.path.join(self.drafts_dir, draft["name"])
        os.makedirs(d, exist_ok=True)
        paths = {"doc": os.path.join(d, f"{draft['name']}.md"), "json": os.path.join(d, "draft.json")}
        with open(paths["doc"], "w", encoding="utf-8") as f:
            f.write(self.render_skill_doc(draft))
        draft["paths"] = paths
        with open(paths["json"], "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in draft.items() if k != "_scope"}, f, ensure_ascii=False, indent=2)
        return paths

    def _insert_skill_index_line(self, category, line):
        with open(self.index_file, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        header = f"## {category}"
        if header in lines:
            start = lines.index(header)
            end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
            insert_at = end
            while insert_at > start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            lines.insert(insert_at, line)
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [header, line]
        with open(self.index_file, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip("\n") + "\n")

    def register_skill_draft(self, draft):
        """核准：寫進 tools/、寫入 SKILLS.md、刪除草稿目錄、軌跡與對話紀錄記起點。不產生腳本。回傳給使用者的訊息。"""
        if draft["errors"]:
            raise ValueError("草稿還有需要修正的問題：" + "；".join(draft["errors"]))
        err = self.validate_new_skill_name(draft["name"])
        if err:
            raise ValueError(err)
        doc_path = os.path.join(self.tools_dir, f"{draft['name']}.md")
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write(self.render_skill_doc(draft))
        self._insert_skill_index_line(draft["category"], self.skill_index_line(draft))
        # 稽核：草稿 JSON 搬到 logs/，草稿目錄刪除
        try:
            log_dir = os.path.join(self.script_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, f"make_skill_{draft['name']}_{time.strftime('%Y%m%d_%H%M%S')}.json"),
                      "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in draft.items() if k not in ("_scope", "paths")}, f, ensure_ascii=False, indent=2)
        except (OSError, TypeError, ValueError):
            pass
        self.discard_skill_draft(draft)
        self.add_trajectory_boundary("make_skill", name=draft["name"])
        return (
            f"✅ 已註冊流程技能 {draft['name']}（{draft['title']}）：\n"
            f"- 規格：{os.path.relpath(doc_path, self.script_dir)}（{len(draft['steps'])} 步；沒有自己的腳本，"
            f"步驟呼叫 {'、'.join(draft['skills_used']) or '（無）'}）\n"
            f"- 索引：SKILLS.md「{draft['category']}」新增一行\n"
            f"之後 action.command 填 `{draft['name']}` 載入規格，系統會提醒模型照步驟一步一步做；"
            f"下一次呼叫 AI 時 system prompt 的技能索引就會包含它。規格可以直接手動修改。"
        )

    def discard_skill_draft(self, draft):
        d = os.path.join(self.drafts_dir, draft["name"])
        for fname in ("draft.json", f"{draft['name']}.md"):
            try:
                os.remove(os.path.join(d, fname))
            except OSError:
                pass
        try:
            os.rmdir(d)
        except OSError:
            pass

    # ---------- 待決定的草稿：CLI 與 Web 共用的狀態機 ----------
    def start_skill_draft(self, name, spec=None):
        """/make_skill <name> [範圍]：挑對話範圍、呼叫模型草擬、寫草稿檔、設為待決定。回傳 (draft, error)。"""
        name = (name or "").strip()
        if self.pending_skill_draft:
            return None, (f"已有技能草稿 {self.pending_skill_draft['name']} 待決定：請先核准（y）、"
                          f"取消（n）或送出修改意見。")
        err = self.validate_new_skill_name(name)
        if err:
            return None, err
        start, end, steps, err = self.transcript_scope(spec)
        if err:
            return None, err
        entries = self.transcript[start:end]
        plans = [m["plan"] for m in self.transcript_starts if m.get("plan") and start <= m["pos"] < end]
        try:
            draft = self.draft_skill_from_conversation(name, entries, steps, plans)
        except ValueError as e:
            return None, str(e)
        except Exception as e:
            return None, f"呼叫模型草擬技能失敗：{e}"
        self.write_skill_draft(draft)
        self.pending_skill_draft = draft
        return draft, None

    def revise_skill_draft(self, feedback):
        """使用者的修改意見：帶著上一版 JSON 與意見重擬，同一段對話。回傳 (draft, error)。"""
        old = self.pending_skill_draft
        if not old:
            return None, "目前沒有待決定的技能草稿。"
        entries, steps, plans = old["_scope"]
        try:
            draft = self.draft_skill_from_conversation(old["name"], entries, steps, plans,
                                                       previous=old.get("raw"), feedback=feedback)
        except ValueError as e:
            return None, str(e)
        except Exception as e:
            return None, f"呼叫模型重擬技能失敗：{e}"
        draft["revision"] = old.get("revision", 0) + 1
        self.write_skill_draft(draft)
        self.pending_skill_draft = draft
        return draft, None

    def approve_skill_draft(self):
        """核准並註冊；草稿還有需要修正的問題時不註冊、保留草稿。回傳 (ok, message)。"""
        draft = self.pending_skill_draft
        if not draft:
            return False, "目前沒有待決定的技能草稿。"
        if draft["errors"]:
            return False, ("❌ 草稿還有需要修正的問題，沒有註冊（草稿保留）：\n"
                           + "\n".join(f"  - {e}" for e in draft["errors"])
                           + "\n請送出修改意見重擬（例如指出要用哪個既有技能），或取消（n）。")
        try:
            msg = self.register_skill_draft(draft)
        except (OSError, ValueError) as e:
            return False, f"⚠️ 註冊技能失敗，草稿保留：{e}"
        self.pending_skill_draft = None
        return True, msg

    def cancel_skill_draft(self):
        draft = self.pending_skill_draft
        if not draft:
            return "目前沒有待決定的技能草稿。"
        self.discard_skill_draft(draft)
        self.pending_skill_draft = None
        return f"🚫 已取消技能草稿 {draft['name']}（草稿檔已刪除，對話紀錄與軌跡保留，可再次 /make_skill）。"
