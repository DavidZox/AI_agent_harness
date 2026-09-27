"""PlanMixin：/plan 模式——計畫是 harness 保存的草稿，模型只負責提出內容或修改，不重寫整份。

流程：
1. 草稿：/plan on 之後的新任務，交給一次性的規劃 session 產生第一版（plan_start_draft，format=PLAN_DRAFT_SCHEMA），
   或使用者自己用 /plan add 一條條加。草稿不進主對話，存在 self.plan 並寫成 logs/plan_draft.md。
2. 修改：使用者直接下指令（/plan add／insert／edit／del／move，完全不經過模型），或用自然語言說要怎麼改——
   這時規劃 session 只輸出「要套用的操作」（format=PLAN_OPS_SCHEMA），由 harness 套在草稿上。
   小模型每次重寫整份時會把 4 步變 2 步、改掉沒提到的步驟，所以不讓它重寫。
3. 核准（y）：任務原文＋[PLAN_CONFIRMED] 才進主對話；步驟與進度每次都顯示在動態區（plan_block）。
4. 執行：依執行紀錄自動推進（plan_on_exec：這一步的技能執行成功就換下一步），不需要模型自己勾。
   /plan_exec_guard on 時另外有執行前檢查（plan_block_reason）：會改變狀態、但不是目前這一步的技能不執行；
   模型停下來而計畫還沒做完時自動提醒它繼續（plan_after_reply）。連續失敗 PLAN_MAX_FAILURES 次就退出計畫、交回使用者。
   沒開 /plan_exec_guard 時，計畫只是顯示與進度，沒有任何限制。"""
import os
import re
import time
from .config import DERIVED_RESULT_SCRIPTS, NUM_CTX, PLAN_DRAFT_FILE, PLAN_MAX_FAILURES, PLAN_MAX_STEPS
from .schemas import PLAN_DRAFT_SCHEMA, PLAN_OPS_SCHEMA


_MARKS = {"pending": "[ ]", "done": "[✓]"}
PLAN_HELP = ("y＝核准並執行｜n＝取消｜/plan add <技能>：<要做什麼>｜/plan insert <N> <技能>：<要做什麼>｜"
             "/plan edit <N> <技能>：<要做什麼>｜/plan del <N>｜/plan move <N> <M>｜/plan show｜其他文字＝用說的改（AI 只提出要改哪幾條）")


