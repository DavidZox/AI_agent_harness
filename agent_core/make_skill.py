"""MakeSkillMixin：/make_skill——把做對的操作軌跡編譯成組合技能（草擬、驗證、預覽、重播、註冊）。"""
import json
import ollama
import os
import re
import subprocess
import sys
import time
from .config import (
    DEFAULT_SKILL_CATEGORY,
    MAKE_SKILL_MAX_PREDICT,
    NON_READONLY_SKILLS,
    NUM_CTX,
    SKILL_NAME_RE,
    TOOL_EXEC_TIMEOUT,
)
from .schemas import MAKE_SKILL_SCHEMA


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

    def _skill_dependencies(self, skill):
        """tools/<skill>.md frontmatter 的 dependencies 陣列；讀不到就空。"""
        if not skill:
            return []
        try:
            with open(os.path.join(self.tools_dir, f"{skill}.md"), "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("dependencies:"):
                        return [str(d) for d in json.loads(line.split(":", 1)[1].strip() or "[]")]
        except (OSError, ValueError):
            pass
        return []

    def validate_new_skill_name(self, name):
        """新技能名稱的規則：英數底線、字母開頭、不與現有技能或腳本衝突。合法回傳 None，否則回傳原因。"""
        if not SKILL_NAME_RE.match(name or ""):
            return "技能名稱只能用英文字母、數字與底線，須以字母開頭、2～41 字（例如 check_ros2_nodes）。"
        if os.path.exists(os.path.join(self.tools_dir, f"{name}.md")) or \
                os.path.exists(os.path.join(self.base_path, "scripts", f"{name}_cmd.py")):
            return f"技能 {name} 已存在（tools/{name}.md 或 scripts/{name}_cmd.py），請換一個名稱。"
        return None

    def _make_skill_prompts(self, name, groups, trailing, plan_text, tasks, previous=None, feedback=None):
        categories = self._skill_categories() or [DEFAULT_SKILL_CATEGORY]
        system_prompt = f"""你是機器人維運 Agent 框架的技能規格撰寫者。系統記錄了一段使用者引導 Agent「做對」的操作軌跡，
現在要把它編譯成一個可重複使用的新技能（名稱：{name}）。新技能的腳本由系統自動組合（依序執行軌跡中既有技能的腳本），
你不需要寫任何程式，只需要填寫下列 JSON 欄位，全部使用繁體中文：
- title：中文短標題（12 字內）。
- description：SKILLS.md 索引用的一行描述，說明「什麼情境該用這個技能」（40 字內）。
- category：從現有分類中選一個：{"、".join(categories)}；都不合適就填「{DEFAULT_SKILL_CATEGORY}」。
- purpose：兩三句話，說明這個技能從頭到尾做了什麼、何時使用。
- parameters：之後重複使用時會變動的值（容器名稱、路徑、關鍵字、topic／node 名稱等），每個含 name（英文 snake_case）、
  description（中文）、example（軌跡中實際出現的原值，逐字照抄）。固定不變的指令結構不要參數化；沒有會變動的值就給空陣列。
- steps：軌跡中每一個成功步驟都要有一筆，step_id 照抄。include 通常為 true，只有明顯屬於探索、與最終流程無關的步驟才 false。
  purpose 為該步的目的（30 字內）。args 必須與該步原本的參數「數量相同、順序相同」：要參數化的值改寫成 {{參數名稱}} 佔位符，
  其餘逐字照抄；不可新增、刪除或改寫參數。
- success_criteria：從成功步驟的輸出判斷「怎樣算成功」，一句話。
- pitfalls：從失敗嘗試與修正歸納出的注意事項（每則 60 字內，沒有就給空陣列），寫給之後使用這個技能的 Agent 看。
只能使用軌跡中出現過的步驟，不可以憑空新增步驟或腳本。"""

        lines = [f"技能名稱：{name}", ""]
        lines.append("【使用者在引導過程中下的指令】")
        lines += [f"- {t}" for t in tasks] or ["（無記錄）"]
        lines.append("")
        lines.append("【使用者核准的計畫】")
        lines.append(plan_text.strip() if plan_text else "（這段操作沒有經過 Plan 模式）")
        lines.append("")
        lines.append("【操作軌跡：成功步驟，依時間順序】")
        for g in groups:
            r = g["step"]
            skill = f"技能 {r['skill']}" if r.get("skill") else f"腳本 {r['script']}"
            lines.append(f"步驟 {r['id']}（{skill}）：{r['command']}")
            lines.append(f"  參數（JSON）：{json.dumps(r['args'], ensure_ascii=False)}")
            head = " ".join(r["output_head"].split())[:200]
            lines.append(f"  結果：PASS；輸出開頭：{head}")
            if g["failed_before"]:
                lines.append("  這一步之前失敗過的嘗試：")
                for fr in g["failed_before"]:
                    lines.append(f"    - {fr['command']} → {' '.join(fr['output_head'].split())[:160]}")
        if trailing:
            lines.append("")
            lines.append("【最後仍失敗、沒有被修正的嘗試（不會成為步驟，可寫進 pitfalls）】")
            for fr in trailing:
                lines.append(f"- {fr['command']} → {' '.join(fr['output_head'].split())[:160]}")
        if feedback:
            lines.append("")
            lines.append("【使用者對上一版草稿的修改意見，請依意見重擬】")
            lines.append(feedback.strip())
            lines.append("")
            lines.append("【上一版草稿（JSON）】")
            lines.append(json.dumps(previous, ensure_ascii=False))
        lines.append("")
        lines.append("請輸出 JSON。")
        return system_prompt, "\n".join(lines)

    _PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

    @classmethod
    def _fill_placeholders(cls, text, values):
        return cls._PLACEHOLDER_RE.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), text)

    @classmethod
    def _rename_placeholders(cls, text, rename):
        return cls._PLACEHOLDER_RE.sub(lambda m: "{" + rename.get(m.group(1), m.group(1)) + "}", text)

    def _normalize_skill_draft(self, name, data, groups, trailing, plan_text, tasks):
        """把模型填的 JSON 對照實際軌跡做驗證與收斂：參數化必須能還原成記錄到的原值，
        對不上就退回原值並留下提醒；分類不存在就放預設分類；模型漏填的步驟照原值納入。"""
        warnings = []
        get = data.get if isinstance(data, dict) else (lambda k, d=None: d)
        clean = lambda v, n: " ".join(str(v or "").split())[:n]
        title = clean(get("title"), 30) or name
        description = clean(get("description"), 80) or f"由 make_skill 依操作軌跡產生的組合技能"
        categories = self._skill_categories()
        category = clean(get("category"), 40)
        if category not in categories:
            if category and category != DEFAULT_SKILL_CATEGORY:
                warnings.append(f"分類「{category}」不在 SKILLS.md 裡，改放「{DEFAULT_SKILL_CATEGORY}」")
            category = DEFAULT_SKILL_CATEGORY
        purpose = clean(get("purpose"), 400) or description
        success = clean(get("success_criteria"), 200)

        params, rename, seen = [], {}, set()
        for p in (get("parameters") or []):
            if not isinstance(p, dict):
                continue
            raw = str(p.get("name") or "").strip()
            pname = re.sub(r"[^a-z0-9_]", "_", raw.lower()).strip("_")
            if pname and pname[0].isdigit():
                pname = f"p_{pname}"
            if not pname or pname in seen:
                continue
            seen.add(pname)
            if raw and raw != pname:
                rename[raw] = pname
            params.append({"name": pname, "description": clean(p.get("description"), 80) or pname,
                           "example": str(p.get("example") or "")})
        examples = {p["name"]: p["example"] for p in params}

        model_steps = {}
        for s in (get("steps") or []):
            if isinstance(s, dict) and isinstance(s.get("step_id"), int):
                model_steps[s["step_id"]] = s
        steps_out, excluded, used = [], [], set()
        for g in groups:
            r = g["step"]
            ms = model_steps.get(r["id"])
            if ms is None:
                warnings.append(f"步驟 #{r['id']} 模型未填寫，依原始參數納入")
                ms = {}
            if ms.get("include") is False:
                excluded.append({"step_id": r["id"], "command": r["command"],
                                 "reason": clean(ms.get("purpose"), 60) or "模型判定為探索性步驟"})
                continue
            fallback = f"執行 {r['skill']}" if r.get("skill") else f"執行 {r['script']}"
            step_purpose = clean(ms.get("purpose"), 60) or fallback
            args = list(r["args"])
            proposed = ms.get("args") if isinstance(ms.get("args"), list) else None
            if proposed is not None and len(proposed) != len(args):
                warnings.append(f"步驟 #{r['id']} 的參數數量與實際記錄不同，改用原值")
            elif proposed is not None:
                # 模型給的參數名可能含空白／連字號（{Work Dir}）：先做字面改名再走正規的佔位符處理
                templated = []
                for a in proposed:
                    a = str(a)
                    for raw, new_name in rename.items():
                        a = a.replace("{" + raw + "}", "{" + new_name + "}")
                    templated.append(self._rename_placeholders(a, rename))
                if all(self._fill_placeholders(t, examples) == o for t, o in zip(templated, args)):
                    args = templated
                else:
                    warnings.append(f"步驟 #{r['id']} 的參數化代回原值後與實際記錄不一致，改用原值")
            # 小模型常見：參數宣告得對（example 是原值），args 卻照抄原值、沒放佔位符。example 就是記錄到的原值，
            # 由系統代入是安全的（代回一定等於原值）：整個參數相同直接換；長度 ≥3 的值也允許在參數字串裡以
            # 詞邊界為界的子字串替換（避免 "docker" 換掉 "docker_runcmd" 裡的字）。長的 example 先換。
            auto_filled = []
            for p in sorted(params, key=lambda p: -len(p["example"])):
                ex, pname = p["example"], p["name"]
                if not ex:
                    continue
                for i, a in enumerate(args):
                    if "{" + pname + "}" in a:
                        continue
                    if a == ex:
                        args[i] = "{" + pname + "}"
                        auto_filled.append(pname)
                    elif len(ex) >= 3:
                        new_a = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(ex) + r"(?![A-Za-z0-9_])", "{" + pname + "}", a)
                        if new_a != a:
                            args[i] = new_a
                            auto_filled.append(pname)
            if auto_filled:
                warnings.append(f"步驟 #{r['id']}：模型未放佔位符，系統依原值自動代入參數 {', '.join(sorted(set(auto_filled)))}")
            for a in args:
                used.update(n for n in self._PLACEHOLDER_RE.findall(a) if n in examples)
            steps_out.append({
                "step_id": r["id"], "skill": r.get("skill"), "script": r["script"], "purpose": step_purpose,
                "args": args, "original_args": list(r["args"]), "cwd": r.get("cwd"), "container_cwd": r.get("container_cwd"), "target_container": r.get("target_container"),
            })
        if not steps_out:
            warnings.append("模型把所有步驟都排除了，改為全部納入（可在修改意見指明要拿掉哪幾步）")
            excluded = []
            for g in groups:
                r = g["step"]
                steps_out.append({
                    "step_id": r["id"], "skill": r.get("skill"), "script": r["script"],
                    "purpose": f"執行 {r['skill']}" if r.get("skill") else f"執行 {r['script']}",
                    "args": list(r["args"]), "original_args": list(r["args"]),
                    "cwd": r.get("cwd"), "container_cwd": r.get("container_cwd"), "target_container": r.get("target_container"),
                })
        unused = [p["name"] for p in params if p["name"] not in used]
        if unused:
            warnings.append(f"參數 {', '.join(unused)} 沒有被任何步驟使用，已移除")
            params = [p for p in params if p["name"] in used]

        pitfalls, seen_p = [], set()
        for x in (get("pitfalls") or []):
            t = clean(x, 120)
            if t and t not in seen_p:
                seen_p.add(t)
                pitfalls.append(t)
        pitfalls = pitfalls[:8]

        skills_used = sorted({s["skill"] for s in steps_out if s.get("skill")})
        deps = sorted({d for sk in skills_used for d in self._skill_dependencies(sk)})
        return {
            "name": name, "title": title, "description": description, "category": category,
            "purpose": purpose, "success_criteria": success, "parameters": params, "steps": steps_out,
            "excluded": excluded, "pitfalls": pitfalls, "warnings": warnings,
            "skills_used": skills_used, "dependencies": deps,
            "non_readonly": sorted(set(skills_used) & NON_READONLY_SKILLS),
            "trailing_failures": [fr["command"] for fr in trailing],
            "created": time.strftime("%Y-%m-%d %H:%M"), "model": self.skill_model, "revision": 0,
            "source": {"session": self.session_id, "step_ids": [g["step"]["id"] for g in groups],
                       "plan": plan_text, "tasks": tasks},
        }

    def draft_skill_from_trajectory(self, name, steps, plan_text=None, previous=None, feedback=None):
        """呼叫草擬模型（獨立一次性 session，不碰 self.messages）產生技能草稿 dict。失敗拋 ValueError。"""
        groups, trailing = self._group_trajectory(steps)
        if not groups:
            raise ValueError("選取的步驟裡沒有任何成功的執行，無法編譯成技能。")
        tasks = []
        for r in steps:
            t = (r.get("task") or "").strip()
            if t and t not in tasks:
                tasks.append(t)
        system_prompt, user_prompt = self._make_skill_prompts(name, groups, trailing, plan_text, tasks, previous, feedback)
        res = ollama.chat(
            model=self.skill_model,
            messages=[{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
            format=MAKE_SKILL_SCHEMA,
            options={'temperature': 0.1, 'num_ctx': NUM_CTX, 'num_predict': MAKE_SKILL_MAX_PREDICT},
            think=False,
        )
        raw = res['message']['content'].strip()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            raise ValueError(f"模型沒有回傳合法的 JSON 草稿（開頭：{raw[:120]!r}），請再試一次或換 AGENT_SKILL_MODEL。")
        draft = self._normalize_skill_draft(name, data, groups, trailing, plan_text, tasks)
        draft["raw"] = data
        draft["_steps"] = steps  # 修改意見重擬時要用同一批步驟
        return draft

    # ---------- 範本渲染：規格文件 / 組合腳本 / 索引行 / 預覽 ----------
    def _skill_signature(self, draft):
        return " ".join(f"<{p['name']}>" for p in draft["parameters"])

    def _skill_example_call(self, draft):
        tail = " ".join(self._quote_arg(p["example"]) for p in draft["parameters"])
        return f"EXECUTE: scripts/{draft['name']}_cmd.py" + (f" {tail}" if tail else "")

    def render_skill_doc(self, draft):
        """tools/<name>.md：與其他技能相同的 OKF 段落（用途／語法／範例／回傳／異常），維持精簡。"""
        n = len(draft["steps"])
        lines = [
            "---", "type: Tool", f"title: {draft['title']}", f"description: {draft['description']}",
            "version: 0.1.0", f"dependencies: {json.dumps(draft['dependencies'], ensure_ascii=False)}",
            f"source: make_skill {draft['created']}（組合技能，步驟來自實際操作軌跡；可直接編輯）", "---", "",
            "# 用途", draft["purpose"],
            f"依序執行 {n} 個既有技能的腳本，任一步回 `[ERROR]` 即停止並回報該步原因：",
        ]
        for i, s in enumerate(draft["steps"], 1):
            skill = s["skill"] or s["script"]
            call = " ".join(self._quote_arg(a) for a in s["args"])
            lines.append(f"{i}. {s['purpose']}（{skill}：`scripts/{s['script']}{' ' + call if call else ''}`）")
        lines += ["", "# 語法", f"`EXECUTE: scripts/{draft['name']}_cmd.py {self._skill_signature(draft)}`".replace(" `", "`")]
        for p in draft["parameters"]:
            lines.append(f"* `{p['name']}`：{p['description']}（例：`{p['example']}`）")
        if not draft["parameters"]:
            lines.append("不需要參數。")
        lines += ["", "# 範例", f"`{self._skill_example_call(draft)}`", "", "# 回傳"]
        success = f" {draft['success_criteria']}" if draft["success_criteria"] else ""
        lines.append(f"成功：`[PASS] {draft['name']} 完成 {n}/{n} 步` 加各步驟輸出（標明步驟編號）。{success}".rstrip())
        lines.append("失敗：`[ERROR] ... 在第 k/N 步失敗` 加該步原因，之前步驟的輸出保留供診斷；依原因修正參數，不要原樣重試。")
        lines += ["", "# 異常"]
        for p in draft["pitfalls"]:
            lines.append(f"* {p}")
        skills = "、".join(draft["skills_used"]) or "（無）"
        lines.append(f"* 各步驟的參數規則與其他異常見底層技能的規格：{skills}。")
        return "\n".join(lines) + "\n"

    def render_skill_script(self, draft):
        """scripts/<name>_cmd.py：只有資料（NAME / PARAMS / STEPS），執行邏輯在 _composite.py。
        放在 drafts/<name>/ 時會自己往上找到 scripts/，草稿可直接重播測試。"""
        params = [{"name": p["name"], "description": p["description"], "example": p["example"]} for p in draft["parameters"]]
        steps = [{"purpose": s["purpose"], "skill": s["skill"] or "", "script": s["script"], "args": s["args"],
                  "source_step": s["step_id"]} for s in draft["steps"]]
        header = (
            f'"""{draft["title"]}（make_skill 於 {draft["created"]} 依實際操作軌跡自動產生的組合技能）\n\n'
            f'{draft["description"]}\n'
            "依序執行下列既有技能的腳本，任一步回 [ERROR] 即停止；命令列位置參數依 PARAMS 順序代入 STEPS 的 {名稱} 佔位符。\n"
            "這支檔案只是資料，可直接編輯 PARAMS / STEPS；執行邏輯在同目錄的 _composite.py。\n"
            f'草擬模型：{draft["model"]}；來源軌跡步驟：{draft["source"]["step_ids"]}\n"""\n'
        )
        body = (
            "import os\n"
            "import sys\n\n"
            "_HERE = os.path.dirname(os.path.abspath(__file__))\n"
            "# 正式位置為 skills_system/scripts/；草稿位於 skills_system/drafts/<name>/ 時往上兩層找 scripts/\n"
            '_SCRIPTS_DIR = _HERE if os.path.exists(os.path.join(_HERE, "_composite.py")) \\\n'
            '    else os.path.join(os.path.dirname(os.path.dirname(_HERE)), "scripts")\n'
            "sys.path.insert(0, _SCRIPTS_DIR)\n"
            "from _composite import run_composite\n\n"
            f"NAME = {json.dumps(draft['name'])}\n"
            f"PARAMS = {json.dumps(params, ensure_ascii=False, indent=4)}\n"
            f"STEPS = {json.dumps(steps, ensure_ascii=False, indent=4)}\n\n"
            'if __name__ == "__main__":\n'
            "    print(run_composite(NAME, PARAMS, STEPS, sys.argv[1:], scripts_dir=_SCRIPTS_DIR))\n"
        )
        return header + body

    def skill_index_line(self, draft):
        return f"- [{draft['name']}](tools/{draft['name']}.md) — {draft['description']}"

    def skill_draft_preview(self, draft):
        """給使用者看的預覽：摘要、參數、步驟、排除、注意事項、提醒，最後附完整規格文件。"""
        d = draft
        lines = [f"🧩 技能草稿 {d['name']}：{d['title']}" + (f"（第 {d['revision']} 次重擬）" if d.get("revision") else ""),
                 f"分類：{d['category']}｜索引描述：{d['description']}",
                 f"參數：{len(d['parameters'])} 個" + ("" if d["parameters"] else "（不需要參數）")]
        for p in d["parameters"]:
            lines.append(f"  - {p['name']}：{p['description']}（例：{p['example']}）")
        lines.append(f"步驟：{len(d['steps'])} 步（來自軌跡 #{', #'.join(str(i) for i in d['source']['step_ids'])}）")
        for i, s in enumerate(d["steps"], 1):
            call = " ".join(self._quote_arg(a) for a in s["args"])
            lines.append(f"  {i}. {s['purpose']} — {s['skill'] or s['script']}：scripts/{s['script']}{' ' + call if call else ''}")
        if d["excluded"]:
            lines.append("排除的步驟（要加回請在修改意見指明編號）：")
            for e in d["excluded"]:
                lines.append(f"  - #{e['step_id']} {e['command']}（{e['reason']}）")
        if d["pitfalls"]:
            lines.append("異常／注意事項：")
            lines += [f"  - {p}" for p in d["pitfalls"]]
        notes = list(d["warnings"])
        if d["non_readonly"]:
            notes.append(f"含會改變狀態的步驟（{'、'.join(d['non_readonly'])}）：重播驗證會實際執行這些操作，請先確認。")
        if d["trailing_failures"]:
            notes.append(f"軌跡最後仍有 {len(d['trailing_failures'])} 次未修正的失敗嘗試，未納入步驟：" + "；".join(d["trailing_failures"][:3]))
        if notes:
            lines.append("⚠️ 提醒：")
            lines += [f"  - {n}" for n in notes]
        paths = d.get("paths") or {}
        if paths:
            lines.append(f"草稿檔案：{os.path.relpath(os.path.dirname(paths['doc']), self.script_dir)}/（{d['name']}.md、{d['name']}_cmd.py、draft.json）")
        lines.append("── 規格文件預覽（tools/%s.md）──" % d["name"])
        lines.append(self.render_skill_doc(d).rstrip())
        return "\n".join(lines)

    # ---------- 草稿檔案：寫入 / 重播 / 註冊 / 丟棄 ----------
    def write_skill_draft(self, draft):
        """寫到 skills_system/drafts/<name>/：<name>.md、<name>_cmd.py、draft.json（供稽核與修改意見重擬）。"""
        d = os.path.join(self.drafts_dir, draft["name"])
        os.makedirs(d, exist_ok=True)
        paths = {"doc": os.path.join(d, f"{draft['name']}.md"),
                 "script": os.path.join(d, f"{draft['name']}_cmd.py"),
                 "json": os.path.join(d, "draft.json")}
        with open(paths["doc"], "w", encoding="utf-8") as f:
            f.write(self.render_skill_doc(draft))
        with open(paths["script"], "w", encoding="utf-8") as f:
            f.write(self.render_skill_script(draft))
        draft["paths"] = paths
        with open(paths["json"], "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in draft.items() if k != "_steps"} | {"steps_source": draft.get("_steps")},
                      f, ensure_ascii=False, indent=2)
        return paths

    def replay_skill_draft(self, draft):
        """用軌跡中的原值（各參數的 example）實際跑一次草稿腳本，回傳 (ok, output)。
        cwd 用第一步當時的工作目錄（相對路徑才會一樣），不同步 harness 狀態（這只是測試）。"""
        script = draft["paths"]["script"]
        argv = [p["example"] for p in draft["parameters"]]
        first = draft["steps"][0]
        cwd = first.get("cwd") if first.get("cwd") and os.path.isdir(first["cwd"]) else self.current_cwd
        env = os.environ.copy()
        env["CONTAINER_CWD"] = first.get("container_cwd") or self.container_cwd
        env["TARGET_CONTAINER"] = first.get("target_container") or self.target_container
        try:
            res = subprocess.run([sys.executable, script] + argv, capture_output=True, text=True,
                                 cwd=cwd, env=env, timeout=TOOL_EXEC_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, f"[ERROR] 重播逾時（超過 {TOOL_EXEC_TIMEOUT} 秒）"
        out = res.stdout.strip() or res.stderr.strip() or "（沒有任何輸出）"
        if res.returncode != 0 and not out.startswith("[ERROR]"):
            out = f"[ERROR] 草稿腳本異常結束（exit code {res.returncode}）:\n{out}"
        return not out.lstrip().startswith("[ERROR]"), out

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
        """核准：搬進 tools/ 與 scripts/、寫入 SKILLS.md、刪除草稿目錄、軌跡記起點。回傳給使用者的訊息。"""
        err = self.validate_new_skill_name(draft["name"])
        if err:
            raise ValueError(err)
        doc_path = os.path.join(self.tools_dir, f"{draft['name']}.md")
        script_path = os.path.join(self.base_path, "scripts", f"{draft['name']}_cmd.py")
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write(self.render_skill_doc(draft))
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(self.render_skill_script(draft))
        self._insert_skill_index_line(draft["category"], self.skill_index_line(draft))
        # 稽核：草稿 JSON 搬到 logs/，草稿目錄刪除
        try:
            log_dir = os.path.join(self.script_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, f"make_skill_{draft['name']}_{time.strftime('%Y%m%d_%H%M%S')}.json"),
                      "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in draft.items() if k not in ("_steps", "paths")}, f, ensure_ascii=False, indent=2)
        except (OSError, TypeError, ValueError):
            pass
        self.discard_skill_draft(draft)
        self.add_trajectory_boundary("make_skill", name=draft["name"])
        rel = lambda p: os.path.relpath(p, self.script_dir)
        return (
            f"✅ 已註冊技能 {draft['name']}（{draft['title']}）：\n"
            f"- 規格：{rel(doc_path)}\n"
            f"- 腳本：{rel(script_path)}（組合 {len(draft['steps'])} 步，執行邏輯在 scripts/_composite.py）\n"
            f"- 索引：SKILLS.md「{draft['category']}」新增一行\n"
            f"之後 action.command 填 `{draft['name']}` 載入規格，再依規格執行（範例：`{self._skill_example_call(draft)}`）；"
            f"下一次呼叫 AI 時 system prompt 的技能索引就會包含它。規格與腳本都可以直接手動修改。"
        )

    def discard_skill_draft(self, draft):
        d = os.path.join(self.drafts_dir, draft["name"])
        for fname in ("draft.json", f"{draft['name']}.md", f"{draft['name']}_cmd.py"):
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
        """/make_skill <name> [範圍]：挑步驟、呼叫模型草擬、寫草稿檔、設為待決定。回傳 (draft, error)。"""
        name = (name or "").strip()
        if self.pending_skill_draft:
            return None, (f"已有技能草稿 {self.pending_skill_draft['name']} 待決定：請先核准（y）、"
                          f"重播驗證後核准（t）、取消（n）或送出修改意見。")
        err = self.validate_new_skill_name(name)
        if err:
            return None, err
        steps, err = self.trajectory_steps(spec)
        if err:
            return None, err
        try:
            draft = self.draft_skill_from_trajectory(name, steps, self._plan_for_steps(steps))
        except ValueError as e:
            return None, str(e)
        except Exception as e:
            return None, f"呼叫模型草擬技能失敗：{e}"
        self.write_skill_draft(draft)
        self.pending_skill_draft = draft
        return draft, None

    def revise_skill_draft(self, feedback):
        """使用者的修改意見：帶著上一版 JSON 與意見重擬，同一批步驟。回傳 (draft, error)。"""
        old = self.pending_skill_draft
        if not old:
            return None, "目前沒有待決定的技能草稿。"
        try:
            draft = self.draft_skill_from_trajectory(old["name"], old["_steps"], old["source"]["plan"],
                                                     previous=old.get("raw"), feedback=feedback)
        except ValueError as e:
            return None, str(e)
        except Exception as e:
            return None, f"呼叫模型重擬技能失敗：{e}"
        draft["revision"] = old.get("revision", 0) + 1
        self.write_skill_draft(draft)
        self.pending_skill_draft = draft
        return draft, None

    def approve_skill_draft(self, replay=False):
        """核准並註冊；replay=True 先用原值重播草稿腳本，失敗則保留草稿不註冊。回傳 (ok, message)。"""
        draft = self.pending_skill_draft
        if not draft:
            return False, "目前沒有待決定的技能草稿。"
        note = ""
        if replay:
            ok, out = self.replay_skill_draft(draft)
            if not ok:
                return False, ("🧪 重播驗證失敗，草稿保留、尚未註冊。可送出修改意見重擬、直接核准（y）跳過驗證，"
                               f"或取消（n）：\n{out[:2000]}")
            note = f"🧪 重播驗證通過（以軌跡中的原值執行草稿腳本）：\n{out[:1500]}\n\n"
        try:
            msg = self.register_skill_draft(draft)
        except (OSError, ValueError) as e:
            return False, f"⚠️ 註冊技能失敗，草稿保留：{e}"
        self.pending_skill_draft = None
        return True, note + msg

    def cancel_skill_draft(self):
        draft = self.pending_skill_draft
        if not draft:
            return "目前沒有待決定的技能草稿。"
        self.discard_skill_draft(draft)
        self.pending_skill_draft = None
        return f"🚫 已取消技能草稿 {draft['name']}（草稿檔已刪除，軌跡保留，可再次 /make_skill）。"
