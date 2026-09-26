"""PromptMixin：組 system prompt（AGENT.md、狀態、Objective、計畫、記憶、滾動摘要、技能索引、檢索清單）與 Plan 模式的請求文字。"""
import os


class PromptMixin:

    def load_long_term_memory(self, max_lines=30):
        if not os.path.exists(self.memory_file):
            return "No long-term memory."

        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                lines = f.readlines()

            memory_lines = lines[-max_lines:]
            return "".join(memory_lines)

        except Exception as e:
            return f"[Memory Load Error] {e}"

    def build_plan_request(self, user_task):
        """/plan 模式用：把使用者的原始任務包裝成「先規劃、別執行」的請求。
        不需要另外把技能索引塞進來，因為 SKILLS.md 已經在系統提示詞裡，
        AI 本來就看得到，這裡只需要下達規劃指令即可。"""
        return f"""[PLAN_REQUEST]
請先不要執行任何指令。請依照你目前看到的 SKILLS.md 技能索引，
針對下面的任務規劃出所需的步驟清單，列出來讓使用者確認後才會開始執行。

規則：
- 用條列式（1. 2. 3. ...）列出步驟，簡短清楚即可
- 每個步驟盡量標明會用到的技能名稱（來自 SKILLS.md），以及這步要做什麼
- 如果某步驟不需要任何技能，直接說明要做什麼即可
- 這一輪的 action 必須是 null（不執行任何技能），計畫寫在 reply 裡，等待使用者確認

任務：
{user_task}
"""

    def confirm_plan(self, plan_msg):
        """使用者核准計畫（CLI 的 _run_plan_flow 與 Web 的 handle_plan_response 共用）：
        存進 current_plan 注入 system prompt、加入 [PLAN_CONFIRMED] 訊息，並在操作軌跡記一個起點，
        之後 /make_skill 不指定範圍時就從這裡開始取步驟。"""
        self.current_plan = plan_msg
        self.messages.append({
            'role': 'user',
            'content': "[PLAN_CONFIRMED]\n使用者已核准上述計畫，現在開始依計畫執行第一個步驟。"
        })
        self.add_trajectory_boundary("plan_confirmed", plan=plan_msg)

    def build_plan_revision_request(self, feedback):
        """/plan 模式用：使用者對計畫不滿意時，帶著回饋重新規劃一次。規則同上。"""
        return f"""[PLAN_REVISION]
使用者對你剛才列出的計畫有以下修改意見，請依照意見重新規劃一份新的步驟清單。
規則同上：計畫寫在 reply、action 必須是 null，等待使用者確認。

修改意見：
{feedback}
"""

    def _build_objective_prompt(self):
        """若有設定 sticky objective，組成提醒 AI 優先遵守的區塊；否則回傳空字串。"""
        if not self.sticky_objective:
            return ""

        return f"""
            ## CURRENT PRIMARY OBJECTIVE
            {self.sticky_objective}

            規則:
            - 你必須始終以此任務為最高優先級
            - 除非使用者明確清除 objective
            - 不可自行移除或遺忘
            - 當上下文過長時，優先維持此目標
            """

    def _build_plan_context_prompt(self):
        """若有已核准、正在執行中的任務計畫，組成提醒 AI 依計畫執行的區塊；
        否則回傳空字串。跟 _build_objective_prompt 同一種模式：每次組
        system prompt 都重新塞入，才不會被滑動視窗或壓縮摘要沖掉。"""
        if not self.current_plan:
            return ""

        return f"""
            ## CURRENT APPROVED TASK PLAN
            使用者已核准以下步驟計畫，請依計畫逐步執行：
            {self.current_plan}

            規則:
            - 依計畫逐步執行，一次只做一步，等系統回傳這一步的結果後再進行下一步
            - 除非使用者明確要求變更，否則不可自行更改或遺忘此計畫
            - 這個區塊會持續到使用者送出下一個新任務（或輸入 /plan done）才清除；
              所有步驟都做完後，向使用者回報結果並等待新指示即可
            """

    def _build_task_anchor_text(self):
        """獨立 session（summarize_tool_result、_result_recall）的「使用者的目標」，依序組合：
        - sticky_objective：使用者用 /objective set 設定的最高優先目標（有設定才有）
        - current_task：使用者最新的一句話（每次送出新訊息都由 set_current_task 更新，/clear 清空）
        - task_history：任務線，連同最新一句共最近 TASK_HISTORY_KEEP（3）句使用者的原話；最新一句常只是
          「第一個」這種短回答，原本要做什麼要看前幾句
        - current_plan：Plan 模式核准的計畫（到下一個新任務為止）
        有幾層就給幾層，不互斥。「這一步的目的」（決策 AI 的 thought／reply／action）由 _last_assistant_step
        另外提供，這裡不夾帶 AI 的回覆。"""
        parts = []
        if self.sticky_objective:
            parts.append(f"使用者設定的最高優先 Objective：{self.sticky_objective}")
        if self.current_task:
            parts.append(f"使用者最新的訊息：{self.current_task}")
            earlier = [t for t in self.task_history[:-1] if t and t != self.current_task]
            if earlier:
                # 任務線：使用者回答追問時最新一句常只剩關鍵字（例如「看 motor_rear_left」），原本要做什麼在前幾句
                parts.append("這串對話較早的使用者訊息（舊→新；最新訊息可能只是對其中一項的追問，原本的目的看這裡）：\n"
                             + "\n".join(f"{i}. {t}" for i, t in enumerate(earlier, 1)))
        if self.current_plan:
            parts.append(f"使用者已核准的任務計畫（逐步執行中）：\n{self.current_plan}")
        return "\n".join(parts) if parts else "(未取得使用者原始任務敘述)"

    def get_system_prompt(self):

        objective_prompt = self._build_objective_prompt()
        plan_prompt = self._build_plan_context_prompt()
        tool_use_index_prompt = self._tool_use_index_block()

        profile = ""
        if os.path.exists(self.profile_file):
            with open(self.profile_file, "r", encoding="utf-8") as f:
                profile = f.read()

        with open(self.index_file, "r", encoding="utf-8") as f:
            skills = f.read()

        memory_content = self.load_long_term_memory()
        history_summary = self.rolling_summary or "No history summary yet."

        status_prompt = (f"\n\n## Current Agent State\n- CURRENT_WORKING_DIRECTORY: {self.current_cwd}\n"
                         f"- CURRENT_CONTAINER_DIRECTORY: {self.container_cwd}\n"
                         f"- CURRENT_TARGET_CONTAINER: {self.target_container or '（未設定：需要容器時先用 docker_containers 查、docker_open 選定）'}")

        return f"""
                {profile}
                {status_prompt}
                {objective_prompt}
                {plan_prompt}
                ## Long Term Memory
                {memory_content}
                ## Recent Compressed History Summary
                {history_summary}
                ## Available Skills (SKILLS.md)
                {skills}
                {tool_use_index_prompt}
                """
