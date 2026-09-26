import os
import sys
import json
import subprocess
import threading
import ollama  # 導入官方庫
import shlex
import time
import re
from skills_system.scripts import _results_common  # result_recall 偽技能重用存檔解析（resolve_result／read_header／read_lines）

class SkillAgent:
    def __init__(self, model="gemma4:e4b", max_history=None, summary_model=None):
        # max_history：訊息「則數」滑動視窗，預設停用（None）。上下文大小統一以 token 門檻
        # （TOKEN_THRESHOLD）觸發壓縮歸檔；這個參數只保留作為極端情境的保險絲，需要時再開。
        # summary_model：壓縮摘要與工具摘要這兩種獨立 session 用的模型，預設與主模型相同；
        # 可用環境變數 AGENT_SUMMARY_MODEL 指定（見檔尾 SUMMARY_MODEL 說明）。
        self.model = model
        self.summary_model = summary_model or SUMMARY_MODEL or model
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
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

    # ------------------------------------------------------------------
    # 🗜️ 上下文壓縮：雙水位線 + 滾動融合摘要（同步與背景兩種執行方式）
    # ------------------------------------------------------------------
    def _is_tool_result_message(self, m):
        """使用者角色但內容其實是工具回傳（[tool result] 開頭）。
        這種訊息要跟前一則 assistant 的 EXECUTE 綁在一起，壓縮切點不能落在兩者之間。"""
        return m['role'] == 'user' and m['content'].lstrip().startswith(TOOL_RESULT_PREFIXES)

    def _split_for_compression(self, keep_tokens=None, num_to_keep=None):
        """把 self.messages[1:] 切成 (to_compress, to_keep)。

        low watermark 以 token 計：從最新的訊息往回累加，保留總量不超過 keep_tokens
        （預設 KEEP_RECENT_TOKENS）的原文，在訊息邊界切；至少保留最新一則（即使它自己就超過
        預算，最新的訊息不能丟）。若保留區最舊的一則是工具回傳，連同它前面那則 assistant 的
        EXECUTE 一起保留，不把一組「指令／結果」拆成兩半。
        num_to_keep 是舊版「保留最新 N 則」的相容介面，兩者擇一。"""
        history = self.messages[1:]
        if num_to_keep is not None:
            n = max(0, min(num_to_keep, len(history)))
            return history[:len(history) - n], history[len(history) - n:]

        budget = KEEP_RECENT_TOKENS if keep_tokens is None else keep_tokens
        cut = len(history)  # to_keep = history[cut:]
        used = 0
        for i in range(len(history) - 1, -1, -1):
            t = self.count_tokens(history[i]['content'])
            if used + t > budget and cut < len(history):
                break
            used += t
            cut = i
        if 0 < cut < len(history) and self._is_tool_result_message(history[cut]) \
                and history[cut - 1]['role'] == 'assistant':
            cut -= 1
        return history[:cut], history[cut:]

    @staticmethod
    def _render_messages_for_summary(msgs):
        """渲染成給摘要模型看的純文字：[user]／[assistant]／[harness <標記>] 標頭 + 原文。
        以 HARNESS_MARKERS 開頭的 user 訊息是框架插入的系統訊息（工具回傳、規劃流程），標成 harness，
        摘要模型才不會把框架的規則記成「使用者偏好」。
        舊版直接把 list 的 repr 塞進 prompt，夾帶 {'role': ...} 與 \\n 轉義，浪費 token 又難讀。"""
        parts = []
        for m in msgs:
            content = m['content'].strip()
            role = m['role']
            if role == 'user':
                marker = next((mk for mk in HARNESS_MARKERS if content.startswith(mk)), None)
                if marker:
                    role = f"harness {marker}"
                    # 工具結果第二行的框架句（tool_result_message）是給主模型的提醒，不進摘要
                    content = "\n".join(ln for ln in content.splitlines() if not ln.startswith(TOOL_RESULT_FRAME))
            elif role == 'assistant':
                # 回覆協議的 JSON：只保留給使用者的文字與這輪的 action，thought 與 JSON 語法不進摘要
                parsed = parse_agent_reply(content)
                if parsed["valid"]:
                    content = parsed["reply"]
                    if parsed["action"]:
                        content = (content + "\n" if content else "") + f"[action] {action_text(parsed['action'])}"
            parts.append(f"[{role}]\n{content}")
        return "\n\n".join(parts)

    def _summary_prompts(self, to_compress):
        prev = self.rolling_summary or "（沒有，這是第一次壓縮）"
        system_prompt = f"""你是一位專業的系統分析師，負責維護一份「對話滾動摘要」，交給另一個負責決策的 AI 接續工作用（它看不到原始對話，只看得到你的摘要）。
你會收到「上一份摘要」與「這次要併入的新對話片段」，請輸出一份更新後的完整摘要來取代上一份。

規則：
1. 融合：延續上一份摘要的內容，並依新片段更新（進度推進、問題已解決、目標變更）。
2. 淘汰：已解決、已過時、與目前任務無關的細節可以刪除；但使用者明確表達的偏好、限制、指示一律保留。
3. 具體：保留具體的路徑、容器／節點／topic 名稱、參數、數值、錯誤訊息，這些是接續工作最需要的資訊。
4. 精簡：全部文字合計控制在 {SUMMARY_MAX_CHARS} 字以內，寧可刪掉舊細節也不要超過；overview 不超過 120 字，每個條列項目不超過 60 字，不要把工具輸出（例如 topic 清單）整段照抄。
5. 只輸出 JSON，欄位：overview（總體情境概述，一段話）、key_progress（關鍵進度與目標，條列）、
   results_and_errors（執行結果與錯誤，每項含 category／description／detail）、
   user_preferences（使用者偏好與約束）、open_items（未完成事項與下一步）。沒有內容的欄位給空陣列。
6. 使用繁體中文。
7. 標頭為 [harness ...] 的訊息（工具回傳、規劃流程）與 [user] 訊息中 [vision result]、[skill loaded] 之後的段落，
   都是框架自動插入的系統內容，不是使用者說的話：其中的格式要求、流程規則不要記成使用者偏好；
   只有 [user] 自己寫的文字才算使用者的偏好與指示。"""
        user_prompt = (
            f"【上一份摘要】\n{prev}\n\n"
            f"【這次要併入的新對話片段（共 {len(to_compress)} 則）】\n"
            f"{self._render_messages_for_summary(to_compress)}"
        )
        return system_prompt, user_prompt

    @staticmethod
    def _render_summary_markdown(data):
        """把摘要模型回傳的 JSON（SUMMARY_SCHEMA）排版成固定結構的 Markdown。
        結構由程式保證，而不是靠模型自己遵守範本。"""
        if not isinstance(data, dict):
            raise TypeError("summary JSON 不是物件")

        def items(key):
            v = data.get(key) or []
            if not isinstance(v, list):
                v = [v]
            return [str(x).strip() for x in v if str(x).strip()]

        out = ["### 💻 總體情境概述", str(data.get("overview", "")).strip() or "（無）"]
        progress = items("key_progress")
        if progress:
            out += ["", "### 📝 關鍵進度與目標"] + [f"{i}. {x}" for i, x in enumerate(progress, 1)]
        rows = data.get("results_and_errors") or []
        if isinstance(rows, list) and rows:
            out += ["", "### ❌ 執行結果與錯誤", "| 類別 | 內容描述 | 具體錯誤訊息/結果 |", "| :--- | :--- | :--- |"]
            for r in rows:
                if isinstance(r, dict):
                    cells = [str(r.get(k, "")) for k in ("category", "description", "detail")]
                else:
                    cells = ["", str(r), ""]
                cells = [c.replace("|", "\\|").replace("\n", " ").strip() for c in cells]
                out.append("| " + " | ".join(cells) + " |")
        prefs = items("user_preferences")
        if prefs:
            out += ["", "### 👤 使用者偏好與約束"] + [f"- {x}" for x in prefs]
        open_items = items("open_items")
        if open_items:
            out += ["", "### ⏭️ 未完成事項與下一步"] + [f"- {x}" for x in open_items]
        return "\n".join(out).strip()

    def summarize_messages(self, to_compress):
        """呼叫摘要模型，把「上一份滾動摘要 + to_compress」融合成新的一份摘要（Markdown）。
        不接觸 self.messages，可以在背景執行緒呼叫。回傳 (markdown, meta)。
        以 Ollama 的 format=JSON schema 強制結構化輸出；模型仍沒給合法 JSON 時退回原文，
        總比丟掉整段歷史好。"""
        system_prompt, user_prompt = self._summary_prompts(to_compress)
        res = ollama.chat(
            model=self.summary_model,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            format=SUMMARY_SCHEMA,
            options={'temperature': 0.2, 'num_ctx': NUM_CTX, 'num_predict': SUMMARY_MAX_PREDICT},
            think=False,
        )
        raw = res['message']['content'].strip()
        structured = True
        data = None
        try:
            data = json.loads(raw)
            markdown = self._render_summary_markdown(data)
        except (ValueError, TypeError):
            structured = False
            data = None
            markdown = raw
        get = getattr(res, "get", None)
        meta = {
            'structured': structured,
            'data': data,  # 排版前的 JSON，歸檔成 .json 供日後反思機制讀取
            'eval_count': get('eval_count') if get else None,
            'prompt_eval_count': get('prompt_eval_count') if get else None,
        }
        return markdown, meta

    def _archive_summary(self, markdown, n_messages, meta):
        """把新的融合摘要寫成 logs/summary_<ts>.md。這是歷史稽核用的存檔：system prompt
        只注入最新一份（rolling_summary），舊檔不再被載入。"""
        log_dir = os.path.join(self.script_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        timestamp = time.strftime('%Y-%m-%d_%H-%M-%S')
        file_path = os.path.join(log_dir, f"summary_{timestamp}.md")
        suffix = 1
        while os.path.exists(file_path):  # 同一秒內連續壓縮時不覆蓋
            file_path = os.path.join(log_dir, f"summary_{timestamp}_{suffix}.md")
            suffix += 1
        header = (
            f"# Summary at {timestamp}"
            f"（融合上一份摘要 + {n_messages} 則訊息；model {self.summary_model}；"
            f"structured={meta.get('structured')}）"
        )
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"{header}\n\n{markdown}\n")
        # 同名 .json：排版前的結構化資料 + 這次壓縮的統計，給日後的反思機制讀（比 parse Markdown 容易）
        try:
            with open(file_path[:-3] + ".json", "w", encoding="utf-8") as f:
                json.dump({
                    'timestamp': timestamp,
                    'model': self.summary_model,
                    'messages_compressed': n_messages,
                    'structured': bool(meta.get('structured')),
                    'prompt_eval_count': meta.get('prompt_eval_count'),
                    'eval_count': meta.get('eval_count'),
                    'summary': meta.get('data'),
                    'markdown': markdown,
                }, f, ensure_ascii=False, indent=2)
        except (OSError, TypeError, ValueError) as e:
            print(f"⚠️ 摘要 JSON 歸檔失敗（不影響主流程）：{e}")
        self._prune_summary_archive(log_dir)
        return file_path

    @staticmethod
    def _prune_summary_archive(log_dir, keep=None):
        """只保留最近 keep 份 summary_*.md（連同同名 .json），其餘刪除。"""
        keep = SUMMARY_ARCHIVE_KEEP if keep is None else keep
        if keep <= 0:
            return 0
        try:
            files = sorted(f for f in os.listdir(log_dir) if f.startswith("summary_") and f.endswith(".md"))
        except OSError:
            return 0
        removed = 0
        for name in files[:-keep] if len(files) > keep else []:
            for path in (os.path.join(log_dir, name), os.path.join(log_dir, name[:-3] + ".json")):
                try:
                    os.remove(path)
                    removed += 1
                except FileNotFoundError:
                    pass
                except OSError as e:
                    print(f"⚠️ 刪除舊摘要歸檔失敗：{path}：{e}")
        return removed

    def _load_latest_summary(self):
        """啟動時載入 logs/ 裡最新一份摘要的內文（去掉標頭行），作為跨 session 延續的滾動摘要。"""
        log_dir = os.path.join(self.script_dir, "logs")
        if not os.path.isdir(log_dir):
            return None
        files = sorted(f for f in os.listdir(log_dir) if f.startswith("summary_") and f.endswith(".md"))
        if not files:
            return None
        try:
            with open(os.path.join(log_dir, files[-1]), "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return None
        # 舊版檔案的內文開頭還會有模型自己寫的第二個 "# Summary at" 標頭，一起去掉
        while lines and (lines[0].startswith("# Summary at") or not lines[0].strip()):
            lines.pop(0)
        body = "\n".join(lines).strip()
        return body or None

    def _apply_compression(self, compressed, markdown, meta):
        """把摘要結果套用到主對話。以「物件身分」把 compressed 那幾則從 self.messages 原地移除
        （不重綁 list、不靠索引），所以背景壓縮期間主執行緒新 append 的訊息不會遺失；
        然後更新滾動摘要、歸檔、刷新 system prompt。"""
        file_path = self._archive_summary(markdown, len(compressed), meta)
        ids = {id(m) for m in compressed}
        with self.messages_lock:
            self.rolling_summary = markdown
            for i in range(len(self.messages) - 1, 0, -1):
                if id(self.messages[i]) in ids:
                    del self.messages[i]
            # 只刷新 system prompt 讓新摘要進入上下文；保留 to_keep、current_plan、current_task、
            # sticky_objective（更早的版本這裡誤呼叫 reset_conversation() 把它們全清掉）。
            if self.messages and self.messages[0]['role'] == 'system':
                self.messages[0] = {'role': 'system', 'content': self.get_system_prompt()}
            # Ollama 上次回報的精確 prompt 大小已失效，改用校準比估算直到下一次呼叫
            self.last_prompt_tokens = None
            self.last_prompt_chars = None
            self.last_compression = {
                'file': file_path,
                'messages': len(compressed),
                'summary_tokens': self.count_tokens(markdown),
                'structured': meta.get('structured'),
                'time': time.time(),
            }

    def compress_context_to_file(self, num_to_keep=None, keep_tokens=None):
        """同步壓縮（呼叫端等待）：保留區以外的舊訊息與上一份滾動摘要融合成新摘要、歸檔到 logs/，
        並用摘要取代那些訊息。low watermark 預設以 token 計（KEEP_RECENT_TOKENS，見
        _split_for_compression）；num_to_keep 是舊版「保留最新 N 則」的相容參數。
        若有背景壓縮正在進行會先等它完成再切分，不會同時跑兩份摘要。
        回傳 True 代表真的壓縮了；False 代表沒有可壓的內容。"""
        self.wait_for_background_compression()
        with self.messages_lock:
            to_compress, to_keep = self._split_for_compression(keep_tokens=keep_tokens, num_to_keep=num_to_keep)
        if not to_compress:
            return False
        print(f"\n⏳ 正在壓縮 {len(to_compress)} 則舊對話（保留最新 {len(to_keep)} 則原文）...")
        markdown, meta = self.summarize_messages(to_compress)
        self._apply_compression(to_compress, markdown, meta)
        print(f"💾 [系統] 歷史已壓縮並存入: {os.path.basename(self.last_compression['file'])}")
        return True

    def compressible_tokens(self, keep_tokens=None):
        """保留區以外可壓的舊訊息共多少 token（估算）。after_turn_compression 用它判斷值不值得壓。"""
        with self.messages_lock:
            to_compress, _ = self._split_for_compression(keep_tokens=keep_tokens)
        return sum(self.count_tokens(m['content']) for m in to_compress)

    def has_compressible_history(self, keep_tokens=None):
        """保留區以外是否還有可壓的舊訊息。上下文超過水位但全部訊息都在保留預算內時（例如
        system prompt 本身就很大），壓縮什麼都做不了，呼叫端可據此不必宣告「壓縮中」。"""
        with self.messages_lock:
            return bool(self._split_for_compression(keep_tokens=keep_tokens)[0])

    def compression_in_progress(self):
        t = self._compress_thread
        return bool(t and t.is_alive())

    def wait_for_background_compression(self, timeout=None):
        """若有背景壓縮進行中就等它結束。硬水位觸發同步壓縮前一定先呼叫，避免兩份摘要重疊。"""
        t = self._compress_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)

    def start_background_compression(self, keep_tokens=None):
        """/parallel_cal on：在背景執行緒做壓縮，主對話不等待。
        流程：快照要壓的訊息 → 呼叫摘要模型（不持有任何鎖）→ 完成後以物件身分原地替換。
        同一時間只允許一個背景壓縮；回傳 True 代表已啟動。
        注意：只有 Ollama 真的為這個模型配置 2 個以上 slot、或摘要模型與主模型不同（不同模型是
        不同 runner 程序，天然平行）時，摘要才會跟主對話同時推論。實測 Ollama 0.34 的新引擎對多模態
        模型（gemma4）強制單 slot，OLLAMA_NUM_PARALLEL=2 也一樣；此時同模型的背景摘要會讓下一次
        主對話在 Ollama 內排隊，等待只是搬到下一次呼叫（訊息不會遺失、通知照常到達）。要真的平行
        請用 AGENT_SUMMARY_MODEL 指定另一個模型。"""
        with self._compress_state_lock:
            if self.compression_in_progress():
                return False
            with self.messages_lock:
                to_compress, _ = self._split_for_compression(keep_tokens=keep_tokens)
            if not to_compress:
                return False

            def worker():
                try:
                    markdown, meta = self.summarize_messages(to_compress)
                    self._apply_compression(to_compress, markdown, meta)
                    info = self.last_compression
                    self._add_notice(
                        f"📦 [背景壓縮完成] 已將 {info['messages']} 則舊對話融合成摘要"
                        f"（約 {info['summary_tokens']} tokens）並歸檔：{os.path.basename(info['file'])}；"
                        f"目前上下文約 {self.context_tokens()} tokens。"
                    )
                except Exception as e:
                    self._add_notice(f"⚠️ [背景壓縮失敗] {e}；主對話未變動，超過硬水位時會改以同步方式壓縮。")

            self._compress_thread = threading.Thread(target=worker, name="context-compress", daemon=True)
            self._compress_thread.start()
            return True

    def _add_notice(self, text):
        with self._compress_state_lock:
            self._notices.append(text)

    def pop_notices(self):
        """取出並清空背景壓縮的通知（CLI 在下一次輸入後印出；Web Console 在下一個請求或狀態輪詢時推送）。"""
        with self._compress_state_lock:
            out, self._notices = self._notices, []
        return out

    def ensure_context_budget(self):
        """硬水位：上下文超過 TOKEN_THRESHOLD 就一定要在呼叫模型前壓下來，確保壓縮先於
        Ollama 於 num_ctx 處的靜默截斷。有背景壓縮進行中就先等它（不重複跑），等完仍超標才同步壓縮。
        回傳 True 代表這次確實有壓縮（同步、或剛等完的背景），供 UI 顯示。"""
        if self.context_tokens() <= TOKEN_THRESHOLD:
            return False
        waited = self.compression_in_progress()
        self.wait_for_background_compression()
        if self.context_tokens() <= TOKEN_THRESHOLD:
            return waited
        return self.compress_context_to_file()

    def _last_assistant_step(self):
        """最近一則 assistant 回覆解析成 {thought, reply, action}：這一步「為什麼執行、執行了什麼」。
        CLI／Web 都在 run_tool 之前就把該則回覆加進 self.messages，所以摘要時最後一則 assistant 就是下這個工具的那一輪。
        給獨立摘要 session 當第二層聚焦依據（第一層是使用者的目標，見 _build_task_anchor_text）；沒有就回 None。"""
        for m in reversed(self.messages):
            if m['role'] != 'assistant':
                continue
            parsed = parse_agent_reply(m['content'])
            if parsed["valid"]:
                return {"thought": parsed["thought"], "reply": parsed["reply"], "action": parsed["action"]}
            return {"thought": "", "reply": parsed["reply"], "action": None}
        return None

    @staticmethod
    def _clip_tool_output(result, max_chars=None):
        """原始輸出超過獨立 session 的輸入上限時保留頭尾（開頭多半是狀態列與統計、結尾是最後狀態），
        中間以說明行取代並告知省略了多少字元。回傳 (text, omitted_chars)。"""
        max_chars = TOOL_SUMMARY_INPUT_MAX_CHARS if max_chars is None else max_chars
        if len(result) <= max_chars:
            return result, 0
        head = int(max_chars * 0.6)
        tail = max_chars - head
        omitted = len(result) - head - tail
        return (f"{result[:head]}\n\n…（原始輸出過長，此處省略中間 {omitted} 字元；以下是結尾部分）…\n\n{result[-tail:]}", omitted)

    @staticmethod
    def _gap_reported(not_covered):
        """not_covered 欄位是否代表「真的有缺口」（而不是模型寫「無」「沒有」這類空值的各種說法）。
        _render_tool_summary 用它決定要不要印「未涵蓋」；summarize_tool_result 的自動追問迴圈用它決定要不要再跳一次。"""
        text = str(not_covered or "").strip()
        return bool(text) and text.rstrip("。.") not in ("無", "沒有", "none", "None", "N/A", "n/a")

    @staticmethod
    def _render_tool_summary(data, result_id=None):
        """把 TOOL_SUMMARY_SCHEMA 的 JSON 排成固定結構的純文字（回答／相關事實／錯誤／未涵蓋／延伸方向）。"""
        if not isinstance(data, dict):
            raise TypeError("tool summary JSON 不是物件")

        def items(key):
            v = data.get(key) or []
            if not isinstance(v, list):
                v = [v]
            return [str(x).strip() for x in v if str(x).strip()]

        lines = [f"回答：{str(data.get('answer') or '').strip() or '（摘要模型沒有給出回答）'}", "相關事實："]
        lines += [f"- {f}" for f in items("facts")] or ["- （原始輸出中沒有與任務直接相關的事實）"]
        errors = items("errors")
        if errors:
            lines.append("錯誤／異常：")
            lines += [f"- {e}" for e in errors]
        not_covered = str(data.get("not_covered") or "").strip()
        if SkillAgent._gap_reported(not_covered):
            lines.append(f"未涵蓋：{not_covered}")
        related = [r for r in (data.get("related_records") or []) if isinstance(r, dict) and r.get("id")]
        if related:
            lines.append("這個問題可能還需要之前的存檔：")
            lines += [f"- #{r['id']}：{str(r.get('reason') or '').strip() or '（未給理由）'}" for r in related]
        questions = [q for q in (data.get("suggested_questions") or []) if isinstance(q, dict) and str(q.get("question") or "").strip()][:3]
        if questions:
            where = f"（存檔 #{result_id} 原文裡有、上面沒寫）" if result_id else ""
            # 給決策 AI 自己參考的方向，不是要它照抄給使用者看：故意不用「可追問」這種標籤式字眼，並要求改寫成自然的
            # 一句話。keywords 只留給 harness 內部的自動追問用，不給 main session——實測小模型會把「a|b」改寫成
            # 「a OR b」又不加引號，result_grep 直接報錯；追問改由 result_recall 交給獨立 session 回原文提煉。
            lines.append(f"使用者接下來可能還想知道{where}（你自己判斷要不要主動問；要問就用一句自然的話問，"
                         f"不要條列、不要照抄下面的文字）：")
            lines += [f"- {str(q['question']).strip()}" for q in questions]
        return "\n".join(lines)

    def _extract_task_oriented(self, raw_text, purpose_text, tool_tokens, omitted=0, catalog=""):
        """任務導向擷取的核心呼叫（summarize_tool_result 的第一輪、自動追問的每一跳、_result_recall 共用）。
        raw_text 是要讀的原文，purpose_text 是錨點（可以是「使用者目標＋這一步的目的」，也可以是使用者的一句追問——
        呼叫端決定錨在什麼問題上，這裡不管錨點從哪來）。規則要求名稱與數值照抄、不推測、不給建議，並用 not_covered
        明說原始輸出沒有涵蓋什麼。catalog 是工具使用檢索清單（_tool_use_catalog）：獨立 session 看過完整原文後，
        順便判斷使用者的問題還需要清單裡哪幾筆（related_records），給 main session 當 recall 建議。
        結構以 format=TOOL_SUMMARY_SCHEMA 強制。回傳 (data, structured, raw_model_text)：JSON 解析失敗時
        data=None、structured=False，呼叫端可退回 raw_model_text（模型原文，總比丟掉整段輸出好）。"""
        system_prompt = f"""你是一個「資訊過濾器」：把一支工具（腳本）執行後的完整原始輸出，依 user 訊息開頭提供的使用者目標／問題擷取成精簡的重點，交給另一個負責決策的 AI。那個 AI 看不到原始輸出，只看得到你的擷取結果。

規則：
1. 只保留與使用者目標或問題直接相關的事實、數值、名稱、錯誤；樣板文字、排版、重複內容、與任務無關的欄位一律捨棄。
2. 名稱與數值一律照抄原文：topic／node／容器／檔案／路徑／站點／任務 id、數值與單位、錯誤訊息，都不要改寫、四捨五入或概括成「一些」「若干」。
3. 只陳述原始輸出裡有的內容，不要推測、補充背景或給建議；原始輸出沒有的就寫進 not_covered。
4. 使用者的目標／問題若在原始輸出裡找不到答案，answer 要直接寫「輸出中沒有…」，並在 not_covered 說明缺什麼。
5. 原始輸出若標示「省略中間 N 字元」，被省略的部分不可假設，要寫進 not_covered。
6. 數量、排序與門檻判斷以原始輸出裡腳本算好的結果為準（例如「共 N 個」「最新修改：」「條件 …：第一則符合 #k」），直接照抄、不要自己重數或推翻；原始輸出沒有算好而必須比較時，逐一核對原文並在 facts 引用依據（序號、原文數值或時間），無法確定就寫進 not_covered，不要猜。
7. 精簡：answer 一句話（含關鍵數值或名稱）；facts 每項一句、不超過 60 字；全部合計不超過 {TOOL_SUMMARY_MAX_CHARS} 字。
8. suggested_questions：使用者接下來可能想知道、但目標／問題沒問到、且原始輸出裡有資料可答的問題，最多 3 個；每個附 keywords＝在原始輸出裡搜得到的關鍵字（同義詞用 | 分隔，可含正則）。使用者的要求已經明確、輸出也已回答時給空陣列，不要硬湊。
9. index_hint：之後可能是完全不同的一次對話，要靠這一句話判斷「使用者那時問的事跟這份存檔有沒有關」。寫法「<使用者想知道什麼>：<這份原文是什麼、關鍵內容>」，50 字以內（一段英數路徑或檔名算 1 字），名稱照抄原文、放前面。例：「想找記憶相關檔案：skills_system/tools 清單，含 modify_memory.md、result_recall.md」「inference.py 在做什麼：原始碼，視覺推論主流程與 DEFAULT_MODEL 等設定」。不要用「此輸出」「檢視」「了解」開頭，不要重複 answer。
10. related_records：user 訊息若附了【過去的工具使用檢索清單】，判斷使用者現在的目標／問題是否「還需要」清單裡某筆的內容才能答完整（例如要跟之前查過的東西比較、問題提到之前查過的東西、這份輸出缺的正好是那筆有的），是就列出那筆的 id（清單上的數字）與一句理由，最多 2 筆；只是同一種工具或主題相近不算；沒有清單或都不需要就給空陣列（大多數情況是空陣列）。

輸出 JSON 物件：
- answer：一句話直接回答使用者的目標／問題。
- facts：與任務相關的事實清單（名稱、數值照抄）。
- errors：原始輸出中的錯誤／警告／異常，照抄原文；沒有就空陣列。
- not_covered：原始輸出沒有涵蓋、或因篇幅被省略而無法確認的部分；沒有就寫「無」。
- suggested_questions：[{{question, keywords}}] 可追問的問題與搜尋關鍵字，最多 3 個。
- index_hint：50 字以內，「<使用者想知道什麼>：<這份原文有什麼>」，供日後跨對話檢索使用。
- related_records：[{{id, reason}}] 使用者的問題還需要的舊存檔，最多 2 筆，通常是空陣列。
"""
        catalog_block = (
            f"【過去的工具使用檢索清單（只用來填 related_records；不是這份原始輸出的內容，不可寫進 answer／facts）】\n{catalog}\n\n"
            if catalog else ""
        )
        user_prompt = (
            f"{purpose_text}\n\n{catalog_block}"
            f"【工具的完整原始輸出（約 {tool_tokens} tokens{'，過長已保留頭尾' if omitted else ''}）】\n{raw_text}"
        )
        res = ollama.chat(
            model=self.summary_model,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            format=TOOL_SUMMARY_SCHEMA,
            options={'temperature': 0.2, 'num_ctx': NUM_CTX, 'num_predict': TOOL_SUMMARY_MAX_PREDICT},
            think=False,
        )
        raw = res['message']['content'].strip()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None, False, raw
        return (data, True, raw) if isinstance(data, dict) else (None, False, raw)

    def _extract_with_followups(self, clipped, purpose_text, tool_tokens, omitted, result_id, exclude_ids=()):
        """任務導向擷取＋有界的自動追問（summarize_tool_result 與 _result_recall 共用）：先擷取一次，若 not_covered
        還有缺口、且摘要自己給了 suggested_questions，就拿第一條的 keywords 對同一份存檔（result_id）再 result_grep
        一次（_exec_script 直接跑腳本，不經過 run_tool、不記軌跡、不產生新編號），把 grep 到的原文重新交給
        _extract_task_oriented——永遠錨回 purpose_text，不是拿上一輪的摘要文字當輸入。最多跳
        TOOL_SUMMARY_MAX_FOLLOWUP_HOPS 次；跳滿、grep 落空或沒有 keywords 可用時照實停下，not_covered 該是什麼
        就是什麼，不偽裝成已解決。每一次擷取都附工具使用檢索清單（排除 exclude_ids），最後的 related_records
        只留清單裡真的有的編號（_validate_related）。回傳 (data, structured, raw_model_text)。"""
        catalog, allowed = self._tool_use_catalog(exclude_ids)
        data, structured, raw_model_text = self._extract_task_oriented(clipped, purpose_text, tool_tokens, omitted, catalog)
        if not (structured and data is not None):
            return data, structured, raw_model_text
        self._validate_related(data, allowed)
        hops = 0
        while (
            result_id
            and not data.get("related_records")   # 缺的在別筆存檔：下一步是 recall 那筆，在這份裡 grep 只是白跑
            and self._gap_reported(data.get("not_covered"))
            and data.get("suggested_questions")
            and hops < TOOL_SUMMARY_MAX_FOLLOWUP_HOPS
        ):
            top_q = data["suggested_questions"][0] if isinstance(data["suggested_questions"][0], dict) else {}
            keywords = str(top_q.get("keywords") or "").strip()
            if not keywords:
                break
            grep_script = os.path.join(self.base_path, "scripts", "result_grep_cmd.py")
            grep_out = self._exec_script("result_grep_cmd.py", grep_script, [str(result_id), keywords, "--block"])
            # grep 落空就停：實測以前只擋 [ERROR]，「共 0 行命中」照樣拿去擷取，把第一輪列好的 26 個檔案換成「搜尋命中 0 行」
            if grep_out.lstrip().startswith("[ERROR]") or GREP_NO_HIT_RE.search(grep_out[:400]):
                break
            hops += 1
            print(f"🔁 [自動追問 {hops}/{TOOL_SUMMARY_MAX_FOLLOWUP_HOPS}] 關鍵字「{keywords}」對存檔 #{result_id} 再查一次…")
            grep_clipped, grep_omitted = self._clip_tool_output(grep_out)
            new_data, new_structured, _ = self._extract_task_oriented(
                grep_clipped, purpose_text, self.count_tokens(grep_out), grep_omitted, catalog)
            if not new_structured or new_data is None:
                break
            self._validate_related(new_data, allowed)
            data = self._merge_followup(data, new_data)
        return data, structured, raw_model_text

    @staticmethod
    def _merge_followup(base, hop):
        """自動追問一跳的結果併回第一輪：hop 只讀了 grep 到的片段，回答／未涵蓋／下一個方向用 hop 的（它針對缺口），
        事實、錯誤、related_records 取聯集（不然第一輪讀整份原文得到的事實會被片段蓋掉），index_hint 永遠用第一輪的
        （它描述的是整份存檔，不是 grep 片段）。"""
        merged = dict(hop)
        for key in ("facts", "errors"):
            merged[key] = list(dict.fromkeys(str(x) for x in (base.get(key) or []) + (hop.get(key) or [])))
        related, seen = [], set()
        for r in (base.get("related_records") or []) + (hop.get("related_records") or []):
            if r["id"] not in seen:
                seen.add(r["id"])
                related.append(r)
        merged["related_records"] = related[:2]
        merged["index_hint"] = base.get("index_hint") or hop.get("index_hint")
        return merged

    @staticmethod
    def _validate_related(data, allowed_ids):
        """related_records 只留檢索清單裡真的有的編號（小模型可能編出不存在的編號、或把這一份自己列進去），最多 2 筆；
        就地改寫 data["related_records"] 成 [{"id": int, "reason": str}]。"""
        out, seen = [], set()
        for r in data.get("related_records") or []:
            if not isinstance(r, dict):
                continue
            m = re.search(r"\d+", str(r.get("id") or ""))
            rid = int(m.group()) if m else None
            if rid is None or rid not in allowed_ids or rid in seen:
                continue
            seen.add(rid)
            out.append({"id": rid, "reason": " ".join(str(r.get("reason") or "").split())[:80]})
            if len(out) >= 2:
                break
        data["related_records"] = out
        return out

    def _followup_guidance(self, ids, data, structured, recalled=False):
        """接在任務導向擷取後面、給 main session 的下一步建議——獨立 session 看過完整原文與檢索清單後的判斷，
        依序：還需要之前的存檔 → 把「這一份＋那幾份」一起 recall（同一個獨立 session 才比得了）；有自動追問也解不開
        的缺口 → 自然地問使用者；已經回答 → 直接回答、不必再 recall。追問一律指向 result_recall（使用者的話交給
        獨立 session 回原文提煉），grep 只留給找字串。ids 是這次讀的存檔編號（摘要時只有一個；recall 可以有幾個）。"""
        ids = [str(i) for i in (ids if isinstance(ids, (list, tuple)) else [ids]) if i]
        if not ids:
            return "你看不到原始輸出：需要其他資訊時，換更精確的參數重新執行工具，不要憑空補上。"
        label = "、".join(f"#{i}" for i in ids)
        ok = structured and data is not None
        related = [str(r["id"]) for r in (data.get("related_records") or [])] if ok else []
        new = [r for r in related if r not in ids]
        has_gap = ok and self._gap_reported(data.get("not_covered"))
        has_questions = ok and bool(data.get("suggested_questions"))
        if recalled:
            base = f"不要再對 {label} 重複 recall 或 grep。"
        else:
            base = (f"完整原始輸出已存成 #{ids[0]}：之後使用者追問這份輸出裡上面沒寫到的內容時，執行 "
                    f"result_recall {ids[0]} \"<使用者的話>\" 回原文提煉（只有要找某個字串出現在哪幾行才用 result_grep），"
                    f"不要重新執行同一個工具。")
        if new and len(ids) < RECALL_MAX_RECORDS:
            combo = ",".join((ids + new)[:RECALL_MAX_RECORDS])
            return base + (f"獨立 session 判斷這個問題還需要之前的存檔 {'、'.join('#' + r for r in new)} 一起看才答得完整："
                           f"下一輪直接執行 result_recall {combo} \"<使用者的話>\"（逗號隔開的幾份原文會交給同一個獨立 session "
                           f"一起提煉），拿到後再回答，不要先問使用者、也不要重跑當時的工具。")
        if has_gap and has_questions:
            # 這裡的「未涵蓋」是自動追問已經跳過 TOOL_SUMMARY_MAX_FOLLOWUP_HOPS 次仍解不開的缺口，
            # harness 已經盡力，這時交回使用者選方向比主模型自己硬猜更可靠。
            return base + ("上面的缺口 harness 已經自動再查過還是沒解開：下一輪用 reply 自然地問使用者一句話（不要條列、不要照抄），"
                           "不要自己選一個方向去執行，除非使用者這句話已經對應到其中一個方向。")
        return base + ("上面已經回答了使用者的問題：直接據此回答或做任務的下一步，不需要再 recall；"
                       "後面列的方向只是額外可能有興趣的，順口提一句或略過都可以。")

    def summarize_tool_result(self, result, tool_tokens, step=None, result_id=None):
        """任務導向摘要（Task-Oriented Summarization）：開一個獨立、乾淨的一次性 session（自己的 system/user
        prompt，不接觸 self.messages），把超過門檻的工具回傳擷取成主對話用得上的重點，摘要完就丟棄。

        跟通用摘要的差別在「帶著問題讀原文」——獨立 session 同時收到兩層聚焦依據：
        1. 使用者的目標（_build_task_anchor_text：Objective、使用者最近 3 句原話（任務線）、已核准的計畫）；
        2. 這一步的目的（_last_assistant_step：決策 AI 剛才的 thought／reply 與執行的 action）。
        規則要求名稱與數值照抄、不推測、不給建議，並用 not_covered 明說原始輸出沒有涵蓋什麼，主模型才知道
        該換參數重查而不是憑空補上。結構以 format=TOOL_SUMMARY_SCHEMA 強制，解析失敗退回模型原文。
        原始輸出超過 TOOL_SUMMARY_INPUT_MAX_CHARS 時只讀頭尾（_clip_tool_output），並在給主模型的附註標明。
        step 可由呼叫端指定（測試用），預設取最近一則 assistant 回覆。
        result_id 預設取最近一次 run_tool 的存檔編號，並把 answer 回填到 index.md。獨立 session 同時拿到工具使用
        檢索清單，順便判斷使用者的問題還需要清單裡哪幾筆（related_records）；結尾的下一步建議（_followup_guidance）
        依序是：需要舊存檔 → 先 result_recall 那筆；自動追問也解不開的缺口 → 用自然的一句話問使用者；已回答 →
        直接回答不必 recall；之後的追問一律 result_recall <編號> "<使用者的話>"（延伸方向不再附 grep 關鍵字給主模型）。
        同時把 index_hint（使用者問題 x 這份輸出的關聯，50 字以內）連同檔名記進 tools_use_index.md（見
        _append_tool_use_index；result_* 這類看舊存檔的衍生輸出不記）——這份跟 index.md 不同，永久累加、跨 session
        存活，讓再久以前的工具回傳也有機會被日後的 main session 從 system prompt 尾端的檢索清單裡認出來。

        自動追問（有界、確定性；_extract_with_followups，與 _result_recall 共用）：第一輪擷取後若 not_covered 還有缺口、且摘要自己給了 suggested_questions，
        直接拿第一條的 keywords 對同一份存檔再跑一次 result_grep（_exec_script，不經過 run_tool，不記軌跡、
        不產生新編號——這不是模型的動作，是同一次工具回傳的延伸擷取），把 grep 到的原文重新交給
        _extract_task_oriented（永遠錨回 purpose_text，不是拿上一輪的摘要文字當輸入，避免摘要疊摘要）。
        最多跳 TOOL_SUMMARY_MAX_FOLLOWUP_HOPS 次；跳滿、grep 落空或沒有 keywords 可用時照實停下，
        not_covered 該是什麼就是什麼，不偽裝成已經解決——使用者原始問題若本來就模糊，這裡不會硬鎖定一個
        可能不相關的答案，而是誠實地把缺口留給主模型，逼出使用者下一句話當新錨點。"""
        anchor = self._build_task_anchor_text()
        result_id = self.last_result_id if result_id is None else result_id
        step = step if step is not None else (self._last_assistant_step() or {})
        step_lines = []
        if step.get("action"):
            step_lines.append(f"執行的工具：{action_text(step['action'])}")
        if step.get("thought"):
            step_lines.append(f"決策 AI 執行前的想法：{step['thought']}")
        if step.get("reply"):
            step_lines.append(f"決策 AI 對使用者的說明：{step['reply']}")
        step_text = "\n".join(step_lines) or "（沒有取得這一步的說明，請以使用者的目標為依據）"
        purpose_text = f"【使用者的目標】\n{anchor}\n\n【這一步的目的（決策 AI 為什麼執行這個工具）】\n{step_text}"
        clipped, omitted = self._clip_tool_output(result)

        data, structured, raw_model_text = self._extract_with_followups(
            clipped, purpose_text, tool_tokens, omitted, result_id, exclude_ids=(result_id,))

        answer = ""
        if structured and data is not None:
            body = self._render_tool_summary(data, result_id)
            answer = str(data.get("answer") or "").strip()
        else:
            body = raw_model_text
        self.last_tool_summary = {
            'structured': structured, 'input_tokens': tool_tokens, 'omitted_chars': omitted,
            'summary_tokens': self.count_tokens(body), 'model': self.summary_model, 'result_id': result_id,
        }
        if result_id and answer:
            self.update_result_answer(result_id, answer)
        # 檢索清單只收「原始工具」的結果：result_grep／result_view／result_list／result_recall 是看舊存檔的衍生輸出，
        # 記進去會跟原本那筆重複佔位，而且之後 recall 到它只拿得到部分內容（摘要的摘要），不如 recall 原本那筆。
        script = next((r.get("script") for r in reversed(self.trajectory) if r.get("id") == result_id), None)
        if structured and data is not None and script not in DERIVED_RESULT_SCRIPTS:
            self._append_tool_use_index(result_id, self.last_result_file, data.get("index_hint"))
        status = "失敗（[ERROR]）" if result.lstrip().startswith("[ERROR]") else "成功"
        recall = self._followup_guidance([result_id] if result_id else [], data, structured)
        note = (
            f"（原始輸出約 {tool_tokens} tokens，超過門檻 {TOOL_RESULT_TOKEN_THRESHOLD}；以上由獨立 session 依使用者目標與"
            f"這一步的目的從完整輸出擷取{'，過長部分只讀了頭尾' if omitted else ''}，完整內容已顯示給使用者。{recall}）"
        )
        return f"{TOOL_SUMMARY_TAG}\n執行結果：{status}\n{body}\n{note}"

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

    def _tool_use_index_block(self):
        """system prompt 尾端的『工具使用檢索清單』：讀 tools_use_index.md 最近 TOOL_USE_INDEX_SHOW 行組成。
        這份檔案只有觸發過任務導向擷取（summarize_tool_result／_result_recall）的回傳才有一筆，不是每次工具
        呼叫都有；檔案本身不裁剪、跨 session（甚至跨這支程式的重新啟動）持續累加，這裡只裁「顯示視窗」——
        目的是讓再久以前的工具回傳，只要現在的問題看起來有關，都有機會被發現，而不必模型自己記得存檔編號。
        跟 _build_plan_context_prompt／_build_objective_prompt 同一種模式：每次組 system prompt 都重新讀檔案、
        重新塞入，不會被滑動視窗或壓縮摘要沖掉。TOOL_USE_INDEX_SHOW<=0 時關閉（環境變數可調／可關）。"""
        rows = self._tool_use_index_rows()
        if not rows:
            return ""
        rows_text = "\n".join(f"- #{rid}（{ts}）：{hint}" for rid, ts, hint in rows)
        return f"""
            ## 工具使用檢索清單（Tool Use Index）
            以下每筆是過去某次工具回傳的存檔（可能是很早之前、甚至上次啟動的）跟當時問題的關聯描述，最上面最新；只是線索，原文不在這裡：
            {rows_text}

            每次回答前依序判斷:
            1. 對話裡最近的工具回傳已經寫了使用者要的那個具體名稱或數值（不是概括描述）→ 直接回答，不用 recall
            2. 使用者這句話在接續、追問或延伸其中一筆（「剛剛那些腳本」「之前查的那個馬達」；「那個」「隨便選一個」這類指涉
               在對話裡找不到對象時，多半指最上面那筆）→ 直接執行 EXECUTE: scripts/result_recall_cmd.py <編號> "<使用者這句話的原文>"
               （編號＝上面的 #數字，要一起看好幾筆時用逗號隔開；第二個參數抄使用者說的話、不是清單上的描述；
               不用先載入規格、不要先 result_list 或 result_grep），
               系統把那份存檔的完整原文連同使用者的問題交給獨立 session 提煉後回給你，再據此回答或做下一步——不要憑印象回答
            3. 問的是「現在」「目前」「最新」的狀態 → 舊存檔只是參考，重新執行當時的工具查最新狀態
            4. 都不像 → 當作新問題，不要牽強附會，也不要反問使用者「你指的是哪一筆」
            result_recall 回報「找不到結果」＝原始檔已超過保留上限被清掉：誠實告訴使用者這筆資料已經不在，不要用猜的。
            """

    def _tool_use_index_rows(self, exclude=()):
        """tools_use_index.md 最近 TOOL_USE_INDEX_SHOW 筆（新→舊）→ [(編號, 時間, 描述)]，同一編號只留最新一筆。
        system prompt 尾端的檢索清單（_tool_use_index_block）與交給獨立 session 判斷 related_records 的清單
        （_tool_use_catalog）共用；exclude 排除特定編號（例如正在被摘要或被 recall 的那一筆自己）。"""
        if TOOL_USE_INDEX_SHOW <= 0:
            return []
        try:
            with open(os.path.join(self.results_dir(), TOOL_USE_INDEX_NAME), encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            return []
        excluded = {str(x).lstrip("#") for x in exclude if x}
        rows, seen = [], set()
        for ln in reversed(lines):
            parts = ln.split(" | ", 4)
            rid = parts[0].strip().lstrip("#") if len(parts) >= 5 else ""
            if not rid.isdigit() or rid in seen or rid in excluded:
                continue
            seen.add(rid)
            rows.append((int(rid), parts[1].strip(), parts[4].strip()))
            if len(rows) >= TOOL_USE_INDEX_SHOW:
                break
        return rows

    def _tool_use_catalog(self, exclude=()):
        """交給獨立 session 的檢索清單文字與可接受的編號集合（_validate_related 用）；清單空時回 ("", set())。"""
        rows = self._tool_use_index_rows(exclude)
        return "\n".join(f"#{rid}（{ts}）：{hint}" for rid, ts, hint in rows), {rid for rid, _, _ in rows}

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

    def set_current_task(self, text):
        """使用者送出新任務（或回答追問）時呼叫：current_task 是最新一句，task_history 保留最近幾句當任務線。"""
        self.current_task = text
        if text:
            self.task_history = (self.task_history + [text])[-TASK_HISTORY_KEEP:]

    def reset_conversation(self):
        self.current_plan = None  # /clear 時一併清掉進行中的計畫，避免舊計畫殘留誤導新任務
        self.current_task = None  # 同上，避免舊任務敘述殘留誤導下一次的摘要 session
        self.task_history = []
        self.add_trajectory_boundary("clear")  # 軌跡本身保留（/trajectory 仍看得到），只記一個起點
        # 原地替換而不重綁 list：背景壓縮執行緒若正在對舊 list 做原地刪除，不會操作到已被丟棄的物件
        with self.messages_lock:
            self.messages[:] = [{'role': 'system', 'content': self.get_system_prompt()}]

    def _truncate_memory(self):
        """訊息「則數」滑動視窗，預設停用（max_history=None）。啟用時超過則數會直接丟棄最舊訊息、
        不摘要不歸檔，與 token 觸發的 compress_context_to_file 是不同邏輯，只當保險絲用。"""
        if self.max_history and len(self.messages) > self.max_history + 1:
            print(f"⚠️  [記憶優化] 啟動滑動視窗（保留 {self.max_history} 筆）")
            with self.messages_lock:
                self.messages[:] = [self.messages[0]] + self.messages[-self.max_history:]

    def ask_ai(self):
        try:
            with self.messages_lock:
                if self.messages and self.messages[0]['role'] == 'system':
                    self.messages[0]['content'] = self.get_system_prompt()

            self._truncate_memory()

            # 硬水位：呼叫模型前的最後一道檢查（唯一的硬水位檢查點）。使用者貼了一大段文字或工具
            # 回傳剛加入之後就直接送模型，這裡確保壓縮一定發生在 Ollama 於 num_ctx 處靜默截斷之前。
            # 軟水位（回合結束後順手壓縮）在 after_turn_compression。
            self.auto_compressed = self.ensure_context_budget()

            # 送出的是快照：背景壓縮執行緒可能在這次呼叫期間原地修改 self.messages，
            # 校準（_record_usage）也必須以真正送出的這份為準。
            with self.messages_lock:
                snapshot = list(self.messages)

            self.last_reply_retry = None
            raw_content, eval_tokens = self._chat_once(snapshot)

            # 空白回覆的自動重試：合法 JSON、reply 是空字串、action 是 null。實測成因是 thought／reply 的文字裡
            # 出現英文雙引號（例如想寫 關鍵字是 "scheduler"），在 JSON 字串裡它就是「字串結束」，文法接著只允許
            # , "reply":，模型被迫給空字串與 null，思考看起來像被截斷。這種回覆對使用者沒有任何用處，
            # 重試一次並附上暫時的提醒（只放進這次送出的快照，不進 self.messages，歷史裡不會留下壞範例）。
            parsed = parse_agent_reply(raw_content)
            if parsed["valid"] and not parsed["reply"] and not parsed["action"]:
                tail = parsed["thought"][-30:].replace("\n", " ")
                nudge = {'role': 'user', 'content': (
                    "[harness] 你剛才的回覆 reply 是空字串、action 是 null"
                    + (f"，thought 在「{tail}」處中斷" if tail else "")
                    + "——很可能是文字裡的英文雙引號 \" 提前結束了 JSON 字串。請針對同一個問題重新回覆："
                    "thought 一到兩句、不要使用英文雙引號（引用名稱用「」或反引號），reply 必須有內容；"
                    "若需要執行技能就填 action。"
                )}
                raw_retry, eval_retry = self._chat_once(snapshot + [nudge])
                parsed_retry = parse_agent_reply(raw_retry)
                self.last_eval_tokens = (eval_tokens or 0) + (eval_retry or 0)  # 兩次呼叫的產出都算這一輪的 AI tokens
                still_blank = parsed_retry["valid"] and not parsed_retry["reply"] and not parsed_retry["action"]
                self.last_reply_retry = (
                    "⚠️ 模型第一次回覆為空白（thought 疑似被英文雙引號提前截斷），已自動重試一次"
                    + ("，重試後仍為空白。" if still_blank else "。")
                )
                raw_content = raw_retry

            return raw_content

        except Exception as e:
            return f"Ollama 連線錯誤: {e}"

    def _chat_once(self, snapshot):
        """呼叫一次主模型，回傳 (回覆原文, 這次產出的 token 數)。
        關閉 Ollama 的獨立 thinking 模式：此版本 Ollama 會把推理過程放進 message.thinking 欄位，若不關閉，
        模型有時會把整個決策都留在 thinking 裡，導致 content 回傳空字串。
        num_ctx：若不指定，Ollama 會用內建預設值（4096）而非模型實際支援的上限，對話還沒到我們的門檻
        Ollama 就已經在背後截斷最舊的內容；這裡統一用 NUM_CTX（各水位與它同一尺度）。"""
        response = ollama.chat(
            model=self.model,
            messages=snapshot,
            format=AGENT_REPLY_SCHEMA,  # 回覆協議：{thought, reply, action}，見檔尾 AGENT_REPLY_SCHEMA 說明
            options={'temperature': 0.2, 'num_ctx': NUM_CTX},
            think=False
        )
        self._record_usage(response, snapshot)
        raw_content = response['message']['content'].strip()
        if "<thought>" in raw_content:
            raw_content = raw_content.split("</thought>")[-1].strip()
        elif "...done thinking." in raw_content:
            raw_content = raw_content.split("...done thinking.")[-1].strip()
        return raw_content, self.last_eval_tokens

    def parse_reply(self, raw):
        """ask_ai() 的原始回覆 → {"thought", "reply", "action", "valid"}（見 parse_agent_reply）。"""
        return parse_agent_reply(raw)

    @staticmethod
    def format_reply_for_console(parsed):
        """CLI 顯示：思考（有才印）、給使用者的文字、以及這輪要執行的 action（有才印）。"""
        lines = []
        if parsed["thought"]:
            lines.append(f"💭 {parsed['thought']}")
        lines.append(parsed["reply"] or ("（本輪沒有文字回覆）" if parsed["action"] else "（空白回覆）"))
        if parsed["action"]:
            lines.append(f"▶ action: {action_text(parsed['action'])}")
        if not parsed["valid"]:
            lines.append("⚠️ 模型輸出不是合法的 JSON 回覆，已降級為純文字顯示，本輪不執行任何指令。")
        return "\n".join(lines)

    def _skill_memory_entries(self, skill_name):
        """技能綁定的經驗記憶條目（skills_system/memory/<skill>.md 裡以「- [」開頭的行）。
        沒有檔案或沒有條目時回傳空 list。"""
        path = os.path.join(self.skill_memory_dir, f"{skill_name}.md")
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return [line.rstrip() for line in f if line.lstrip().startswith("- [")]
        except OSError:
            return []

    def list_skills(self):
        """解析 SKILLS.md 索引：每個技能的名稱、一行描述、所屬分類（## 標題），以及是否有綁定的經驗記憶。
        只列出 tools/<name>.md 真的存在的技能。Web Console 的「/」選單與 CLI 的 /skills 都用這個。"""
        skills = []
        category = "其他"
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return skills
        for line in lines:
            if line.startswith("## "):
                category = line[3:].strip()
                continue
            if not line.startswith("- ["):
                continue
            name = line[3:line.index("]")] if "]" in line else ""
            if not name or not os.path.exists(os.path.join(self.tools_dir, f"{name}.md")):
                continue
            desc = line.split("—", 1)[1].strip() if "—" in line else ""
            skills.append({
                "name": name,
                "description": desc,
                "category": category,
                "has_memory": bool(self._skill_memory_entries(name)),
            })
        return skills

    def manual_skill_block(self, skill_name):
        """使用者手動按需載入技能規格時，要附在下一則使用者訊息後面的區塊（含該技能的經驗記憶）。
        內容等同 AI 自己 EXECUTE 技能名稱後系統回傳的規格，讓 AI 可以直接依其中的腳本路徑執行、
        省掉一輪「先載規格」。技能不存在回傳 None。"""
        doc = self._load_skill_doc(skill_name)
        if doc is None:
            return None
        name = skill_name[:-3] if skill_name.endswith(".md") else skill_name
        return (
            f"{SKILL_LOADED_MARKER}\n"
            f"{SKILL_DOC_PREFIX} '{name}' 的規格文件（使用者從選單手動載入，等同你以 action.command 填技能名稱後"
            f"系統回傳的規格；依此內容才可執行：action.command 填其中標明的實際腳本路徑、args 填參數）：\n{doc.rstrip()}"
        )

    def _load_skill_doc(self, skill_name):
        """若 skill_name 對應到 tools/<skill_name>.md，回傳其內容；否則回傳 None。
        容忍 AI 直接照抄索引連結而帶上 .md 後綴（例如 list_dir.md）。
        若該技能有綁定的經驗記憶（memory/<skill_name>.md），附在規格文件後面一起回傳：
        這些是使用者要求記住、專屬於此技能的修正，只在真的用到此技能時才進入上下文。"""
        if skill_name.endswith(".md"):
            skill_name = skill_name[:-3]
        doc_path = os.path.join(self.tools_dir, f"{skill_name}.md")
        if not os.path.exists(doc_path):
            return None
        with open(doc_path, "r", encoding="utf-8") as f:
            doc = f.read()
        entries = self._skill_memory_entries(skill_name)
        if not entries:
            return doc
        memory_block = (
            f"# 經驗記憶（使用者曾要求記住、專屬於 {skill_name} 的 {len(entries)} 則經驗，使用此技能時必須遵守）\n"
            + "\n".join(entries)
        )
        return f"{doc.rstrip()}\n\n{memory_block}\n"

    def _normalize_script_name(self, raw_token):
        """確保腳本檔名以 _cmd.py 結尾（例如 cd -> cd_cmd.py，find_file.py -> find_file_cmd.py）。"""
        if not raw_token.endswith("_cmd.py") and not raw_token.endswith(".py"):
            return f"{raw_token}_cmd.py"
        if raw_token.endswith(".py") and not raw_token.endswith("_cmd.py"):
            return raw_token.replace(".py", "_cmd.py")
        return raw_token

    def _parse_script_args(self, script_name, remainder):
        """把 action.args 字串以 shlex 依空白／引號拆成參數列表（含空白的參數用雙引號包住）。
        空字串參數（模型常寫成 `. ""`）一律丟掉：沒有任何腳本需要空的位置參數，留著只會變成 [ERROR] 找不到路徑。"""
        try:
            parts = shlex.split(remainder)
        except Exception:   # 引號不成對（實測模型會送 """）：退回空白切分並去掉殘留的引號
            parts = [a.strip("\"'") for a in remainder.split()]
        return [a for a in parts if a != ""]

    def _sync_state_from_tool_output(self, output_text):
        """解析工具輸出中的狀態標記，同步更新目前的工作目錄／容器目錄。"""
        lines = output_text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("[CWD_CHANGED]"):
                self.current_cwd = line.replace("[CWD_CHANGED]", "").strip()
            # 抓取 [CONTAINER_CWD] 標記的下一行作為路徑
            if line.startswith("[CONTAINER_CWD]") and i + 1 < len(lines):
                self.container_cwd = lines[i + 1].strip()
            # 目標容器：容器技能成功操作某容器後印在末行（見 _docker_common.with_target_marker）
            if line.startswith("[TARGET_CONTAINER]"):
                self.target_container = line[len("[TARGET_CONTAINER]"):].strip()

    def run_tool(self, ai_response):
        """依 AI 回覆 JSON 的 action 欄位執行（ai_response 可以是原始 JSON 字串或 parse_reply 的結果）。
        action 為 null／回覆不是合法 JSON → 不執行、回傳 None；reply 裡的文字完全不看。
        有 action 時行為分兩種：

        1. 若 command 對應到 tools/<name>.md 的技能規格文件（代表 AI 用的是
           SKILLS.md 索引裡的技能名稱），直接把規格文件內容當作系統回傳注入
           上下文，不執行任何腳本——這就是按需載入 (Progressive Disclosure)。
           不做技能名稱 -> 腳本檔名的猜測或對照；AI 讀完規格後，下一輪需改用
           規格書中標明的實際腳本路徑（例如 scripts/cd_cmd.py）才會真正執行。
        2. 否則將 command 視為實際腳本路徑，以 args 為參數執行對應的 CLI 腳本並回傳結果。
        """
        parsed = ai_response if isinstance(ai_response, dict) else parse_agent_reply(ai_response)
        action = parsed.get("action")
        self.last_result_id = self.last_result_file = None   # 只有真的執行腳本（_record_trajectory）才會有存檔
        if not action:
            return None

        try:
            payload = action_text(action)
            parts = payload.split(maxsplit=1)
            raw_token = os.path.basename(parts[0])
            remainder = parts[1] if len(parts) > 1 else ""

            skill_doc = self._load_skill_doc(raw_token)
            if skill_doc is not None:
                n_memory = len(self._skill_memory_entries(raw_token[:-3] if raw_token.endswith(".md") else raw_token))
                memory_note = f"，附 {n_memory} 則技能經驗記憶" if n_memory else ""
                print(f"📖 Agent 選擇技能索引: {raw_token}（載入規格文件{memory_note}，尚未執行）")
                # 回給模型的這段話是兩階段流程裡最關鍵的一句：實測 JSON 協議下小模型載完規格容易停下來解釋規格或
                # 反問使用者（reply 欄位本身就在邀請它聊天），所以這裡直接下達下一輪該做的事。帶了參數時再明說那些參數
                # 沒有被執行——實測模型用「docker_open <容器>」載完規格就當作容器已切換，直接做下一步。
                # 實測（2026-09-25 情境稽核）：只寫「繼續使用者原本的任務」時，模型會跳過這一步直接做下一步（載完 change_dir
                # 規格就去 ls），所以要明說「下一輪先把這一個技能真正執行一次，之後才做下一步」。
                if remainder.strip():
                    next_step = (f"你附的參數「{remainder.strip()}」也沒有被執行、狀態沒有改變。下一輪請先把這個技能真正執行一次："
                                 f"action.command 填規格標明的實際腳本路徑（scripts/...）、args 填同樣的參數，執行完看到結果後才做下一步；")
                else:
                    next_step = ("下一輪請先把這個技能真正執行一次——action.command 填規格標明的實際腳本路徑（scripts/...）、"
                                 "args 填參數，執行完看到結果後才做使用者任務的下一步；")
                return (f"{SKILL_DOC_PREFIX} '{raw_token}' 的規格文件（由系統提供）。這一輪只是載入規格、還沒有執行任何東西：{next_step}"
                        f"不要向使用者解釋規格內容，也不要詢問是否要執行。\n{skill_doc}")

            script_name = self._normalize_script_name(raw_token)
            if script_name == "result_recall_cmd.py":
                # 偽技能：result_recall 沒有實體 scripts/result_recall_cmd.py（要呼叫 ollama.chat，只能在本
                # process 內做，見 README「Agent_Runner.py 是唯一的核心來源」），必須在存在性檢查前攔截，
                # 否則會落入下面「找不到腳本」的分支。規格文件走一般的兩階段揭露，不受影響。
                return self._result_recall(remainder)
            script_path = os.path.join(self.base_path, "scripts", script_name)

            print(f"🛠️  Agent 啟動工具: {script_name}")
            if not os.path.exists(script_path):
                # 幾乎都是模型跳過「先載入規格」直接猜腳本檔名（例如 list_dir_cmd.py，實際是 ls_cmd.py）。
                # 給可直接行動的指引：猜的名稱裡若含有某個技能名稱，就建議先 EXECUTE 那個技能取得規格。
                stem = script_name[:-len("_cmd.py")] if script_name.endswith("_cmd.py") else script_name
                candidates = [
                    f[:-3] for f in sorted(os.listdir(self.tools_dir))
                    if f.endswith(".md") and (f[:-3] in stem or stem in f[:-3])
                ] if os.path.isdir(self.tools_dir) else []
                hint = (
                    f"這個名稱看起來是技能 {', '.join(candidates)}，請先把 action.command 填成 `{candidates[0]}`（args 留空）"
                    f"載入規格文件，再依規格標明的實際腳本路徑執行。"
                    if candidates else
                    "腳本路徑只能從規格文件取得，不可自行推測：請先以 action.command 填入 SKILLS.md 裡的技能名稱載入規格。"
                )
                return f"[ERROR] 找不到腳本 {script_name}。{hint}"

            clean_args = self._parse_script_args(script_name, remainder)
            # 軌跡記錄用：執行「前」的狀態
            cwd_before, container_before, target_before = self.current_cwd, self.container_cwd, self.target_container
            output_text = self._exec_script(script_name, script_path, clean_args)
            self._sync_state_from_tool_output(output_text)  # 逾時訊息裡不會有狀態標記，掃過去是安全的 no-op
            self._record_trajectory(script_name, clean_args, output_text, cwd_before, container_before, target_before)
            return output_text

        except Exception as e:
            return f"解析指令失敗: {e}"

    def _exec_script(self, script_name, script_path, clean_args):
        """執行一支腳本、回傳 output_text；不記軌跡、不同步狀態（呼叫端負責）——run_tool() 的正常派發，
        與 summarize_tool_result() 的自動追問（直接跑 result_grep_cmd.py，跳過整個 run_tool，不印
        「Agent 啟動工具」這種暗示模型主動執行的訊息）共用同一個執行原語。"""
        env = os.environ.copy()
        env["CONTAINER_CWD"] = self.container_cwd
        env["TARGET_CONTAINER"] = self.target_container  # 容器技能省略容器名稱時的預設（比照 cwd）
        env["TOOL_RESULTS_DIR"] = self.results_dir()     # result_list／result_grep／result_view 的存檔目錄
        env["HARNESS_SESSION"] = self.session_id         # 讓 result_grep 16 優先解析成目前 session 的 #16
        try:
            res = subprocess.run(
                [sys.executable, script_path] + clean_args,
                capture_output=True,
                text=True,
                cwd=self.current_cwd,
                env=env,
                timeout=TOOL_EXEC_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return (
                f"[ERROR] 工具 {script_name} 執行逾時（超過 {TOOL_EXEC_TIMEOUT} 秒），已被系統強制終止。"
                f"這是 harness 的最後防線，各腳本自身應有更短的逾時；若經常觸發請檢查該腳本。"
            )
        if res.returncode == 0:
            return res.stdout.strip()
        # 腳本異常結束：優先用 stderr，沒有就用 stdout；兩者皆空也要給 AI 一個
        # 明確的失敗訊息，否則空字串會被誤判成「沒有工具需要執行」。
        # 統一補上 [ERROR] 前綴，讓 _content_for_context 能正確判定為失敗。
        output_text = res.stderr.strip() or res.stdout.strip() or "（沒有任何輸出）"
        if not output_text.startswith("[ERROR]"):
            output_text = f"[ERROR] 腳本 {script_name} 異常結束（exit code {res.returncode}）:\n{output_text}"
        return output_text

    def _result_recall(self, remainder):
        """result_recall 偽技能——追問任何存檔內容的主要路徑（檢索清單裡的舊紀錄、或剛剛摘要沒寫到的細節）：main
        session 帶編號＋使用者的問題原文執行；這裡把那份存檔的完整原文重新讀出來，交給獨立 session 以使用者的問題為
        錨點提煉（_extract_with_followups：同一套任務導向擷取＋有界自動追問，逐步收斂；也附檢索清單，讓它判斷還需要
        哪一筆），main session 只收到提煉後的重點與下一步建議。跟 result_grep 的差別是語意由獨立 session 理解，不是
        main session 自己想關鍵字比對字面。錨點用任務線（_build_task_anchor_text：最新一句＋前幾句）——實測使用者
        回答追問時最新一句常只剩「第一個」，模型給的參數只當聚焦點。
        可以一次給多個編號（1,2，最多 RECALL_MAX_RECORDS 份）：問題要一起看幾份存檔時（例如比較 tools 與 scripts 的
        清單），這幾份原文交給同一個獨立 session。實測只能 recall 一份時會來回跳——讀 #1 的 session 看不到 #2，說「還
        需要 #2」；讀 #2 的又說「還需要 #1」，比較永遠做不成。多份時不做自動追問（原文已經都在），正在讀的這幾份也從
        檢索清單排除，不會再被列成「還需要」。這是 main session 主動選的一次動作，照樣記軌跡、產生新的存檔編號；但不記進
        tools_use_index.md（它是被取回那幾筆的衍生輸出，清單留原本的就好）。"""
        try:
            parts = shlex.split(remainder) if remainder.strip() else []
        except ValueError:
            parts = remainder.split()
        if not parts:
            return '[ERROR] 需要指定存檔編號與問題：result_recall <編號>[,<編號>] "<使用者的問題原文>"。'
        ref_arg, question = parts[0], " ".join(parts[1:]).strip()
        if not question:
            return '[ERROR] 需要問題內容：result_recall <編號>[,<編號>] "<使用者的問題原文>"。'

        loaded, missing = [], []
        for ref in [r.strip() for r in re.split(r"[,，、]+", ref_arg) if r.strip()]:
            path, err = _results_common.resolve_result(ref, self.results_dir())
            if err:
                missing.append(ref.lstrip("#"))
                continue
            meta, body_start = _results_common.read_header(path)
            rid = str(meta.get("id") or ref.lstrip("#"))
            if rid in {x[0] for x in loaded}:
                continue
            loaded.append((rid, meta, "\n".join(_results_common.read_lines(path)[body_start - 1:])))
            if len(loaded) >= RECALL_MAX_RECORDS:
                break
        if not loaded:
            return (f"[ERROR] 找不到結果 {'、'.join('#' + m for m in missing) or ref_arg}。這個編號若來自工具使用檢索清單，"
                    f"代表原始檔已超過保留上限被清掉：直接告訴使用者這筆資料已經不在，需要的話重新執行當時的工具，不要換個編號亂試。")
        ids = [rid for rid, _, _ in loaded]
        label = "、".join(f"#{i}" for i in ids)
        per_budget = TOOL_SUMMARY_INPUT_MAX_CHARS // len(loaded)
        chunks, omitted, background = [], 0, []
        for rid, meta, raw in loaded:
            clipped_one, om = self._clip_tool_output(raw, per_budget)
            omitted += om
            chunks.append(clipped_one if len(loaded) == 1 else f"===== 存檔 #{rid}（{meta.get('script') or '?'}）=====\n{clipped_one}")
            hint = self._tool_use_index_hint(rid)
            background.append(f"#{rid}：當時的任務：{meta.get('task') or '(未知)'}；執行的指令：{meta.get('command') or meta.get('script') or '(未知)'}"
                              + (f"；檢索清單對它的描述：{hint}" if hint else ""))
        clipped = "\n\n".join(chunks)
        tool_tokens = sum(self.count_tokens(raw) for _, _, raw in loaded)
        hints = {self._tool_use_index_hint(i) for i in ids}
        # 錨點用 harness 自己記的任務線，不靠模型抄：實測小模型會把清單上的描述抄進參數當問題，而使用者回答追問時
        # 最新一句常只剩「第一個」。模型給的參數只當聚焦點，跟使用者原話或清單描述一樣時就是噪音、不帶。
        user_now = " ".join((self.current_task or "").split())
        focus = question if user_now and question != user_now and question not in hints else ""
        primary = user_now or question
        anchor = self._build_task_anchor_text() if user_now else question
        purpose_text = (
            f"【使用者現在的問題】\n{anchor}\n"
            + (f"【決策 AI 要聚焦的點】\n{focus}\n" if focus else "")
            + f"\n【{'這份' if len(ids) == 1 else '這幾份'}存檔的背景（不是現在要回答的問題）】\n" + "\n".join(background)
        )
        data, structured, raw_model_text = self._extract_with_followups(
            clipped, purpose_text, tool_tokens, omitted, ids[0] if len(ids) == 1 else None, exclude_ids=ids)
        if structured and data is not None:
            body = self._render_tool_summary(data, "、#".join(ids))
        else:
            body = raw_model_text or "（獨立 session 沒有回傳合法結構，請改用 result_grep 自行搜尋關鍵字。）"
        gone = f"（{'、'.join('#' + m for m in missing)} 已經不在：原始檔超過保留上限被清掉，要的話告訴使用者。）\n" if missing else ""
        guidance = self._followup_guidance(ids, data, structured, recalled=True)
        output_text = (f"{TOOL_SUMMARY_TAG}\n重新讀取存檔 {label} 的完整原文，"
                       f"針對「{primary}」{'（聚焦：' + focus + '）' if focus else ''}提煉：\n{gone}{body}\n"
                       f"（以上是獨立 session 回到完整原文、針對使用者的問題提煉的重點。{guidance}）")

        self._record_trajectory(
            "result_recall_cmd.py", [ref_arg, question], output_text,
            self.current_cwd, self.container_cwd, self.target_container,
        )
        return output_text


    # =========================================================
    # 🧩 操作軌跡記錄與 make_skill（把做對的步驟編譯成組合技能）
    # 分工：harness 記錄軌跡、挑步驟、驗證模型填的表單、用範本產生檔案、寫入索引；
    # 模型只填一份 JSON（標題、描述、分類、參數化、每步目的、注意事項）；使用者在預覽後核准。
    # 模型不寫任何 Python：產生的腳本只是資料，執行邏輯在 scripts/_composite.py。
    # =========================================================
    def _script_skill_map(self):
        """scripts/<x>.py -> 技能名稱。先以「<技能>.md 提到 scripts/<技能>_cmd.py」為準，
        其餘出現過的腳本再補上；每次重建（十幾個小檔案，成本可忽略），技能新增後不需重啟。"""
        mapping, mentions = {}, {}
        if not os.path.isdir(self.tools_dir):
            return mapping
        for fname in sorted(os.listdir(self.tools_dir)):
            if not fname.endswith(".md"):
                continue
            skill = fname[:-3]
            try:
                with open(os.path.join(self.tools_dir, fname), "r", encoding="utf-8") as f:
                    scripts = set(re.findall(r"scripts/([A-Za-z0-9_]+\.py)", f.read()))
            except OSError:
                continue
            if f"{skill}_cmd.py" in scripts:
                mapping[f"{skill}_cmd.py"] = skill
            for s in scripts:
                mentions.setdefault(s, skill)
        for s, skill in mentions.items():
            mapping.setdefault(s, skill)
        return mapping

    def _record_trajectory(self, script_name, args, output_text, cwd, container_cwd, target_container=""):
        """run_tool 每執行一支腳本（成功、失敗、逾時都算；找不到腳本的猜測不算）記一筆。"""
        status = "ERROR" if output_text.lstrip().startswith("[ERROR]") else "PASS"
        self.trajectory_seq += 1
        record = {
            "id": self.trajectory_seq,
            "kind": "exec",
            "ts": time.strftime("%m-%d %H:%M:%S"),
            "script": script_name,
            "skill": self._script_skill_map().get(script_name),
            "args": list(args),
            "command": self._format_execute(script_name, args),
            "cwd": cwd,
            "container_cwd": container_cwd,
            "target_container": target_container,
            "status": status,
            "output_head": output_text[:TRAJECTORY_OUTPUT_HEAD],
            "task": (self.current_task or "")[:200],
            "plan_active": bool(self.current_plan),
        }
        record["result_file"] = self._archive_tool_result(record, output_text)
        self.last_result_id = record["id"] if record["result_file"] else None
        self.last_result_file = record["result_file"]
        self.trajectory.append(record)
        self._append_trajectory_log(record)
        return record

    # ---------------------------------------------------------------- 📄 工具結果存檔
    def results_dir(self):
        return os.path.join(self.script_dir, "logs", TOOL_RESULTS_DIRNAME)

    def _max_archived_id(self):
        """既有存檔裡最大的編號（沒有存檔就 0）：__init__ 用它當 trajectory_seq 的起點，讓編號跨 session 不重複。"""
        return max((f[1] for f in _results_common.list_result_files(self.results_dir())), default=0)

    def _archive_tool_result(self, record, output_text):
        """把一次腳本執行的完整原始輸出寫成 logs/tool_results/<session>_<id>_<腳本>.md，並在 index.md 追加一行。
        檔頭是簡單的 key: value（宿主機不一定有 PyYAML）。寫不進去不影響主流程（回傳 None）。"""
        try:
            d = self.results_dir()
            os.makedirs(d, exist_ok=True)
            stem = re.sub(r"[^A-Za-z0-9_-]", "_", re.sub(r"(_cmd)?\.py$", "", record["script"]))
            filename = f"{self.session_id}_{record['id']:03d}_{stem}.md"
            task = " ".join((record.get("task") or "").split())
            header = [
                "---", f"id: {record['id']}", f"session: {self.session_id}", f"ts: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"script: {record['script']}", f"skill: {record.get('skill') or ''}", f"command: {record['command']}",
                f"cwd: {record['cwd']}", f"container: {record.get('target_container') or ''}", f"status: {record['status']}",
                f"chars: {len(output_text)}", f"task: {task[:200]}", "---",
            ]
            with open(os.path.join(d, filename), "w", encoding="utf-8") as f:
                f.write("\n".join(header) + "\n" + output_text + ("\n" if not output_text.endswith("\n") else ""))
            line = (f"#{record['id']} | {time.strftime('%Y-%m-%d %H:%M')} | {record['script']} | {record['status']} | "
                    f"{len(output_text)} 字 | {filename} | 任務：{task[:60]} | 回答：")
            with open(os.path.join(d, TOOL_RESULTS_INDEX), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._prune_tool_results(d)
            return filename
        except OSError:
            return None

    def _prune_tool_results(self, d):
        """只留最近 TOOL_RESULTS_KEEP 個檔、總大小不超過 TOOL_RESULTS_MAX_MB；刪最舊的並把 index.md 裡對應的行拿掉。"""
        names = sorted(fn for fn in os.listdir(d) if TOOL_RESULT_FILE_RE.match(fn))   # 檔名＝session_id + 三位數編號，排序即時間順序
        sizes = {fn: os.path.getsize(os.path.join(d, fn)) for fn in names}
        total, removed = sum(sizes.values()), []
        while names and (len(names) > TOOL_RESULTS_KEEP or total > TOOL_RESULTS_MAX_MB * 1024 * 1024):
            fn = names.pop(0)
            total -= sizes[fn]
            os.remove(os.path.join(d, fn))
            removed.append(fn)
        if removed:
            index = os.path.join(d, TOOL_RESULTS_INDEX)
            if os.path.exists(index):
                with open(index, encoding="utf-8") as f:
                    lines = [ln for ln in f.read().splitlines() if not any(f"| {fn} |" in ln for fn in removed)]
                with open(index, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + ("\n" if lines else ""))
        return removed

    def update_result_answer(self, result_id, answer):
        """摘要 session 產出「回答」後回填到 index.md 該筆的「回答：」欄，result_list 一眼就能看到每個存檔在講什麼。"""
        if not result_id or not answer:
            return False
        try:
            index = os.path.join(self.results_dir(), TOOL_RESULTS_INDEX)
            with open(index, encoding="utf-8") as f:
                lines = f.read().splitlines()
            key = f"#{result_id} | "
            for i, ln in enumerate(lines):
                if ln.startswith(key) and f"| {self.session_id}_" in ln:
                    lines[i] = ln.rsplit("| 回答：", 1)[0] + "| 回答：" + " ".join(answer.split())[:120]
                    break
            else:
                return False
            with open(index, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            return True
        except OSError:
            return False

    def _tool_use_index_hint(self, result_id):
        """tools_use_index.md 裡某編號的關聯敘述（同編號有多筆時以最後一筆為準）；沒有就回空字串。_result_recall 拿它當背景。"""
        path = os.path.join(self.results_dir(), TOOL_USE_INDEX_NAME)
        key = f"#{str(result_id).lstrip('#')} | "
        hint = ""
        try:
            with open(path, encoding="utf-8") as f:
                for ln in f:
                    if ln.startswith(key):
                        parts = ln.rstrip("\n").split(" | ", 4)
                        if len(parts) >= 5:
                            hint = parts[4].strip()
        except OSError:
            pass
        return hint

    def _append_tool_use_index(self, result_id, filename, index_hint):
        """把這筆任務導向擷取記進 tools_use_index.md：日後（可能是完全不同一次啟動、不同 session）main
        session 才有機會發現「現在的問題」跟「某次工具回傳」有關，進而用 result_recall 依編號重新讀原文提煉——
        這不是給模型翻找細節用的（細節查 result_grep／result_view），只存一句話關聯敘述，不存原文。
        只有 summarize_tool_result 處理「原始工具」的大量回傳時才呼叫（result_* 這類看舊存檔的衍生輸出不記，見
        DERIVED_RESULT_SCRIPTS）。永遠 append、不受 _prune_tool_results 影響，即使原始檔之後被清掉也留著當歷史軌跡；
        system prompt 只顯示最近 TOOL_USE_INDEX_SHOW 筆，見 _tool_use_index_block。沒有 result_id、拿不到檔名、或
        模型沒給出 index_hint 時不寫——沒檔名代表原文根本沒存成功，寫了也是死線索。長度用中文字數算（clip_hint）。
        檔名只是給人看／除錯用，模型只需要抄編號（編號從啟動時的最大存檔編號續編，跨 session 不重複）。"""
        hint = clip_hint(index_hint)
        if not result_id or not filename or not hint:
            return
        try:
            d = self.results_dir()
            os.makedirs(d, exist_ok=True)
            line = f"#{result_id} | {time.strftime('%Y-%m-%d %H:%M')} | {self.session_id} | {filename} | {hint}"
            with open(os.path.join(d, TOOL_USE_INDEX_NAME), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def result_file_path(self, ref):
        """'16'／'#16'（優先目前 session）、'latest'、'index' 或檔名 → 完整路徑；找不到回 None。Web 的 /api/results/<ref> 用。"""
        d = self.results_dir()
        if not os.path.isdir(d):
            return None
        ref = str(ref or "").strip()
        if ref == "index":
            p = os.path.join(d, TOOL_RESULTS_INDEX)
            return p if os.path.exists(p) else None
        names = sorted(fn for fn in os.listdir(d) if TOOL_RESULT_FILE_RE.match(fn))
        if ref.lstrip("#").isdigit():
            rid = int(ref.lstrip("#"))
            cands = [fn for fn in names if fn.split("_")[2] == f"{rid:03d}"]
            mine = [fn for fn in cands if fn.startswith(self.session_id + "_")]
            chosen = (mine or cands)
            return os.path.join(d, chosen[-1]) if chosen else None
        if ref in ("latest", "last"):
            return os.path.join(d, names[-1]) if names else None
        base = os.path.basename(ref)
        return os.path.join(d, base) if base in names else None

    def add_trajectory_boundary(self, reason, **extra):
        """在軌跡記一個起點（/clear、計畫核准、make_skill 完成）；連續的起點只留一個。"""
        if not self.trajectory:
            return None  # 什麼都還沒執行（例如啟動時的 reset_conversation），不需要起點
        if self.trajectory[-1]["kind"] == "boundary" and not extra:
            return None  # 連續的一般起點只留一個；帶資料的起點（計畫核准、make_skill）一律記
        self.trajectory_seq += 1
        record = {"id": self.trajectory_seq, "kind": "boundary", "ts": time.strftime("%m-%d %H:%M:%S"),
                  "reason": reason, **extra}
        self.trajectory.append(record)
        self._append_trajectory_log(record)
        return record

    def _append_trajectory_log(self, record):
        """追加到 logs/trajectory.jsonl（跨 session 的稽核記錄；寫不進去不影響主流程）。"""
        try:
            log_dir = os.path.join(self.script_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, TRAJECTORY_LOG), "a", encoding="utf-8") as f:
                f.write(json.dumps({"session": self.session_id, **record}, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError):
            pass

    @staticmethod
    def _quote_arg(arg):
        """顯示用：含空白／引號的參數以雙引號包住（與 AGENT.md 範例一致，shlex 可還原）。"""
        if arg == "" or any(c.isspace() for c in arg) or '"' in arg or "'" in arg:
            return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return arg

    def _format_execute(self, script_name, args):
        tail = " ".join(self._quote_arg(a) for a in args)
        return f"EXECUTE: scripts/{script_name}" + (f" {tail}" if tail else "")

    def trajectory_steps(self, spec=None):
        """挑出要編譯的步驟，回傳 (steps, error)。
        spec 省略＝上一個起點之後的全部步驟；'all'＝整個 session；'3-7'、'3,5,8'、'3-5,9'＝依編號。"""
        execs = [r for r in self.trajectory if r["kind"] == "exec"]
        if not execs:
            return [], "本次 session 還沒有執行過任何腳本，沒有可編譯的軌跡。"
        spec = (spec or "").strip().lower()
        if spec in ("", "recent", "last"):
            last_boundary = max((i for i, r in enumerate(self.trajectory) if r["kind"] == "boundary"), default=-1)
            steps = [r for r in self.trajectory[last_boundary + 1:] if r["kind"] == "exec"]
            if not steps:
                return [], ("自上一個起點（/clear、計畫核准或上一次 make_skill）之後沒有執行過腳本；"
                            "要用更早的步驟請指定編號範圍（例如 3-7）或 all，/trajectory 可查編號。")
            return steps, None
        if spec == "all":
            return execs, None
        wanted = set()
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, _, hi = part.partition("-")
                if not (lo.strip().isdigit() and hi.strip().isdigit()):
                    return [], f"步驟範圍格式錯誤：{part}（可用 3-7、3,5,8 或 all）"
                lo, hi = sorted((int(lo), int(hi)))
                wanted.update(range(lo, hi + 1))
            elif part.isdigit():
                wanted.add(int(part))
            else:
                return [], f"步驟範圍格式錯誤：{part}（可用 3-7、3,5,8 或 all）"
        steps = [r for r in execs if r["id"] in wanted]
        if not steps:
            return [], f"編號 {spec} 沒有對應到任何已執行的腳本步驟，輸入 /trajectory 查看編號。"
        return steps, None

    def format_trajectory(self):
        """/trajectory：列出本次 session 的軌跡（編號、成功／失敗、指令、所屬技能、起點）。"""
        if not self.trajectory:
            return "本次 session 尚未記錄任何腳本執行。"
        labels = {"clear": "/clear", "plan_confirmed": "計畫核准", "make_skill": "make_skill"}
        lines = ["🧭 操作軌跡（✅ 成功 / ❌ 失敗；/make_skill 預設取最後一個起點之後的步驟）："]
        for r in self.trajectory:
            if r["kind"] == "boundary":
                label = labels.get(r["reason"], r["reason"])
                if r.get("name"):
                    label += f" {r['name']}"
                lines.append(f"── 起點 #{r['id']}：{label}（{r['ts']}）──")
            else:
                mark = "✅" if r["status"] == "PASS" else "❌"
                skill = f"（{r['skill']}）" if r.get("skill") else ""
                lines.append(f"#{r['id']} {mark} {r['command']}{skill}")
        lines.append("用法：/make_skill <技能名稱> [3-7 | 3,5,8 | all]")
        return "\n".join(lines)

    @staticmethod
    def _group_trajectory(steps):
        """成功步驟各自帶著它之前的失敗嘗試（同一個意圖下的修正歷程）；最後仍未修正的失敗另外回傳。"""
        groups, pending = [], []
        for r in steps:
            if r["status"] == "PASS":
                groups.append({"step": r, "failed_before": pending})
                pending = []
            else:
                pending.append(r)
        return groups, pending

    def _plan_for_steps(self, steps):
        """這批步驟所屬的已核准計畫：最後一個步驟之前最近的計畫核准起點；沒有就用目前的 current_plan。"""
        last_id = steps[-1]["id"]
        for r in reversed(self.trajectory):
            if r["id"] < last_id and r["kind"] == "boundary" and r.get("reason") == "plan_confirmed" and r.get("plan"):
                return r["plan"]
        return self.current_plan

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

# =========================================================
# 🚀 MAIN LOOP (加入 Token Tracking 顯示)
# =========================================================

def _append_discarded_tool_result(agent):
    """使用者選擇不把工具結果加入上下文時，仍需告知 AI「工具已執行完畢」，
    避免它誤以為指令根本沒被處理而重複嘗試。同樣包上系統回傳的框架句。"""
    agent.messages.append({
        'role': 'user',
        'content': tool_result_message(
            "工具已執行完畢，但使用者選擇不把結果加入上下文（結果只有使用者看到）。請依此繼續，不要重複執行同一個指令。",
            (agent._last_assistant_step() or {}).get("action"),
        ),
    })

def _run_plan_flow(agent, user_task):
    """/plan 模式：先讓 AI 依 SKILLS.md 規劃步驟、印出來給使用者看，
    使用者核准後才讓 main() 的主迴圈開始真正執行（ask_ai -> run_tool）。

    安全設計：規劃階段自始至終不會呼叫 agent.run_tool()，就算模型不聽話
    在計畫裡夾帶了 EXECUTE: 指令也不會被執行——確認關卡是靠「這個函式
    根本不執行工具」保證的，不依賴模型是否遵守「先不要執行」的指示。

    回傳 True 代表使用者已核准，main() 可以繼續往下進入正常執行迴圈；
    回傳 False 代表使用者取消，本次任務到此為止，不會呼叫任何工具。
    """
    agent.messages.append({'role': 'user', 'content': agent.build_plan_request(user_task)})

    while True:
        plan_raw = agent.ask_ai()
        agent.total_ai_tokens += agent.last_ai_tokens(plan_raw)
        agent.messages.append({'role': 'assistant', 'content': plan_raw})
        parsed = agent.parse_reply(plan_raw)
        if agent.last_reply_retry:
            print(agent.last_reply_retry)
        plan_msg = parsed["reply"] or plan_raw  # 計畫文字在 reply；就算模型夾帶了 action，這裡也不會執行
        if parsed["thought"]:
            print(f"💭 {parsed['thought']}")
        print(f"\n📝 AI 規劃的任務計畫:\n{'-'*30}\n{plan_msg}\n{'-'*30}")

        choice = input("\n是否核准此計畫並開始執行？(y=核准 / n=取消 / 直接輸入修改意見=重新規劃): ").strip()

        if choice.lower() == 'y':
            agent.confirm_plan(plan_msg)
            return True

        if choice == "" or choice.lower() in ('n', 'no'):
            agent.messages.append({
                'role': 'user',
                'content': "[PLAN_REJECTED]\n使用者取消了上述計畫，本次任務不會執行，請等待使用者的新指示。"
            })
            print("🚫 已取消，本次任務不會執行。")
            return False

        # 其餘輸入視為修改意見，重新規劃一次
        agent.messages.append({'role': 'user', 'content': agent.build_plan_revision_request(choice)})


# =========================================================
# 🔢 Token 計量與上下文預算（全部為「真實 token」尺度）
# =========================================================
# 計量來源：
#   - AI 回覆        → Ollama 回報的 eval_count（精確）
#   - 整體上下文大小 → Ollama 回報的 prompt_eval_count（精確；快取命中時仍為完整值，已實測），
#                     兩次呼叫之間新增的訊息以校準比估算增量
#   - 使用者輸入、工具回傳 → 字元數 ÷ chars_per_token，chars_per_token 每次呼叫後用
#                     prompt_eval_count 重新校準（Ollama 沒有 tokenize API，無法精確計數）
# 下面所有門檻因此都與 num_ctx 同一尺度，可以直接比較。

# Ollama 一次請求的 context 上限。模型本身支援更長（gemma4:e4b 為 131072），這裡是為了記憶體
# 與速度自設的；主對話、壓縮摘要、工具摘要三種 session 共用同一個值。可用 AGENT_NUM_CTX 覆寫。
# 預設從 12288 提高到 32768：system prompt（AGENT.md + SKILLS.md + Memory.md + 滾動摘要）本身約
# 3000～4000 tokens，在 12288 下佔了三成，各水位之間只剩幾百 tokens 給對話，實測兩分鐘內連壓四次、
# 每次 10～15 秒，使用上明顯遲滯。記憶體較小的設備請用環境變數設回 12288 或更低。
NUM_CTX = int(os.environ.get("AGENT_NUM_CTX", "32768"))

# 🌊 雙水位線（dual watermark）比例，皆可用環境變數覆寫。
# 硬水位（TOKEN_THRESHOLD）：呼叫模型前的最後防線，超過一定同步壓縮（ensure_context_budget），
#   確保壓縮先於 Ollama 在 num_ctx 處的靜默截斷；剩下 25% 留給模型輸出與下一則訊息。
#   （更早的版本寫死 8000 且以「字元÷4」計量，換算真實 token 約 17000，早已超過 num_ctx，永遠來不及觸發。）
# 軟水位（SOFT_TOKEN_THRESHOLD）：回合結束後（最終答案已送出、模型閒著）若超過就順手壓縮，切點落在
#   任務邊界、不擋在下一次回覆前面（after_turn_compression）；/parallel_cal on 時改在背景執行緒做。
HARD_RATIO = float(os.environ.get("AGENT_HARD_RATIO", "0.75"))
SOFT_RATIO = float(os.environ.get("AGENT_SOFT_RATIO", "0.60"))
TOKEN_THRESHOLD = int(NUM_CTX * HARD_RATIO)
SOFT_TOKEN_THRESHOLD = int(NUM_CTX * SOFT_RATIO)

# low watermark：壓縮時保留最新這麼多 token 的原文（在訊息邊界切、不拆開 EXECUTE／tool result
# 這一組），其餘與上一份滾動摘要融合成新摘要。舊版固定「保留最新 2 則」，兩則可能只有 50 tokens
# 也可能 1500 tokens，銜接感不穩定。
KEEP_RATIO = 0.15
KEEP_RECENT_TOKENS = int(NUM_CTX * KEEP_RATIO)

# 軟水位的「值不值得」門檻：保留區以外可壓的舊內容少於此數時，回合結束後不壓縮——一次摘要要花
# 一次模型呼叫（10～15 秒），只為了騰出幾百 tokens 不划算；硬水位不受此限。
MIN_COMPRESS_TOKENS = max(1000, NUM_CTX // 20)

# logs/ 歸檔保留份數（summary_*.md 與同名 .json 一起計算、一起刪除）。融合摘要相鄰兩份高度重複，
# 舊檔的價值主要是被融合淘汰的歷史細節，留最近 N 份給日後的反思機制當語料即可。可用 AGENT_SUMMARY_KEEP 覆寫。
SUMMARY_ARCHIVE_KEEP = int(os.environ.get("AGENT_SUMMARY_KEEP", "30"))

# 框架自動插入、但以 user 角色送進對話的系統訊息標記（AGENT.md「Harness Messages」有對應說明）。
# 摘要模型渲染對話時用它把這些訊息標成 [harness ...] 而不是 [user]，避免把框架的規則記成使用者偏好。
SKILL_LOADED_MARKER = "[skill loaded]"  # 使用者從選單手動載入的技能規格（附在使用者訊息後面）
HARNESS_MARKERS = (
    "[tool result]", "[vision result]", SKILL_LOADED_MARKER,
    "[PLAN_REQUEST]", "[PLAN_REVISION]", "[PLAN_CONFIRMED]", "[PLAN_REJECTED]",
)


def attach_skill_docs(message, blocks):
    """把使用者手動載入的技能規格區塊（manual_skill_block 的回傳）附在這則使用者訊息後面。
    跟 📷 影像分析結果同一種做法：使用者原文在前、系統插入的內容在後，主對話維持純文字單一訊息，
    不會出現連續兩則 user 訊息。"""
    blocks = [b for b in (blocks or []) if b]
    if not blocks:
        return message
    return message + "\n\n" + "\n\n".join(blocks)

# 融合摘要的長度目標（字，寫進摘要 prompt）與模型輸出硬上限（token，num_predict），
# 讓滾動摘要不會越滾越長；輸出被硬上限截斷時 JSON 會解析失敗、退回原文，因此上限要留得夠寬。
# 🧷 回覆協議：模型每次回覆都是一個 JSON 物件 {thought, reply, action}。執行與否只看 action 欄位，
# reply 裡不論寫了什麼（包括解釋、舉例時抄出來的 `EXECUTE: ...` 字串）都不會被執行——這是「實體隔離」：
# 給人看的文字與給系統執行的指令分開存放，不再用文字比對從回覆裡找指令。以 Ollama 的 format= schema 強制
# 結構（與摘要、make_skill 同一機制），小模型不需要自律「解釋時不要輸出指令」。
# action 為 null 或 {"command": 技能名稱｜規格標明的腳本路徑, "args": 參數字串}；args 是字串而不是物件，
# 因為所有技能腳本都吃位置參數、規格文件的寫法也是位置參數，這樣既有的 tools/*.md 一份都不用改：
# 規格裡的 `EXECUTE: <路徑> <參數>` 範例就對應 command=<路徑>、args=<參數>。
AGENT_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "reply": {"type": "string"},
        "action": {"anyOf": [
            {"type": "null"},
            {
                "type": "object",
                "properties": {"command": {"type": "string"}, "args": {"type": "string"}},
                "required": ["command", "args"],
            },
        ]},
    },
    "required": ["thought", "reply", "action"],
}


def quote_cli_arg(arg):
    """含空白／引號的參數以雙引號包住（與規格範例一致，shlex 可還原）。"""
    arg = str(arg)
    if arg == "" or any(c.isspace() for c in arg) or '"' in arg or "'" in arg:
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def parse_agent_reply(raw):
    """模型回覆（JSON 字串）→ {"thought", "reply", "action", "valid"}。

    action 正規化為 None 或 {"command": str, "args": str}。不是合法 JSON 時 valid=False、reply=原文、
    action=None：降級成「只顯示文字、不執行任何東西」，絕不退回用文字比對找指令（那正是要避免的誤觸發來源）。
    對非 format= 強制的後端保留一點容錯：args 給成陣列／物件、把整行 `EXECUTE: ...` 塞進 command、
    command 裡夾帶參數，都會被整理成同一種形狀。"""
    text = (raw or "").strip()
    if text.startswith("```"):  # format= 下不會出現，保險去掉程式碼區塊包裹
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return {"thought": "", "reply": (raw or "").strip(), "action": None, "valid": False}

    thought = str(data.get("thought") or "").strip()
    reply = data.get("reply")
    reply = "" if reply is None else str(reply).strip()
    action = data.get("action")
    norm = None
    if isinstance(action, str) and action.strip().lower() not in ("", "null", "none"):
        action = {"command": action, "args": ""}
    if isinstance(action, dict):
        command = str(action.get("command") or "").strip()
        args = action.get("args")
        if isinstance(args, list):
            args = " ".join(quote_cli_arg(a) for a in args)
        elif isinstance(args, dict):
            args = " ".join(quote_cli_arg(v) for v in args.values())
        args = "" if args is None else str(args).strip()
        if command.upper().startswith("EXECUTE:"):
            command = command[len("EXECUTE:"):].strip()
        head, _, rest = command.partition(" ")
        if rest.strip():  # command 夾帶了參數："scripts/cd_cmd.py /opt" → 拆到 args 前面
            command, args = head, (rest.strip() + (" " + args if args else ""))
        if command:
            norm = {"command": command, "args": args}
    return {"thought": thought, "reply": reply, "action": norm, "valid": True}


def action_text(action):
    """action 的單行文字表示（顯示與軌跡用）：`command args`。"""
    if not action:
        return ""
    return f"{action['command']} {action['args']}".strip()


SUMMARY_MAX_CHARS = 600
SUMMARY_MAX_PREDICT = 2000

# 摘要模型（壓縮摘要與工具摘要兩種獨立 session 共用）。預設 None = 與主模型相同。
# 可用 AGENT_SUMMARY_MODEL 指定同家族的小模型以減少摘要耗時（例如 gemma3:1b）；但注意：
# (1) 統一記憶體的機器（Jetson／GB10）上 CPU 卸載省不到記憶體，只省算力；
# (2) 多載一個模型可能把主模型擠出 Ollama，重載主模型的代價遠高於一次摘要；
# (3) 摘要會進入之後每一輪的 system prompt，小模型的錯誤會累積。有足夠記憶體再考慮。
# (4) 反過來說，與主模型不同的摘要模型跑在另一個 runner 程序，/parallel_cal on 的背景摘要才會真的
#     與主對話同時推論——同一個多模態模型（gemma4）目前被 Ollama 強制單 slot，做不到。
SUMMARY_MODEL = os.environ.get("AGENT_SUMMARY_MODEL", "").strip() or None

# /parallel_cal 的預設值（CLI 與 Web Console 啟動時的初始狀態），可用 AGENT_PARALLEL_CAL=1 開啟。
PARALLEL_CAL_DEFAULT = os.environ.get("AGENT_PARALLEL_CAL", "").strip().lower() in ("1", "on", "true", "yes")

# 🧩 make_skill：把使用者引導 Agent「做對」的操作軌跡編譯成新的組合技能（SkillAgent.start_skill_draft 起）。
# 草擬用的模型預設同摘要模型（AGENT_SUMMARY_MODEL，再退回主模型）；這是離線、一次性的工作，記憶體夠的話可用
# AGENT_SKILL_MODEL 指定較大的模型（例如 gemma4:26b）提高參數化與描述的品質。模型只填 JSON，不寫程式。
SKILL_MODEL = os.environ.get("AGENT_SKILL_MODEL", "").strip() or None
MAKE_SKILL_MAX_PREDICT = 3000
TRAJECTORY_LOG = "trajectory.jsonl"   # logs/ 下的軌跡稽核記錄（每次腳本執行一行，跨 session 追加；已 .gitignore）

# 📄 工具結果存檔：每次腳本執行的完整原始輸出都存成 logs/tool_results/<session>_<id>_<腳本>.md（key: value 檔頭 + 原文），
# 並在 index.md 記一行（編號、時間、腳本、狀態、大小、任務、摘要回答）。摘要只讀頭尾、主對話只拿重點，被省略的細節不再是黑洞：
# 模型用 result_list／result_grep／result_view 三個技能回查，不必重跑觀察型工具。技能規格文件不存（它不是執行結果）。
# 編號＝軌跡 id，啟動時從既有存檔的最大編號續編（跨 session 不重複，見 _max_archived_id），/make_skill 與稽核對得上。
# 保留最近 N 個檔且總大小不超過上限，超過刪最舊的。
TOOL_RESULTS_DIRNAME = "tool_results"
TOOL_RESULTS_INDEX = "index.md"
TOOL_RESULTS_KEEP = int(os.environ.get("AGENT_TOOL_RESULTS_KEEP", "200"))
TOOL_RESULTS_MAX_MB = float(os.environ.get("AGENT_TOOL_RESULTS_MAX_MB", "50"))
TOOL_RESULT_FILE_RE = re.compile(r"^\d{8}_\d{6}_\d{3,}_.+\.md$")

# 🔎 工具使用檢索清單（logs/tool_results/tools_use_index.md）：跟 index.md 不同檔、不同用途——index.md 是
# 「每次執行都記一筆」的稽核清單，會隨 _prune_tool_results 一起被裁；這份只有真的觸發過任務導向擷取
# （summarize_tool_result／_result_recall，見 _append_tool_use_index）才會記一筆：編號、時間、session、檔名、
# 「使用者問題 x 原始輸出」的 50 字關聯敘述（index_hint）。編號從啟動時既有存檔的最大編號續編（跨 session 不重複），
# 所以模型只要抄編號執行 result_recall 就能取回正確那份；檔名只是給人看／除錯用。這個檔案永遠只 append，
# 不隨舊存檔被裁掉而刪除對應行；get_system_prompt 只在 system prompt 尾端顯示最近 N 筆（_tool_use_index_block），
# 是「顯示視窗」不是資料上限。
TOOL_USE_INDEX_NAME = "tools_use_index.md"
TOOL_USE_INDEX_SHOW = int(os.environ.get("AGENT_TOOL_USE_INDEX_SHOW", "30"))
TOOL_USE_INDEX_HINT_MAX = 50   # 中文字數（clip_hint 的算法），不是 len()
# 看舊存檔的衍生輸出：不記進檢索清單（會跟原本那筆重複佔位，recall 到它只拿得到部分內容）
DERIVED_RESULT_SCRIPTS = {"result_grep_cmd.py", "result_view_cmd.py", "result_list_cmd.py", "result_recall_cmd.py"}
_HINT_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:#@+\-]+|\S")
_HINT_BREAKS = "，。；、：,;）)"


def clip_hint(text, max_units=TOOL_USE_INDEX_HINT_MAX):
    """index_hint 的長度用中文習慣的字數算：中文字、標點各 1 字，一段英數（skills_system/tools、inference.py）算 1 字。
    以前用 len() 截 50 個字元，路徑一長就把中文只有二十幾字的描述攔腰截斷（實測「…相關的檔案，特別是」後面全沒了）。
    超過上限時在後半段最後一個標點處收尾並加「…」，找不到標點才硬切，不會停在半個詞中間。"""
    text = " ".join(str(text or "").split())
    tokens = list(_HINT_TOKEN_RE.finditer(text))
    if len(tokens) <= max_units:
        return text
    head = text[:tokens[max_units - 1].end()]
    for i in range(len(head) - 1, len(head) // 2, -1):
        if head[i] in _HINT_BREAKS:
            return (head[:i + 1] if head[i] in "）)" else head[:i]).rstrip() + "…"
    return head.rstrip() + "…"
TASK_HISTORY_KEEP = 3   # 任務線：摘要錨點帶最近幾則使用者訊息（使用者回答追問時，最新一句往往只是關鍵字，原本要做什麼在前一句）
TRAJECTORY_OUTPUT_HEAD = 300          # 每筆軌跡保留的輸出開頭字元數（讓草擬模型知道結果長什麼樣）
DEFAULT_SKILL_CATEGORY = "自建技能"   # 模型選的分類不在 SKILLS.md 裡時的落點（沒有這個段落會自動建立）
SKILL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,40}$")
# 會改變狀態（建容器、送工單、寫記憶、切換目錄／容器）的技能：組合技能含這些步驟時，預覽會提醒「重播驗證會真的執行」
NON_READONLY_SKILLS = {"docker_est", "workpackage_send", "workpackage_cancel", "overpending_cancel", "modify_memory", "change_dir", "docker_open"}

# 技能草稿的 JSON schema（Ollama format=）：欄位意義見 SkillAgent._make_skill_prompts，驗證見 _normalize_skill_draft。
MAKE_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "category": {"type": "string"},
        "purpose": {"type": "string"},
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "example": {"type": "string"},
                },
                "required": ["name", "description", "example"],
            },
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "integer"},
                    "include": {"type": "boolean"},
                    "purpose": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["step_id", "include", "purpose", "args"],
            },
        },
        "success_criteria": {"type": "string"},
        "pitfalls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "category", "purpose", "parameters", "steps", "success_criteria", "pitfalls"],
}