class PlanMixin:

    # ---------------------------------------------------------------- 狀態與顯示
    def plan_status(self):
        """None（沒有計畫）／draft（草稿、等使用者核准）／active（執行中）／done（做完）／aborted（連續失敗退出）。"""
        return self.plan["status"] if self.plan else None

    def _plan_current(self):
        """目前這一步的索引（第一個還沒完成的）；全部完成回 None。"""
        return next((i for i, st in enumerate(self.plan["steps"]) if st["status"] != "done"), None) if self.plan else None

    def plan_text(self, marks=True):
        """步驟清單的文字（草稿與執行中共用）：1. [✓] 技能：要做什麼。"""
        if not self.plan or not self.plan["steps"]:
            return "（草稿是空的：用 /plan add <技能>：<要做什麼> 加一步）"
        cur = self._plan_current() if self.plan["status"] == "active" else None
        lines = []
        for i, st in enumerate(self.plan["steps"]):
            mark = ("[→]" if i == cur else _MARKS[st["status"]]) + " " if marks and self.plan["status"] != "draft" else ""
            skill = st["skill"] or "（不需技能）"
            lines.append(f"{i + 1}. {mark}{skill}：{st['goal']}")
        return "\n".join(lines)

    def plan_view(self):
        """給使用者看的草稿（CLI 印出、Web 的計畫卡片）。"""
        warn = f"\n⚠️ {self.plan['warning']}" if self.plan and self.plan.get("warning") else ""
        return f"{self.plan_text()}{warn}\n\n{PLAN_HELP}"

    def _plan_save(self):
        """草稿與進度寫成 logs/plan_draft.md（給人看、除錯用；程式以 self.plan 為準）。"""
        if not self.plan:
            return
        try:
            d = os.path.join(self.script_dir, "logs")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, PLAN_DRAFT_FILE), "w", encoding="utf-8") as f:
                f.write(f"# 計畫（{self.plan['status']}，{time.strftime('%Y-%m-%d %H:%M:%S')}）\n\n"
                        f"任務：{self.plan['task']}\n\n{self.plan_text()}\n")
        except OSError:
            pass

    def plan_stats(self):
        """Web 狀態列用。"""
        if not self.plan:
            return None
        done = sum(1 for st in self.plan["steps"] if st["status"] == "done")
        return {"status": self.plan["status"], "done": done, "total": len(self.plan["steps"]),
                "text": self.plan_text(), "exec_guard": self.plan_exec_guard, "failures": self.plan["failures"]}

    def plan_block(self):
        """動態區的計畫（只有執行中才出現）；沒有就回空字串。"""
        if self.plan_status() != "active":
            return ""
        cur = self._plan_current()
        total = len(self.plan["steps"])
        lines = [f"## CURRENT APPROVED TASK PLAN（使用者已核准；第 {cur + 1 if cur is not None else total}/{total} 步）",
                 self.plan_text(),
                 "規則：[→] 是現在這一步，一次只做一步；這一步的技能執行成功，系統會自動換到下一步，不用自己勾。"
                 "除非使用者要求，不要更改計畫；全部做完後向使用者回報結果。"]
        if self.plan_exec_guard:
            lines.append("執行前檢查已開啟：會改變系統狀態、但不是目前這一步的技能，系統不會執行。")
        return "\n".join(lines)

    # ---------------------------------------------------------------- 技能名稱與步驟文字
    def _skill_names(self):
        return [s["name"] for s in self.list_skills()]

    def _parse_step_text(self, text):
        """「技能：要做什麼」／「技能 要做什麼」→ (技能, 要做什麼)；開頭不是技能名稱就當成不需技能的一步。"""
        text = " ".join(str(text or "").split())
        names = sorted(self._skill_names(), key=len, reverse=True)
        for name in names:
            if text == name or text.startswith((name + "：", name + ":", name + " ")):
                return name, text[len(name):].lstrip("：: ").strip() or name
        return "", text

    def _clean_steps(self, raw_steps):
        """模型給的步驟 → [{skill, goal, status}]；技能名稱不在 SKILLS.md 的改成不需技能並記一個提醒。"""
        names, steps, unknown = set(self._skill_names()), [], []
        for st in raw_steps or []:
            if not isinstance(st, dict):
                continue
            skill = str(st.get("skill") or "").strip()
            goal = " ".join(str(st.get("goal") or "").split())[:160]
            if skill and skill not in names:
                unknown.append(skill)
                goal = f"{goal}（原本寫的技能「{skill}」不在技能索引裡）"
                skill = ""
            if goal or skill:
                steps.append({"skill": skill, "goal": goal or skill, "status": "pending"})
        return steps[:PLAN_MAX_STEPS], unknown

    # ---------------------------------------------------------------- 規劃 session（一次性，不碰主對話）
    def _plan_session(self, system_prompt, user_prompt, schema):
        import json
        try:
            res = self._timed_chat(
                "plan", model=self.model,
                messages=[{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}],
                format=schema, options={'temperature': 0.2, 'num_ctx': NUM_CTX}, think=False,
            )
        except Exception as e:   # 連不到 Ollama 等：不讓 CLI 主迴圈整個結束，草稿維持原樣、提示改用 /plan 指令
            self.plan_error = f"規劃 session 失敗：{e}"
            return None
        self.plan_error = None
        try:
            data = json.loads(res['message']['content'])
            return data if isinstance(data, dict) else None
        except (ValueError, TypeError):
            return None

    def _skill_catalog_text(self):
        return "\n".join(f"- {s['name']}：{s['description']}" for s in self.list_skills())

    def plan_start_draft(self, task):
        """/plan on 之後的新任務：規劃 session 產生第一版草稿。回傳給使用者看的文字。"""
        system_prompt = (
            "你是任務規劃器：把使用者的任務拆成依序執行的步驟，交給另一個 AI 一步一步執行。\n"
            "規則：\n1. 每一步只做一件事，寫成 {skill, goal}：skill 是下面技能清單裡的名稱（照抄），goal 是這一步要做什麼、"
            "要看什麼結果（一句話）。\n2. 不需要技能的步驟（例如整理結果回報使用者）skill 給空字串。\n"
            "3. 不要把「載入規格」列成一步；不要列使用者沒要求的額外檢查；步驟數以剛好完成任務為準。\n"
            f"4. 最多 {PLAN_MAX_STEPS} 步。只輸出 JSON。")
        user_prompt = (f"【技能清單】\n{self._skill_catalog_text()}\n\n【目前狀態】工作目錄 {self.current_cwd}；"
                       f"目標容器 {self.target_container or '（未設定）'}\n\n【任務】\n{task}")
        data = self._plan_session(system_prompt, user_prompt, PLAN_DRAFT_SCHEMA)
        steps, unknown = self._clean_steps((data or {}).get("steps"))
        self.plan = {"task": task, "steps": steps, "status": "draft", "failures": 0,
                     "warning": (f"技能名稱不在索引裡：{'、'.join(unknown)}" if unknown else
                                 (f"{self.plan_error or '規劃 session 沒有給出步驟'}，請用 /plan add 自己加" if not steps else ""))}
        self._plan_save()
        return self.plan_view()

    def _plan_revise(self, feedback):
        """自然語言的修改意見：規劃 session 只輸出操作（add／insert／edit／delete／move），harness 套用。"""
        system_prompt = (
            "你會收到一份計畫草稿（有編號）與使用者的修改意見。只輸出要套用在草稿上的操作，不要重寫整份，"
            "使用者沒提到的步驟不要動。\n操作：add（加在最後）、insert（插在第 step 步之前）、edit（改第 step 步）、"
            "delete（刪第 step 步）、move（把第 step 步移到第 to 步）。step／to 一律用下面草稿上原本的編號（從 1 開始，不用考慮前面的操作造成的位移），"
            "用不到的欄位給 0 或空字串；"
            "skill 是技能清單裡的名稱或空字串（不需要技能）；edit 時沒有要改的欄位照抄原本的內容。只輸出 JSON。")
        user_prompt = (f"【技能清單】\n{self._skill_catalog_text()}\n\n【目前的草稿】\n{self.plan_text(marks=False)}\n\n"
                       f"【使用者的修改意見】\n{feedback}")
        data = self._plan_session(system_prompt, user_prompt, PLAN_OPS_SCHEMA)
        ops = [o for o in (data or {}).get("ops") or [] if isinstance(o, dict)]
        if not ops:
            why = self.plan_error or "沒看懂要改哪一條"
            return "edited", f"{why}，草稿沒有變動（可以用 /plan edit／del／insert 直接改）。\n\n{self.plan_view()}"
        # 模型給的編號都是對照「原本的草稿」寫的；依序套用時前面的插入／刪除會讓後面的編號位移（實測：先插入一步再
        # 「刪第 5 步」，刪到的是原本的第 4 步）。所以先把編號換成原本那一步的物件，套用時再查它現在在第幾步。
        original = list(self.plan["steps"])
        refs = []
        for o in ops:
            n, to = int(o.get("step") or 0), int(o.get("to") or 0)
            refs.append((o, original[n - 1] if 1 <= n <= len(original) else None,
                         original[to - 1] if 1 <= to <= len(original) else None, n, to))
        notes = []
        for o, ref, to_ref, n, to in refs:
            o = dict(o)
            steps = self.plan["steps"]
            if o.get("op") in ("edit", "delete", "move", "insert"):
                if ref is not None and any(x is ref for x in steps):
                    o["step"] = next(i for i, x in enumerate(steps) if x is ref) + 1
                elif o.get("op") == "insert" and n == len(original) + 1:
                    o["step"] = len(steps) + 1          # 插在原本最後一步之後＝加在最後
                else:
                    notes.append(f"{o.get('op')}：原本的第 {n} 步已經不在，略過")
                    continue
            if o.get("op") == "move":
                if to_ref is None or not any(x is to_ref for x in steps):
                    notes.append(f"move：原本的第 {to} 步已經不在，略過")
                    continue
                o["to"] = next(i for i, x in enumerate(steps) if x is to_ref) + 1
            notes.append(self._plan_apply(o))
        return "edited", "\n".join(f"・{n}" for n in notes) + f"\n\n{self.plan_view()}"

    # ---------------------------------------------------------------- 套用操作（使用者指令與規劃 session 共用）
    def _plan_apply(self, op):
        steps = self.plan["steps"]
        kind = str(op.get("op") or "")
        n, to = int(op.get("step") or 0), int(op.get("to") or 0)
        cleaned = self._clean_steps([{"skill": op.get("skill"), "goal": op.get("goal")}])[0]
        new = cleaned[0] if cleaned else None
        if kind in ("add", "insert") and new is None:
            return f"{kind}：沒有內容，略過"
        if kind == "add":
            if len(steps) >= PLAN_MAX_STEPS:
                return f"已經 {PLAN_MAX_STEPS} 步，沒有再加"
            steps.append(new)
            return f"加在第 {len(steps)} 步：{new['skill'] or '（不需技能）'}：{new['goal']}"
        if not 1 <= n <= len(steps) + (1 if kind == "insert" else 0):
            return f"{kind}：沒有第 {n} 步，略過"
        if kind == "insert":
            steps.insert(n - 1, new)
            return f"插在第 {n} 步：{new['skill'] or '（不需技能）'}：{new['goal']}"
        if kind == "edit":
            old = steps[n - 1]
            if new is not None:   # skill 空字串＝這一步不需技能；goal 空的就留原本的
                old["skill"] = new["skill"]
                old["goal"] = new["goal"] if str(op.get("goal") or "").strip() else old["goal"]
            return f"改第 {n} 步：{old['skill'] or '（不需技能）'}：{old['goal']}"
        if kind == "delete":
            gone = steps.pop(n - 1)
            return f"刪掉第 {n} 步：{gone['skill'] or '（不需技能）'}：{gone['goal']}"
        if kind == "move":
            if not 1 <= to <= len(steps):
                return f"move：沒有第 {to} 步，略過"
            steps.insert(to - 1, steps.pop(n - 1))
            return f"第 {n} 步移到第 {to} 步"
        return f"不認得的操作「{kind}」，略過"

    def plan_edit_command(self, text):
        """/plan add|insert|edit|del|move|show …（使用者直接改，不經過模型）。沒有草稿時 add 會開一份新的空草稿。
        回傳 (是否處理了, 給使用者看的文字)。"""
        m = re.match(r"^/plan\s+(add|insert|edit|del|delete|move|show)\b\s*(.*)$", text.strip(), re.I | re.S)
        if not m:
            return False, ""
        verb, rest = m.group(1).lower(), m.group(2).strip()
        if verb == "show":
            return True, (self.plan_view() if self.plan else "目前沒有計畫。")
        if self.plan_status() not in ("draft", None):
            return True, f"計畫已在執行（{self.plan_status()}），不能再改；要重來請 /plan done 後重新規劃。"
        if self.plan is None:
            if verb != "add":
                return True, "目前沒有草稿：先 /plan on 再送出任務讓 AI 規劃，或用 /plan add <技能>：<要做什麼> 自己開始。"
            self.plan = {"task": "（使用者自己列的計畫）", "steps": [], "status": "draft", "failures": 0, "warning": ""}
        nums = re.findall(r"\d+", rest)
        if verb == "add":
            skill, goal = self._parse_step_text(rest)
            note = self._plan_apply({"op": "add", "skill": skill, "goal": goal})
        elif verb == "insert":
            num = re.match(r"^(\d+)\s+(.*)$", rest, re.S)
            if not num:
                return True, "用法：/plan insert <N> <技能>：<要做什麼>"
            skill, goal = self._parse_step_text(num.group(2))
            note = self._plan_apply({"op": "insert", "step": int(num.group(1)), "skill": skill, "goal": goal})
        elif verb == "edit":
            num = re.match(r"^(\d+)\s+(.*)$", rest, re.S)
            if not num:
                return True, "用法：/plan edit <N> <技能>：<要做什麼>"
            skill, goal = self._parse_step_text(num.group(2))
            note = self._plan_apply({"op": "edit", "step": int(num.group(1)), "skill": skill, "goal": goal})
        elif verb in ("del", "delete"):
            if not nums:
                return True, "用法：/plan del <N>"
            note = self._plan_apply({"op": "delete", "step": int(nums[0])})
        else:
            if len(nums) < 2:
                return True, "用法：/plan move <N> <M>"
            note = self._plan_apply({"op": "move", "step": int(nums[0]), "to": int(nums[1])})
        self.plan["warning"] = ""
        self._plan_save()
        return True, f"・{note}\n\n{self.plan_view()}"

    def plan_handle_input(self, text):
        """草稿等待核准時使用者的輸入（CLI 與 Web 共用）：y 核准／n 取消／/plan 指令／其他文字＝自然語言修改。
        回傳 (結果, 給使用者看的文字)，結果是 approved／rejected／edited。"""
        choice = text.strip()
        if choice.lower() == "y":
            if not self.plan["steps"]:
                return "edited", "草稿是空的，不能核准：先用 /plan add 加步驟。"
            self.plan_approve()
            return "approved", f"✅ 計畫已核准，開始執行：\n{self.plan_text()}"
        if choice == "" or choice.lower() in ("n", "no", "/plan done", "/plan off"):
            self.plan = None
            return "rejected", "🚫 已取消計畫，本次任務不會執行。"
        handled, msg = self.plan_edit_command(choice)
        if handled:
            return "edited", msg
        return self._plan_revise(choice)

    def plan_approve(self):
        """核准：任務原文＋[PLAN_CONFIRMED] 才進主對話（草稿與修改過程都沒進去）；步驟與進度之後每次都在動態區。"""
        self.plan["status"] = "active"
        self.plan["failures"] = 0
        self.plan["warning"] = ""
        self.current_plan = self.plan_text(marks=False)   # 獨立 session 的錨點、/make_skill 的計畫
        self.messages.append({'role': 'user', 'content': (
            f"{self.plan['task']}\n\n[PLAN_CONFIRMED]\n使用者已核准這個任務的計畫（完整步驟與進度在每則訊息最後的"
            f"「CURRENT APPROVED TASK PLAN」），現在從第 1 步開始執行。")})
        self.add_trajectory_boundary("plan_confirmed", plan=self.current_plan)
        self._plan_save()

    def plan_clear(self):
        self.plan = None
        self.current_plan = None

    # ---------------------------------------------------------------- 執行中：進度、失敗、執行前檢查
    def _plan_fail(self, why):
        """記一次失敗；滿 PLAN_MAX_FAILURES 次就退出計畫、交回使用者。回傳是否已退出。"""
        self.plan["failures"] += 1
        if self.plan["failures"] < PLAN_MAX_FAILURES:
            return False
        cur = self._plan_current()
        self.plan["status"] = "aborted"
        self._plan_save()
        step = self.plan["steps"][cur] if cur is not None else None
        where = f"第 {cur + 1} 步（{step['skill'] or '不需技能'}：{step['goal']}）" if step else "最後"
        self._add_notice(f"⚠️ 計畫連續 {PLAN_MAX_FAILURES} 次失敗（最後一次：{why}），已退出計畫模式，卡在{where}；"
                         f"請告訴 AI 要怎麼繼續，或 /plan done 清掉計畫。")
        self.current_plan = None
        return True

    def plan_on_exec(self, record):
        """_record_trajectory 每記一筆執行就呼叫：這一步（或它之前不需技能的步驟之後）的技能執行成功 → 換下一步；
        [ERROR] → 記一次失敗。存檔工具（result_*）是回查，不算步驟也不算失敗。"""
        if self.plan_status() != "active" or record.get("script") in DERIVED_RESULT_SCRIPTS:
            return
        if record.get("status") != "PASS":
            self._plan_fail(f"{record.get('skill') or record.get('script')} 回傳 [ERROR]")
            return
        steps, cur = self.plan["steps"], self._plan_current()
        if cur is None:
            return
        # 目前這一步不需技能時（例如「整理後回報」），往後找第一個需要技能的步驟來比對
        target = next((i for i in range(cur, len(steps)) if steps[i]["skill"]), None)
        if target is None or record.get("skill") != steps[target]["skill"]:
            return
        for i in range(cur, target + 1):
            steps[i]["status"] = "done"
        self.plan["failures"] = 0
        self._plan_save()
        nxt = self._plan_current()
        self._add_notice(f"📝 計畫第 {target + 1} 步完成" + (f"，換到第 {nxt + 1} 步：{steps[nxt]['skill'] or '（不需技能）'}：{steps[nxt]['goal']}"
                                                         if nxt is not None else "，全部步驟完成。"))
        if nxt is None:
            self.plan["status"] = "done"
            self.current_plan = None

    def plan_block_reason(self, parsed):
        """/plan_exec_guard on 且計畫執行中時的執行前檢查：會改變狀態、但不是目前這一步的技能 → 回傳擋下的說明（給模型）；
        其他情況回 None（唯讀技能、載入規格、存檔工具、這一步的技能都放行——小模型需要保留修正錯誤的空間）。"""
        if not self.plan_exec_guard or self.plan_status() != "active":
            return None
        info = self.state_change_info(parsed)
        if not info:
            return None
        cur = self._plan_current()
        steps = self.plan["steps"]
        target = next((i for i in range(cur, len(steps)) if steps[i]["skill"]), None) if cur is not None else None
        if target is not None and info["skill"] == steps[target]["skill"]:
            return None
        step = steps[target] if target is not None else None
        now = f"第 {target + 1} 步：{step['skill']}：{step['goal']}" if step else "剩下的步驟都不需要執行技能"
        return (f"[PLAN_BLOCKED] {info['skill']} 會改變系統狀態，但不是計畫目前這一步，這次沒有執行。現在是{now}。"
                f"請照計畫做這一步；計畫本身需要改，就告訴使用者。")

    def plan_after_reply(self, parsed):
        """模型這一輪沒有下 action 時呼叫：計畫執行中而剩下的都是不需技能的步驟 → 視為完成；/plan_exec_guard on 且還有要執行
        技能的步驟 → 回傳要接著送給模型的提醒（harness 自動推進，使用者不必自己催），並記一次失敗；其他情況回 None。"""
        if self.plan_status() != "active" or (parsed or {}).get("action"):
            return None
        steps, cur = self.plan["steps"], self._plan_current()
        if cur is None or all(not st["skill"] for st in steps[cur:]):
            for st in steps:
                st["status"] = "done"
            self.plan["status"] = "done"
            self.current_plan = None
            self._plan_save()
            self._add_notice("📝 計畫的步驟都完成了。")
            return None
        if not self.plan_exec_guard:
            return None
        if self._plan_fail("AI 沒有繼續執行計畫"):
            return None
        target = next(i for i in range(cur, len(steps)) if steps[i]["skill"])
        return (f"[PLAN_CONTINUE]\n計畫還沒做完：現在是第 {target + 1} 步：{steps[target]['skill']}：{steps[target]['goal']}。"
                f"請直接執行這一步（action 填技能名稱或規格標明的腳本路徑）。如果缺參數或需要使用者決定，就在 reply 說明原因、action 填 null。")
