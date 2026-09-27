"""PromptMixin：組 system prompt 與每次送出時附在最尾端的動態區，以及 Plan 模式的請求文字。

排列依「多久變一次」決定（Ollama 只重用跟上一次請求相同的開頭，從第一個不同的 token 開始全部重算）：
- system prompt（get_system_prompt）只放不常變的：AGENT.md、目前上下文模式的規則、長期記憶、滾動摘要、
  使用者原話、技能索引、工具使用檢索清單。清單一千多 tokens、只在跑完獨立 session 時多一筆，留在這裡最划算。
- 動態區（build_state_tail）放小而常變的：Current Agent State、Objective、已核准的計畫、檢索清單最新一筆。
  每次呼叫只接在送出快照的最後一則訊息後面（ask_ai），不寫進 self.messages——切目錄不再讓整段歷史重算，
  而且它們離生成點最近，小模型最看得到。"""
import os
from .config import MEMORY_MAX_CHARS


class PromptMixin:

    # ---------------------------------------------------------------- 長期記憶
    def _memory_entries(self):
        """Memory.md 的條目（非空、不是 # 標題或 > 說明的行）；檔案不在時回 None。"""
        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return None
        return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith(("#", ">"))]

    def load_long_term_memory(self, max_chars=None):
        """整份載入 Memory.md（以條目為單位，不再只讀最後 30 行——條目一多，最舊、往往最基礎的規則會無聲消失）。
        超過 MEMORY_MAX_CHARS 才丟最舊的條目，並在 system prompt 註明還有幾條沒載入、UI 提醒使用者整理。
        開頭一句說明寫入時間的意義：描述某個時間點狀態的條目只代表當時，回答現況前要重查。"""
        budget = MEMORY_MAX_CHARS if max_chars is None else max_chars
        entries = self._memory_entries()
        if not entries:
            self.memory_dropped = 0
            return "No long-term memory."
        kept, used = [], 0
        for e in reversed(entries):
            if kept and used + len(e) > budget:
                break
            kept.append(e)
            used += len(e)
        kept.reverse()
        dropped = len(entries) - len(kept)
        if dropped and dropped != self.memory_dropped:
            # 不在這裡呼叫 _add_notice：這個方法可能在 messages_lock 裡被呼叫（壓縮完刷新 system prompt），
            # _add_notice 要拿另一把鎖，順序跟背景壓縮相反。改成留一個字串，pop_notices 取走時一起送出。
            self._memory_warning = (f"⚠️ 長期記憶有 {dropped} 條較舊的條目超過載入上限（{budget} 字，AGENT_MEMORY_MAX_CHARS）沒有載入，"
                                    f"AI 看不到它們；請整理 Memory.md（合併或刪除過時的條目）。")
        self.memory_dropped = dropped
        guide = ("（每條開頭的 [月-日 時:分] 是寫入時間。規則、偏好、做法一律遵守；描述某個時間點狀態或觀察的條目"
                 "（例如「系統整體健康」「資源不足」）只代表當時：使用者問現在的狀態時，先執行會實際查詢系統的技能"
                 "（例如 docker_containers、ROS2_topic_list、workpackage_status）再回答，不要當成現況。）")
        note = f"\n（另有 {dropped} 條較舊的記憶超過載入上限，沒有載入。）" if dropped else ""
        return guide + "\n" + "\n".join(kept) + note

    # ---------------------------------------------------------------- Plan 模式（草稿與執行在 plan.py）
    def end_plan_for_new_task(self):
        """使用者送出新訊息時（CLI／Web 共用）：做完或退出的計畫到此清掉；執行中的計畫保留（使用者可能只是在回答追問，
        計畫要留著才能繼續），用 /plan done 提早結束。回傳要顯示給使用者的說明（沒有要說的回 None）。"""
        status = self.plan_status()
        if status in ("done", "aborted"):
            self.plan_clear()
            return "🧹 上一個計畫已結束，隨新任務清除。"
        if status == "active":
            return "📝 計畫仍在執行中（/plan done 可提早結束）。"
        if self.current_plan:   # 相容：沒有草稿結構、只有文字的計畫
            self.current_plan = None
            return "🧹 上一個已核准的計畫已隨新任務自動清除。"
        return None

    # ---------------------------------------------------------------- 各區塊
    def _build_objective_prompt(self):
        """若有設定 sticky objective，組成提醒 AI 優先遵守的區塊；否則回傳空字串。"""
        if not self.sticky_objective:
            return ""
        return (f"## CURRENT PRIMARY OBJECTIVE\n{self.sticky_objective}\n"
                "規則：你必須始終以此任務為最高優先級；除非使用者明確清除 objective，不可自行移除或遺忘；上下文過長時優先維持此目標。")

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
        if self.plan_status() == "active":
            parts.append(f"使用者已核准的任務計畫（[→] 是目前這一步）：\n{self.plan_text()}")
        elif self.current_plan:
            parts.append(f"使用者已核准的任務計畫（逐步執行中）：\n{self.current_plan}")
        return "\n".join(parts) if parts else "(未取得使用者原始任務敘述)"

    def _user_words_block(self):
        """被壓縮掉的對話裡使用者的原話（程式逐字保留，見 CompressionMixin._keep_user_words）。對話片段存檔的編號與描述
        另外列在「過去的對話片段」一節（ToolUseIndexMixin._conversation_index_block）。"""
        if not self.user_words:
            return ""
        lines = ["## 使用者說過的話（已被壓縮的對話裡使用者的原話，程式逐字保留，舊→新）"]
        lines += [f"- [{w['ts']}] {w['text']}" if w.get("ts") else f"- {w['text']}" for w in self.user_words]
        if self.user_words_dropped:
            lines.append(f"（更早還有 {self.user_words_dropped} 則沒有列出，原文在「過去的對話片段」的存檔裡）")
        return "\n".join(lines)

    # ---------------------------------------------------------------- system prompt（不常變的部分）
    def get_system_prompt(self):
        profile = ""
        if os.path.exists(self.profile_file):
            with open(self.profile_file, "r", encoding="utf-8") as f:
                profile = f.read().strip()
        with open(self.index_file, "r", encoding="utf-8") as f:
            skills = f.read().strip()
        mode_rules = self.context_mode_rules()
        blocks = [
            profile,
            f"## Context Mode：{self.context_mode}\n{mode_rules}" if mode_rules else f"## Context Mode：{self.context_mode}",
            f"## Long Term Memory\n{self.load_long_term_memory()}",
            f"## Recent Compressed History Summary\n{self.rolling_summary or 'No history summary yet.'}",
            self._user_words_block(),
            self._conversation_index_block(),
            f"## Available Skills (SKILLS.md)\n{skills}",
            self._tool_use_index_block().strip(),
        ]
        return "\n\n".join(b for b in blocks if b)

    # ---------------------------------------------------------------- 動態區（每次送出時附在最尾端）
    def build_state_tail(self):
        """每次呼叫主模型時接在最後一則訊息後面的「目前狀態」（不寫進 self.messages，見 ConversationMixin._with_state_tail）。"""
        state = (f"## Current Agent State\n- CURRENT_WORKING_DIRECTORY: {self.current_cwd}\n"
                 f"- CURRENT_CONTAINER_DIRECTORY: {self.container_cwd}\n"
                 f"- CURRENT_TARGET_CONTAINER: {self.target_container or '（未設定：需要容器時先用 docker_containers 查、docker_open 選定）'}")
        blocks = [state, self._build_objective_prompt(), self.plan_block(), self.tool_use_index_pointer()]
        body = "\n\n".join(b for b in blocks if b)
        return ("[harness state]\n（以下是系統每次附上的目前狀態，不是使用者說的話；請回應上面那則訊息）\n" + body)