# 使用者角色但實為工具回傳的訊息前綴（見 _split_for_compression 的配對規則）
TOOL_RESULT_PREFIXES = ("[tool result]",)

# 工具結果訊息第二行的框架句開頭（tool_result_message）。壓縮摘要渲染時以它辨認並去掉框架句。
TOOL_RESULT_FRAME = "【系統回傳】"


def tool_result_message(content, action=None):
    """把工具結果包成進主對話的 user 訊息（CLI 三種模式、Web、捨棄通知統一用這個）。
    第一行固定 [tool result]（HARNESS_MARKERS 判定、壓縮切點、軌跡都認它），第二行是框架句：明說這是系統執行
    上一輪 action 的結果、不是使用者提供的、不要感謝使用者。

    為什麼寫在每一則裡而不是只寫在 AGENT.md：工具結果只能以 user 角色進入主對話——實測 Ollama 的 gemma4 模板會把
    role=tool 的訊息整個丟掉（模型完全看不到內容）；而 AGENT.md 的 Harness Messages 規則對 4B 模型不夠，實測仍回
    「感謝您提供的語義地圖」。影像分析結果（web_console._run_vision_subsession）用的是同一招。
    action 可給 parse_reply 的 action dict 或指令字串，只取腳本檔名放進框架句方便模型對應。"""
    label = ""
    if isinstance(action, dict) and action.get("command"):
        label = os.path.basename(str(action["command"]).strip())
    elif isinstance(action, str) and action.strip():
        label = os.path.basename(action.strip().split()[0])
    what = f"你上一輪 action（{label}）" if label else "你上一輪 action"
    frame = (f"{TOOL_RESULT_FRAME}以下是系統執行{what}的結果，由系統自動產生、不是使用者提供的（使用者也看到同一份）。"
             "回覆時稱「執行結果」或「系統回傳」，不要感謝使用者、不要說「您提供的」。")
    tail = f"{TOOL_RESULT_FRAME}（以上為系統回傳，不是使用者提供的）"   # 結尾再提醒一次：長輸出時開頭那句離模型太遠
    return f"[tool result]\n{frame}\n{content}\n{tail}"

