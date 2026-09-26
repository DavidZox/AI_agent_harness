"""SkillAgent：把各 mixin 組合成一個類別，並定義所有狀態的初始值（__init__）與 token 計量。"""
import ollama
import os
import threading
import time
from .archive import ArchiveMixin
from .compression import CompressionMixin
from .config import (
    DEFAULT_CHARS_PER_TOKEN,
    MAX_CHARS_PER_TOKEN,
    MIN_CHARS_PER_TOKEN,
    PROJECT_ROOT,
    SKILL_MODEL,
    SUMMARY_MODEL,
)
from .conversation import ConversationMixin
from .dispatch import DispatchMixin
from .make_skill import MakeSkillMixin
from .prompt import PromptMixin
from .tool_summary import ToolSummaryMixin
from .tool_use_index import ToolUseIndexMixin
from .trajectory import TrajectoryMixin


class SkillAgent(ConversationMixin, PromptMixin, DispatchMixin, ToolSummaryMixin, ToolUseIndexMixin, ArchiveMixin, TrajectoryMixin, CompressionMixin, MakeSkillMixin):
    def __init__(self, model="gemma4:e4b", max_history=None, summary_model=None):
        # max_history：訊息「則數」滑動視窗，預設停用（None）。上下文大小統一以 token 門檻
        # （TOKEN_THRESHOLD）觸發壓縮歸檔；這個參數只保留作為極端情境的保險絲，需要時再開。
        # summary_model：壓縮摘要與工具摘要這兩種獨立 session 用的模型，預設與主模型相同；
        # 可用環境變數 AGENT_SUMMARY_MODEL 指定（見檔尾 SUMMARY_MODEL 說明）。
        self.model = model
        self.summary_model = summary_model or SUMMARY_MODEL or model
        self.script_dir = PROJECT_ROOT
        self.base_path = os.path.join(self.script_dir, "skills_system")
        self.index_file = os.path.join(self.base_path, "SKILLS.md")
        self.profile_file = os.path.join(self.script_dir, "AGENT.md")
        self.memory_file = os.path.join(self.script_dir, "Memory.md")

        # 預設工作目錄以程式所在位置為準，不寫死機器特定路徑
        self.current_cwd = self.script_dir
        self.container_cwd = ""
        # 目標容器：容器相關技能預設操作的容器，預設空。跟工作目錄同一套機制——run_tool 以環境變數
        # TARGET_CONTAINER 傳給腳本（省略容器名稱時用它），腳本成功操作某容器後在輸出末行印
        # [TARGET_CONTAINER] <名稱>，_sync_state_from_tool_output 同步回來（比照 cd 的 [CWD_CHANGED]）。
        # 所以它等於「最近一次成功操作的容器」；docker_open 用來刻意選定／切換。system prompt 的
        # Current Agent State 讓模型直接用、不再問使用者要看哪個容器；Web Console 狀態列顯示。
        self.target_container = ""

        self.messages = []
        self.max_history = max_history

        if not os.path.exists(self.index_file):
            raise FileNotFoundError(f"找不到技能索引：{self.index_file}")

        # 技能規格文件（OKF：Open Knowledge Format）目錄，每個技能一份 tools/<name>.md
        self.tools_dir = os.path.join(self.base_path, "tools")
        # 技能綁定的經驗記憶目錄，每個技能一份 memory/<name>.md（由 modify_memory --skill 寫入）。
        # 載入規格文件時自動附在後面，跟規格文件同一套按需載入；全域記憶仍在 Memory.md 常駐。
        self.skill_memory_dir = os.path.join(self.base_path, "memory")

        # =========================
        # 🧠 Token Tracker（真實 token 尺度，見檔尾 NUM_CTX / TOKEN_THRESHOLD 說明）
        # =========================
        self.total_user_tokens = 0
        self.total_ai_tokens = 0
        self.total_tool_tokens = 0
        self.chars_per_token = DEFAULT_CHARS_PER_TOKEN  # 字元→token 校準比，每次 ask_ai 後用 prompt_eval_count 更新
        self.last_prompt_tokens = None   # 最近一次 ask_ai 由 Ollama 回報的完整 prompt token 數（精確）
        self.last_prompt_chars = None    # 該次 prompt 的字元數，之後用來估算增量
        self.last_eval_tokens = None     # 最近一次 ask_ai 產生的 token 數（精確）
        self.auto_compressed = False     # 最近一次 ask_ai 呼叫前是否因超過門檻而自動壓縮（供 UI 顯示）
        self._tokenize_unavailable = False

        # =========================
        # 🎯 Sticky Objective
        # =========================
        self.sticky_objective = None

        # =========================
        # 📝 已核准、正在執行中的任務計畫（/plan 模式用）
        # 跟 sticky_objective 一樣塞進 system prompt，而不是放在 self.messages
        # 裡的一般訊息，這樣才不會被 _truncate_memory 的滑動視窗或
        # compress_context_to_file 的壓縮摘要沖掉，整個任務執行期間都在。
        # =========================
        self.current_plan = None

        # =========================
        # 📌 使用者最近說的話（任務線）
        # current_task 是最新一句、task_history 是連同它在內最近 3 句原話，每次使用者送出新訊息時由
        # set_current_task 更新，/clear 時清空。給獨立 session（summarize_tool_result、_result_recall）當
        # 「使用者的目標」的一部分（與 sticky_objective、current_plan 一起，見 _build_task_anchor_text）。
        # =========================
        self.current_task = None
        self.task_history = []          # 最近 TASK_HISTORY_KEEP 則使用者任務訊息（舊→新），見 set_current_task／_build_task_anchor_text
        self.last_result_id = None      # 最近一次 run_tool 存檔的結果編號（regard 規格載入時為 None），供摘要附註與 UI 顯示
        self.last_result_file = None

        # =========================
        # 🗜️ 滾動摘要與背景壓縮（雙水位線，見檔尾 SOFT_TOKEN_THRESHOLD / KEEP_RECENT_TOKENS 說明）
        # rolling_summary：最新一份「融合摘要」，get_system_prompt 每次注入；每次壓縮都是
        #   「上一份摘要 + 這次要壓的訊息 → 新的一份」，system prompt 只帶一份而不是最近 5 個檔案。
        #   啟動時從 logs/ 最新的 summary_*.md 載入，作為跨 session 的延續。
        # messages_lock：保護 self.messages 的快照與整批替換（背景壓縮執行緒會用到）。
        # =========================
        self.rolling_summary = self._load_latest_summary()
        self.messages_lock = threading.RLock()
        self._compress_thread = None          # 進行中的背景壓縮執行緒（/parallel_cal on 時才會有）
        self._compress_state_lock = threading.Lock()
        self._notices = []                    # 背景壓縮完成／失敗的通知，下一次互動時顯示
        self.last_compression = None          # 最近一次壓縮的統計（檔名、則數、摘要 token 數）
        self.last_tool_summary = None         # 最近一次工具回傳任務導向摘要的統計（structured／input_tokens／omitted_chars…）

        # =========================
        # 🧩 操作軌跡與 make_skill（見檔尾 SKILL_MODEL / MAKE_SKILL_SCHEMA 說明）
        # trajectory：run_tool 每執行一支腳本就記一筆（指令、參數、當時的 cwd、成功／失敗、輸出開頭），
        #   另有 kind="boundary" 的起點記錄（/clear、計畫核准、上一次 make_skill）。存在 messages 之外，
        #   上下文壓縮不會沖掉，/make_skill 從這裡取「做對的步驟」編譯成組合技能，而不是靠模型回憶。
        # pending_skill_draft：等待使用者核准／修改／取消的技能草稿（CLI 與 Web 共用同一份狀態）。
        # =========================
        self.session_id = time.strftime("%Y%m%d_%H%M%S")
        self.trajectory = []
        # 編號跨 session 全域遞增：從既有存檔（logs/tool_results/）的最大編號續編。純數字的 #16 才能在任何一次啟動
        # 都指到同一份存檔——工具使用檢索清單（_tool_use_index_block）就是靠這個讓模型只需要抄編號、不必抄檔名。
        self.trajectory_seq = self._max_archived_id()
        self.drafts_dir = os.path.join(self.base_path, "drafts")
        self.skill_model = SKILL_MODEL or self.summary_model
        self.pending_skill_draft = None
        self.last_reply_retry = None  # 最近一次 ask_ai 是否因「空白回覆」自動重試過（給 UI 顯示的說明），見 ask_ai

    def count_tokens(self, text: str) -> int:
        """估算一段文字的 token 數（真實 token 尺度）。

        Ollama 目前沒有 tokenize API（Python 套件與伺服器皆無），無法對任意文字精確計數，
        這裡用「字元數 ÷ chars_per_token」估算。chars_per_token 會在每次 ask_ai 之後，用
        Ollama 回報的 prompt_eval_count 對整個 prompt 重新校準（見 _record_usage），所以
        使用者輸入、工具回傳的估算尺度跟模型實際計算一致（中文為主的內容實測約 1.8～1.9
        字元/token，舊版寫死的 ÷4 會低估一半以上）。若未來 ollama 套件提供 tokenize()
        會優先使用。"""
        if not text:
            return 0
        if not self._tokenize_unavailable:
            tokenize = getattr(ollama, "tokenize", None)
            if tokenize is None:
                self._tokenize_unavailable = True
            else:
                try:
                    return len(tokenize(model=self.model, prompt=text))
                except Exception:
                    self._tokenize_unavailable = True
        return max(1, round(len(text) / self.chars_per_token))

    def _messages_chars(self) -> int:
        return sum(len(m['content']) for m in self.messages)

    def context_tokens(self) -> int:
        """目前主對話（system prompt + 全部 messages）的 token 數，真實尺度。

        剛呼叫完模型時，以 Ollama 回報的 prompt_eval_count 為基準（精確，含 chat 模板；
        prompt cache 命中時仍回報完整值，已實測），之後每加入一則訊息就用校準比估算增量
        加上去；還沒呼叫過模型（或剛壓縮過）時整段用校準比估算。"""
        chars = self._messages_chars()
        if self.last_prompt_tokens is not None and self.last_prompt_chars is not None:
            delta = chars - self.last_prompt_chars
            return max(0, self.last_prompt_tokens + round(delta / self.chars_per_token))
        return round(chars / self.chars_per_token)


    def _record_usage(self, response, sent_messages=None):
        """用 Ollama 回應的精確計量更新校準與狀態：
        prompt_eval_count = 這次送出的完整 prompt token 數，eval_count = 這次產生的 token 數。
        sent_messages 是這次實際送出的訊息快照；背景壓縮可能在呼叫期間改動 self.messages，
        校準比必須用「真正送出的那份」的字元數來算。"""
        get = getattr(response, "get", None)
        prompt_tokens = get('prompt_eval_count') if get else None
        eval_tokens = get('eval_count') if get else None
        if sent_messages is not None:
            chars = sum(len(m['content']) for m in sent_messages)
        else:
            chars = self._messages_chars()
        if prompt_tokens and chars:
            self.last_prompt_tokens = int(prompt_tokens)
            self.last_prompt_chars = chars
            ratio = chars / prompt_tokens
            if MIN_CHARS_PER_TOKEN <= ratio <= MAX_CHARS_PER_TOKEN:
                self.chars_per_token = ratio
        self.last_eval_tokens = int(eval_tokens) if eval_tokens else None

    def last_ai_tokens(self, text: str) -> int:
        """最近一次 ask_ai 產生內容的 token 數：優先用 Ollama 回報的 eval_count（精確），沒有才估算。"""
        return self.last_eval_tokens if self.last_eval_tokens is not None else self.count_tokens(text)