# 融合摘要的 JSON schema：交給 Ollama 的 format= 做結構化輸出，再由 _render_summary_markdown 排版。
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "key_progress": {"type": "array", "items": {"type": "string"}},
        "results_and_errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["category", "description", "detail"],
            },
        },
        "user_preferences": {"type": "array", "items": {"type": "string"}},
        "open_items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "key_progress", "results_and_errors", "user_preferences", "open_items"],
}

# 字元→token 校準比的預設值與合理範圍。中文為主的內容實測約 1.8～1.9 字元/token。
DEFAULT_CHARS_PER_TOKEN = 1.9
MIN_CHARS_PER_TOKEN, MAX_CHARS_PER_TOKEN = 1.0, 6.0

# 單一工具回傳內容的 token 門檻：超過此值時，不把完整原始內容塞進主對話（避免一次搜尋／列目錄／
# topic 擷取的大量輸出把 context 灌爆、干擾推理），而是交給獨立的摘要 session（summarize_tool_result）
# 拿「完整原始輸出 + 使用者目標 + 這一步的目的」做任務導向擷取，主對話只收到重點；完整內容仍會顯示給
# 使用者（CLI 印出、或 web_console 的系統/工具回傳面板）。約 1000 字元。
# （舊值 250 是「字元÷4」尺度，換成真實尺度即為 500。）
TOOL_RESULT_TOKEN_THRESHOLD = 500

# 🧠 任務導向摘要（Task-Oriented Summarization）：超過門檻的工具回傳一律走這條路，沒有腳本自帶的豁免
# （舊版 [PASS][digest] 讓分析型輸出放寬到 1500 tokens 直接進主對話，等於由腳本決定什麼重要——已移除，
# 腳本改回傳完整資訊，重要與否交給知道任務的獨立 session 判斷）。唯一例外是技能規格文件（SKILL_DOC_PREFIX）。
# 預設開啟；/summarize off 或 AGENT_TOOL_SUMMARY=0 改回只給成功／失敗判定（不多花一次模型呼叫）。
TOOL_SUMMARY_DEFAULT = os.environ.get("AGENT_TOOL_SUMMARY", "1").strip().lower() not in ("0", "off", "false", "no")
# summarize_tool_result() 產生的內容固定以這個標籤開頭：CLI 據此印出、Web 據此推 🧠 卡片，讓使用者看到主對話實際收到的內容。
TOOL_SUMMARY_TAG = "[tool result - 任務導向摘要]"
# _content_for_context 退回「只給成功／失敗判定」時的固定開頭（/summarize off 或摘要 session 失敗）；UI 據此標示 AI 實際收到的是哪一種。
TOOL_REDUCED_TAG = "[tool result - 已精簡]"
# 獨立 session 一次最多讀多少原始輸出（字元）：超過就保留頭尾、明確告知中間省略了多少（見 _clip_tool_output）。
# 16000 字 ≈ 8500 tokens，加上 system prompt 與錨點仍遠低於 NUM_CTX；小記憶體設備可用環境變數調低。
TOOL_SUMMARY_INPUT_MAX_CHARS = int(os.environ.get("AGENT_TOOL_SUMMARY_INPUT_CHARS", "16000"))
TOOL_SUMMARY_MAX_CHARS = 400      # 摘要長度目標（字，寫進 prompt）；門檻 500 tokens ≈ 950 字，摘要要明顯小於它才有意義
TOOL_SUMMARY_MAX_PREDICT = 1200   # 摘要輸出的 num_predict 硬上限；被截斷時 JSON 解析失敗會退回原文
# summarize_tool_result 的自動追問：not_covered 有缺口且摘要自己給出 suggested_questions 時，最多對同一份
# 存檔自動再 result_grep 幾次（見該方法內的迴圈）。停止條件是機械的（跳數上限／grep 落空／沒有關鍵字），
# 不要求任何模型判斷「問題本身夠不夠明確」——跳滿仍未涵蓋就誠實回報，不偽裝成已解決。
TOOL_SUMMARY_MAX_FOLLOWUP_HOPS = int(os.environ.get("AGENT_TOOL_SUMMARY_MAX_HOPS", "2"))
GREP_NO_HIT_RE = re.compile(r"共 0 行命中")   # result_grep_cmd.py 的標頭；自動追問遇到就停，不拿空結果去擷取
RECALL_MAX_RECORDS = 3   # result_recall 一次最多讀幾份存檔（交給同一個獨立 session；輸入上限平均分給每一份）

# 任務導向摘要的結構（Ollama format=）：answer 一句話回答這一步的目的、facts 照抄的相關事實、errors 錯誤原文、
# not_covered 原始輸出沒有／被省略而無法確認的部分——讓主模型知道「摘要裡沒有」不等於「輸出裡沒有」。
TOOL_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "facts": {"type": "array", "items": {"type": "string"}},
        "errors": {"type": "array", "items": {"type": "string"}},
        "not_covered": {"type": "string"},
        "suggested_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "keywords": {"type": "string"}},
                "required": ["question", "keywords"],
            },
        },
        "index_hint": {"type": "string"},
        "related_records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["id", "reason"],
            },
        },
    },
    "required": ["answer", "facts", "errors", "not_covered", "suggested_questions", "index_hint", "related_records"],
}

# run_tool 載入技能規格文件時回傳字串的固定開頭。規格文件是「按需載入」機制的核心，
# 內容（尤其是實際腳本路徑與參數格式）必須完整進入上下文，因此 _content_for_context
# 對這類結果一律放行、不套用 TOOL_RESULT_TOKEN_THRESHOLD，Web Console 也不標記 ⚠️。
# 規格書本身仍應維持精簡（以 400 tokens／約 800 字元以內為原則），節省每次載入的上下文成本。
SKILL_DOC_PREFIX = "📘 已載入技能"


def is_skill_doc_result(result):
    """result 是否為 run_tool 載入規格文件的回傳（而非腳本執行結果）。"""
    return bool(result) and result.lstrip().startswith(SKILL_DOC_PREFIX)


def is_exempt_result(result, tool_tokens):
    """不套用工具回傳門檻的結果：技能規格文件，以及本身已經是任務導向擷取的結果（result_recall 的輸出，以
    TOOL_SUMMARY_TAG 開頭）——後者再摘要一次只會摘要的摘要、多一次模型呼叫。所有腳本輸出（包括分析型的
    --watch／--duration／語義地圖）都走同一套規則——超過門檻就交給獨立 session 做任務導向擷取。
    CLI／Web 的 ⚠️ 標記與 _content_for_context 共用這個判斷。"""
    return is_skill_doc_result(result) or result.startswith(TOOL_SUMMARY_TAG)

# 單一工具腳本的總逾時（秒）：harness 的最後防線。各腳本自身應設定更短的逾時
# （容器類腳本可調的上限 570 秒就是為了低於這個值），這裡只處理腳本本身卡死
# （例如程序內無法中斷的重運算、讀取無回應的裝置）的情況，避免整個 Agent
# （CLI 與 Web Console 都在同一條執行緒上等待工具）被無限期卡住。
TOOL_EXEC_TIMEOUT = 600

def _content_for_context(result, tool_tokens, agent=None, use_summary=True):
    """決定要餵給主對話的內容。門檻內、或技能規格文件：原封不動。超過 TOOL_RESULT_TOKEN_THRESHOLD 時：

    - use_summary=True（預設）且提供 agent：交給 agent.summarize_tool_result()——一個獨立、乾淨的一次性
      session，拿完整原始輸出 + 使用者目標 + 這一步的目的做任務導向擷取，主對話收到的是跟任務有關的重點。
      所有工具回傳同一套規則，沒有腳本自帶的豁免。
    - use_summary=False（/summarize off）、沒有 agent、或摘要 session 失敗：退回只給成功／失敗判定——零延遲，
      但模型拿不到內容，只能換更精確的參數重查。
    """
    if tool_tokens <= TOOL_RESULT_TOKEN_THRESHOLD or is_exempt_result(result, tool_tokens):
        return result

    if use_summary and agent is not None:
        try:
            summary = agent.summarize_tool_result(result, tool_tokens)
            # 摘要不比原文短時直接給原文：實測 700 tokens 左右的結構化輸出（ros2 node info）會被摘要 session「展開」成
            # 900 多 tokens，門檻的目的是省上下文，這時原文反而更省、也不會有摘要自己數錯的問題。
            if agent.count_tokens(summary) < tool_tokens:
                return summary
            print(f"ℹ️ 任務導向摘要（≈{agent.count_tokens(summary)} tokens）不比原文（≈{tool_tokens} tokens）短，主對話改收完整原文")
            return result
        except Exception as e:
            print(f"⚠️ 獨立摘要 session 執行失敗，改用成功/失敗判定：{e}")

    status = "失敗" if result.lstrip().startswith("[ERROR]") else "成功"
    rid = getattr(agent, "last_result_id", None) if agent is not None else None
    recall = (f"完整原始輸出已存成結果檔 #{rid}：需要內容時由你自己執行 result_grep {rid} <關鍵字> 搜、或 result_view {rid} 看片段"
              f"（不要叫使用者去搜），不要重新執行同一個工具" if rid else "需要內容時請換更精確的參數重新執行工具")
    return (
        f"{TOOL_REDUCED_TAG}\n"
        f"指令已{status}執行（原始輸出約 {tool_tokens} tokens，超過門檻 {TOOL_RESULT_TOKEN_THRESHOLD}，"
        f"且獨立摘要 session 未啟用或失敗，內容未加入上下文）。完整內容已顯示給使用者；你看不到它。{recall}，不要憑空補上結果。"
    )


def context_kind(content, tool_tokens=None):
    """主對話實際收到的是哪一種：summary（任務導向摘要）／reduced（只有成功／失敗）／doc（規格文件，完整放行）／raw（完整原文）。
    CLI 與 Web 用同一個判斷來標示，使用者永遠同時看得到完整原文與 AI 收到的版本。"""
    if content.startswith(TOOL_SUMMARY_TAG):
        return "summary"
    if content.startswith(TOOL_REDUCED_TAG):
        return "reduced"
    if is_skill_doc_result(content):
        return "doc"
    return "raw"


def _context_content_for_cli(agent, result, tool_tokens, use_summary):
    """CLI 用：算出要餵給主對話的內容，並印出「主對話實際收到什麼」：完整原文只印一行提示（原文剛才已完整印過），
    任務導向摘要與成功／失敗判定整段印出（Web Console 以 🧠 卡片顯示同一份內容）。"""
    content = _content_for_context(result, tool_tokens, agent=agent, use_summary=use_summary)
    kind = context_kind(content, tool_tokens)
    if kind == "summary":
        print(f"\n🧠 獨立 session 任務導向摘要（主對話實際收到的內容）:\n{'-'*30}\n{content}\n{'-'*30}")
    elif kind == "reduced":
        print(f"\n🧠 主對話實際收到的內容（只有成功／失敗判定）:\n{'-'*30}\n{content}\n{'-'*30}")
    elif kind == "doc":
        print(f"🧠 主對話收到：完整規格文件（≈{tool_tokens} tokens，不受門檻限制）")
    else:
        print(f"🧠 主對話收到：完整原文（≈{tool_tokens} tokens，未縮減）")
    return content

def after_turn_compression(agent, parallel, notify):
    """回合結束後的軟水位檢查（CLI 與 Web Console 共用）。

    這個時間點最終答案已經送出、模型閒著，壓縮不會拉長任何一次回覆的等待時間，切點也剛好
    落在任務邊界。parallel=True（/parallel_cal on）時改在背景執行緒做，主對話可以立刻繼續；
    否則同步做完再回到等待輸入。notify 是輸出通知的函式（CLI 用 print，Web 推 system 事件）。
    回傳 None（未觸發）／'sync'／'background'。"""
    ctx_before = agent.context_tokens()
    if ctx_before <= SOFT_TOKEN_THRESHOLD:
        return None
    # 可壓的內容太少就不值得一次模型呼叫（system prompt 本身很大時，超過水位卻沒什麼可壓是常態）
    if agent.compressible_tokens() < MIN_COMPRESS_TOKENS:
        return None
    if parallel:
        if agent.start_background_compression():
            notify(
                f"🗜️ 回合結束，上下文約 {ctx_before} tokens 超過軟水位 {SOFT_TOKEN_THRESHOLD}，"
                f"已在背景開始壓縮（/parallel_cal on），完成後會通知。"
            )
            return 'background'
        return None
    notify(f"🗜️ 回合結束，上下文約 {ctx_before} tokens 超過軟水位 {SOFT_TOKEN_THRESHOLD}，壓縮中...")
    if agent.compress_context_to_file():
        info = agent.last_compression
        notify(
            f"📦 已將 {info['messages']} 則舊對話融合成摘要（約 {info['summary_tokens']} tokens）並歸檔："
            f"{os.path.basename(info['file'])}；上下文約 {agent.context_tokens()} tokens。"
        )
        return 'sync'
    return None

def _run_make_skill_flow(agent, arg_text):
    """CLI 的 /make_skill：草擬 → 預覽 → y 核准／t 重播驗證後核准／n 取消／其他文字＝修改意見重擬。
    與 Web 的 handle_skill_draft_response 用同一套 SkillAgent 狀態機。"""
    parts = arg_text.split()
    if not parts:
        print("用法：/make_skill <技能名稱> [步驟範圍，例如 3-7、3,5,8 或 all]；先用 /trajectory 查看已記錄的步驟")
        return
    name, spec = parts[0], (parts[1] if len(parts) > 1 else None)
    print(f"🧩 正在依操作軌跡草擬技能 {name}（模型 {agent.skill_model}）…")
    draft, err = agent.start_skill_draft(name, spec)
    if err:
        print(f"⚠️ {err}")
        return
    print(agent.skill_draft_preview(draft))
    while True:
        choice = input("\n核准並註冊(y) / 先重播驗證再註冊(t) / 取消(n) / 直接輸入修改意見: ").strip()
        lower = choice.lower()
        if lower in ('y', 't'):
            ok, msg = agent.approve_skill_draft(replay=(lower == 't'))
            print(msg)
            if ok:
                return
            continue
        if choice == "" or lower in ('n', 'no'):
            print(agent.cancel_skill_draft())
            return
        print("🧩 依修改意見重新草擬…")
        draft, err = agent.revise_skill_draft(choice)
        if err:
            print(f"⚠️ {err}")
            continue
        print(agent.skill_draft_preview(draft))


def main():
    agent = SkillAgent(
        model="gemma4:e4b",
        max_history=None,  # 則數視窗停用，統一以 token 門檻壓縮
    )
    agent.reset_conversation()
    auto_mode = False
    hybrid_mode = False  # 👈 新增狀態
    tool_summary_mode = TOOL_SUMMARY_DEFAULT  # 👈 工具回傳超過門檻時交給獨立 session 做任務導向摘要（預設開；/summarize off 改成只給成功／失敗）
    parallel_cal = PARALLEL_CAL_DEFAULT  # 👈 軟水位壓縮改在背景執行緒做（需 Ollama 有 ≥2 個 parallel slot 才真的平行）
    pending_skill_blocks = []  # 👈 /skill <名稱> 手動載入的技能規格，隨下一則新任務訊息一起送出
    plan_mode = False  # 👈 開啟後，下一個新任務先規劃、經使用者核准後才執行；核准即自動退出

    print("\n" + "="*50)
    print("V6 Robot Agent + Token Tracker 已啟動")
    print("="*50)

    while True:
        try:
            user_msg = input("\n👤 使用者: ")

            # 背景壓縮（/parallel_cal on）完成或失敗的通知，在使用者下一次輸入後印出
            for notice in agent.pop_notices():
                print(notice)
            if agent.compression_in_progress():
                print("🗜️ 背景壓縮仍在進行中，超過硬水位時會先等它完成。")

            # --- 基礎指令 ---
            if user_msg.lower() in ['exit', 'quit']:
                break
            if user_msg.lower() == '/clear':
                agent.reset_conversation()
                print("🧹 記憶已清空。")
                continue

            # --- 新增：手動壓縮指令 ---
            if user_msg.lower() == '/compress':
                if agent.compress_context_to_file():
                    print("🗜️ 歷史已手動壓縮並歸檔。")
                else:
                    print("ℹ️ 目前沒有需要壓縮的舊對話。")
                continue
            
            # --- 模式切換指令 ---
            if user_msg.lower() == '/auto on':
                auto_mode = True
                print("🤖 已開啟 Auto Continue 模式")
                continue
            if user_msg.lower() == '/auto off':
                auto_mode = False
                print("🛑 已關閉 Auto Continue 模式")
                continue
            if user_msg.lower() == '/hybrid on':
                hybrid_mode = True
                print("🧬 已開啟 Hybrid Mode")
                continue
            if user_msg.lower() == '/hybrid off':
                hybrid_mode = False
                print("🧬 已關閉 Hybrid Mode")
                continue
            if user_msg.lower() == '/summarize on':
                tool_summary_mode = True
                print("🧠 已開啟工具回傳的任務導向摘要（預設）：超過門檻的結果由獨立 session 依使用者目標與這一步的目的擷取重點後再交給主對話")
                continue
            if user_msg.lower() == '/summarize off':
                tool_summary_mode = False
                print("🧠 已關閉工具回傳的任務導向摘要：超過門檻的結果只給主對話成功/失敗判定（不多花一次模型呼叫）")
                continue
            if user_msg.lower() == '/parallel_cal on':
                parallel_cal = True
                print("⚡ 已開啟平行壓縮：回合結束後超過軟水位時在背景執行緒壓縮，不擋下一次對話"
                      "（要真的平行需 Ollama 給此模型多個 slot，或以 AGENT_SUMMARY_MODEL 指定不同的摘要模型；硬水位仍為同步）")
                continue
            if user_msg.lower() == '/parallel_cal off':
                parallel_cal = False
                print("🔁 已關閉平行壓縮：改回序列處理，回合結束後超過軟水位時同步壓縮完再等待輸入")
                continue
            if user_msg.lower() == '/skills':
                cat = None
                for sk in agent.list_skills():
                    if sk["category"] != cat:
                        cat = sk["category"]; print(f"\n【{cat}】")
                    print(f"  {sk['name']:<20} {sk['description']}" + ("（含經驗記憶）" if sk["has_memory"] else ""))
                print("\n輸入 /skill <名稱> 可手動載入規格，隨下一則訊息一起送出")
                continue
            if user_msg.lower().startswith('/skill ') or user_msg.lower() == '/skill':
                name = user_msg[len('/skill'):].strip()
                block = agent.manual_skill_block(name) if name else None
                if block is None:
                    print(f"⚠️ 找不到技能 '{name}'，輸入 /skills 查看可用名稱" if name else "用法：/skill <技能名稱>")
                    continue
                if any(b == block for b in pending_skill_blocks):
                    print(f"ℹ️ 技能 {name} 的規格已在待送清單")
                    continue
                pending_skill_blocks.append(block)
                print(f"\n📘 已載入技能 {name} 的規格（≈{agent.count_tokens(block)} tokens），會隨你下一則訊息一起送出：\n{'-'*30}\n{block}\n{'-'*30}")
                continue
            if user_msg.lower() == '/plan on':
                plan_mode = True
                print("📝 已開啟 Plan 模式（下一個新任務會先規劃步驟，經你核准後才執行；核准後自動退出）")
                continue
            if user_msg.lower() == '/plan off':
                plan_mode = False
                print("📝 已關閉 Plan 模式（恢復直接執行）")
                continue
            if user_msg.lower() == '/trajectory':
                print(agent.format_trajectory())
                continue
            if user_msg.lower() == '/make_skill' or user_msg.lower().startswith('/make_skill '):
                _run_make_skill_flow(agent, user_msg[len('/make_skill'):].strip())
                continue
            if user_msg.lower() == '/plan done':
                if agent.current_plan:
                    agent.current_plan = None
                    print("✅ 已提早清除目前的計畫（system prompt 不再提醒 AI 依計畫執行；平常會在下一個新任務送出時自動清除）")
                else:
                    print("ℹ️ 目前沒有進行中的計畫")
                continue

            # --- 🎯 OBJECTIVE 設定 ---
            if user_msg.lower() == "objective set":
                print("\n🎯 進入任務錨點設定模式 (輸入 objective end 結束)")
                objective_lines = []
                while True:
                    line = input("OBJECTIVE >> ")
                    if line.lower() == "objective end": break
                    objective_lines.append(line)
                agent.sticky_objective = "\n".join(objective_lines).strip()
                continue
            if user_msg.lower() == "objective show":
                print(f"\n🎯 Current: {agent.sticky_objective or 'None'}")
                continue
            if user_msg.lower() == "objective clear":
                agent.sticky_objective = ""
                print("🧹 已清除 Sticky Objective")
                continue

            # --- 📥 USER TOKEN 計算 ---
            user_tokens = agent.count_tokens(user_msg)
            agent.total_user_tokens += user_tokens
            print(f"📥 User Tokens: {user_tokens}")

            # 新任務開始：上一個已核准的計畫到此結束，自動清除。計畫的生命週期 = 核准後那個任務的
            # 執行期間（期間的工具決策、auto 迴圈、自動壓縮都不會清掉它）；再打一句新訊息就是新任務。
            # 舊版要求手動 /plan done，容易忘記而讓舊計畫殘留在 system prompt 干擾之後的每個任務。
            if agent.current_plan:
                agent.current_plan = None
                print("🧹 上一個已核准的計畫已隨新任務自動清除（system prompt 不再要求依舊計畫執行）")

            # 記下使用者這句話（任務線：最近 3 句），獨立 session 摘要或 recall 時
            # 拿它當「使用者的目標」的一部分（見 _build_task_anchor_text）
            agent.set_current_task(user_msg)

            # 📘 手動載入的技能規格附在這則訊息後面一起送出（與 Web Console 一致）
            content = user_msg
            if pending_skill_blocks:
                content = attach_skill_docs(user_msg, pending_skill_blocks)
                agent.total_tool_tokens += sum(agent.count_tokens(b) for b in pending_skill_blocks)
                print(f"📘 隨訊息載入 {len(pending_skill_blocks)} 份技能規格")
                pending_skill_blocks.clear()

            # --- 📝 PLAN 模式：先規劃、經使用者核准才進入下面的執行迴圈 ---
            if plan_mode:
                if not _run_plan_flow(agent, content):
                    after_turn_compression(agent, parallel_cal, print)  # 取消也算回合結束
                    continue  # 使用者取消了計畫，維持 Plan 模式，回到最上層等待新的輸入
                # 核准即退出 Plan 模式：規劃階段結束，這個任務依 current_plan 執行，下一個新任務直接執行
                plan_mode = False
                print("📝 計畫已核准，已自動退出 Plan 模式（要再規劃下一個任務請重新 /plan on）")
            else:
                agent.messages.append({'role': 'user', 'content': content})

            while True:
                # --- 🧠 AI 推論 ---
                ai_msg = agent.ask_ai()
                ai_tokens = agent.last_ai_tokens(ai_msg)
                agent.total_ai_tokens += ai_tokens
                if agent.auto_compressed:
                    print("📦 上下文超過門檻，呼叫前已自動壓縮並歸檔。")
                parsed = agent.parse_reply(ai_msg)  # 回覆協議：顯示 reply／thought，執行只看 action
                if agent.last_reply_retry:
                    print(agent.last_reply_retry)
                print(f"\n🧠 AI:\n{'-'*30}\n{agent.format_reply_for_console(parsed)}\n{'-'*30}")
                print(f"📤 AI Tokens: {ai_tokens}")
                agent.messages.append({'role': 'assistant', 'content': ai_msg})

                # --- 🧰 TOOL 執行（只看 action 欄位，reply 裡的文字不會被執行）---
                result = agent.run_tool(parsed)
                tool_tokens = 0
                if result:
                    tool_tokens = agent.count_tokens(result)
                    agent.total_tool_tokens += tool_tokens
                    print(f"\n🚀 系統回傳:\n{'-'*30}\n{result}\n{'-'*30}")
                    if agent.last_result_file:
                        print(f"📄 已存檔 #{agent.last_result_id}（logs/{TOOL_RESULTS_DIRNAME}/{agent.last_result_file}）；"
                              f"之後可用 result_grep {agent.last_result_id} <關鍵字> 回查")
                    print(f"🧰 Tool Tokens: {tool_tokens}")
                    if tool_tokens > TOOL_RESULT_TOKEN_THRESHOLD and not is_exempt_result(result, tool_tokens):
                        print(f"⚠️ 此工具回傳約 {tool_tokens} tokens，超過門檻 {TOOL_RESULT_TOKEN_THRESHOLD}，"
                              + ("加入上下文前將交由獨立 session 依目前任務擷取重點。" if tool_summary_mode
                                 else "加入上下文時只保留成功／失敗判定（/summarize on 可改為獨立 session 摘要）。"))
                else:
                    print("✅ 無工具需要執行")

                # --- 📊 TOKEN 統計顯示 ---
                print(f"\n📦 Context Tokens: {agent.context_tokens()} / 軟水位 {SOFT_TOKEN_THRESHOLD}"
                      f" / 硬水位 {TOKEN_THRESHOLD}（num_ctx {NUM_CTX}）")
                # 硬水位的壓縮檢查統一在 ask_ai() 呼叫前（ensure_context_budget），軟水位在回合結束後
                # （after_turn_compression）。舊版這裡壓縮後 continue，會跳過「把工具結果加入上下文」
                # 那一步，AI 拿不到剛執行的結果而重複下同一個指令，已移除。
                print(f"📊 Stats | User: {agent.total_user_tokens} | AI: {agent.total_ai_tokens} | Tool: {agent.total_tool_tokens}")

                # --- 模式判定流程 ---
                if not result:
                    break

                # 1. Auto Mode
                if auto_mode:
                    agent.messages.append({'role': 'user', 'content': tool_result_message(_context_content_for_cli(agent, result, tool_tokens, tool_summary_mode), parsed["action"])})
                    print("♻️ Auto Continue 中...")
                    continue

                # 2. Hybrid Mode
                if hybrid_mode:
                    choice = input("\n🤔 Hybrid Mode - 加入上下文？(y/n): ").lower()
                    if choice == 'y':
                        agent.messages.append({'role': 'user', 'content': tool_result_message(_context_content_for_cli(agent, result, tool_tokens, tool_summary_mode), parsed["action"])})
                        continue
                    else:
                        _append_discarded_tool_result(agent)
                        print("🚫 該結果已被略過 (已告知 Agent 執行結束)")
                        # 這裡不使用 break，讓 AI 根據這個「工具執行完畢」的資訊繼續推論
                        continue

                # 3. Manual Mode
                choice = input("\n是否將系統結果加入上下文？(y/n/stop): ").lower()
                if choice == 'y':
                    agent.messages.append({'role': 'user', 'content': tool_result_message(_context_content_for_cli(agent, result, tool_tokens, tool_summary_mode), parsed["action"])})
                elif choice == 'stop':
                    break
                else:
                    _append_discarded_tool_result(agent)
                    print("👀 已略過")
                    break

            # --- 🗜️ 回合結束：軟水位檢查（答案已印出，這裡壓縮不影響回覆延遲） ---
            after_turn_compression(agent, parallel_cal, print)

        except KeyboardInterrupt:
            print("\n👋 Bye")
            break

if __name__ == "__main__":
    main()