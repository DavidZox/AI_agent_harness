import os
import sys
import json
import subprocess
import threading
import ollama  # 導入官方庫
import shlex
import time
import re

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
        # 📌 這一輪任務最原始的使用者敘述
        # 給獨立摘要 session（summarize_tool_result）在沒有 sticky_objective
        # 或 current_plan 可用時，當作「原始問題」聚焦摘要內容用，見
        # _build_task_anchor_text。每次使用者送出新任務時更新，/clear 時清空。
        # =========================
        self.current_task = None

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

        # =========================
        # 🧩 操作軌跡與 make_skill（見檔尾 SKILL_MODEL / MAKE_SKILL_SCHEMA 說明）
        # trajectory：run_tool 每執行一支腳本就記一筆（指令、參數、當時的 cwd、成功／失敗、輸出開頭），
        #   另有 kind="boundary" 的起點記錄（/clear、計畫核准、上一次 make_skill）。存在 messages 之外，
        #   上下文壓縮不會沖掉，/make_skill 從這裡取「做對的步驟」編譯成組合技能，而不是靠模型回憶。
        # pending_skill_draft：等待使用者核准／修改／取消的技能草稿（CLI 與 Web 共用同一份狀態）。
        # =========================
        self.session_id = time.strftime("%Y%m%d_%H%M%S")
        self.trajectory = []
        self.trajectory_seq = 0
        self.drafts_dir = os.path.join(self.base_path, "drafts")
        self.skill_model = SKILL_MODEL or self.summary_model
        self.pending_skill_draft = None

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

    count_context_tokens = context_tokens  # 舊名稱相容

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
        """使用者角色但內容其實是工具回傳（[tool result] / 【系統執行結果】）。
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

    def summarize_tool_result(self, result, tool_tokens):
        """比照 compress_context_to_file 的作法：開一個獨立、乾淨的一次性
        session（自己的 system/user prompt，不接觸 self.messages），專門
        把過大的工具回傳內容摘要成精簡版，摘要完就丟棄，不會留在主對話裡。

        這樣主 session 拿到的是「有意義的摘要」而不是單純的成功／失敗判定，
        同時不會因為把原始大量輸出直接塞進主上下文而干擾主 session 的推理。

        這個獨立 session 看不到主對話，因此另外附上 _build_task_anchor_text()
        取得的「使用者原始問題敘述」錨點，讓摘要聚焦在使用者真正在意的地方，
        避免因為不知道任務重點是什麼，而摘掉其實關鍵的資訊。
        """
        anchor = self._build_task_anchor_text()

        system_prompt = """你是一位專業的資料摘要助手。
你的任務是把一段指令執行後的原始輸出，摘要成精簡但保留關鍵資訊的版本，
交給另一個負責決策的 AI 使用，那個 AI 不會看到原始內容，只會看到你的摘要。

你會同時收到「使用者原始的任務／問題敘述」，這是你摘要時的聚焦依據：
跟這個任務有關、會影響下一步決策的資訊要優先保留。

規則：
- 必須保留：成功或失敗、關鍵數值、錯誤訊息、檔案／路徑名稱、數量等會影響下一步決策的資訊
- 可以捨棄：跟使用者任務無關的重複樣板文字、無關的排版細節
- 直接輸出摘要內容，不要加上「以下是摘要」之類的前言，也不要加你自己的建議
- 盡量控制在 200 字以內
"""
        user_prompt = (
            f"使用者原始的任務／問題敘述：\n{anchor}\n\n"
            f"以下是需要摘要的原始工具輸出（原始約 {tool_tokens} tokens）：\n\n{result}"
        )

        res = ollama.chat(
            model=self.summary_model,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            options={'temperature': 0.2, 'num_ctx': NUM_CTX},
            think=False,
        )
        summary = res['message']['content'].strip()
        return (
            f"[tool result - AI 摘要]\n{summary}\n\n"
            f"(原始輸出約 {tool_tokens} tokens，完整內容已顯示在剛才的系統回傳訊息中)"
        )

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
- 這一輪絕對不要輸出 EXECUTE: 指令，只列出計畫，等待使用者確認

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
規則同上：只列出計畫、不要輸出 EXECUTE: 指令，等待使用者確認。

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
        """決定要交給獨立摘要 session（summarize_tool_result）當作「使用者
        原始問題敘述」的錨點文字，讓摘要能聚焦在使用者真正在意的事情上，
        而不是對原始輸出做通用、不知道重點是什麼的精簡（容易先丟掉其實
        關鍵的資訊）。

        優先序：
        - sticky_objective：使用者主動設定、最高優先的任務錨點，本來就
          持續保留在 system prompt 裡，直接拿來用即可
        - current_plan：已核准的計畫本身就是原始任務拆解出的步驟清單；
          額外附上目前最新一則 assistant 回應（此回應是 AI 依計畫產生的，
          內容自然反映了目前執行到哪一步），讓摘要 session 能聚焦在「這
          一步」，而不是整份計畫
        - 兩者都沒有：退回這一輪任務使用者最原始輸入的文字（current_task）
        - 兩者都有：兩段一起給，不需要互斥判斷
        """
        parts = []
        if self.sticky_objective:
            parts.append(f"【使用者設定的最高優先 Objective】\n{self.sticky_objective}")

        if self.current_plan:
            last_assistant = next(
                (m['content'] for m in reversed(self.messages) if m['role'] == 'assistant'),
                None,
            )
            plan_section = f"【使用者已核准的任務計畫（依步驟拆解逐步執行）】\n{self.current_plan}"
            if last_assistant:
                plan_section += (
                    "\n\n【AI 剛才針對目前這一步的回應，可看出目前執行到哪一步】\n"
                    f"{last_assistant}"
                )
            parts.append(plan_section)

        if parts:
            return "\n\n".join(parts)

        return self.current_task or "(未取得使用者原始任務敘述)"

    def get_system_prompt(self):

        objective_prompt = self._build_objective_prompt()
        plan_prompt = self._build_plan_context_prompt()

        profile = ""
        if os.path.exists(self.profile_file):
            with open(self.profile_file, "r", encoding="utf-8") as f:
                profile = f.read()

        with open(self.index_file, "r", encoding="utf-8") as f:
            skills = f.read()

        memory_content = self.load_long_term_memory()
        history_summary = self.rolling_summary or "No history summary yet."

        status_prompt = f"\n\n## Current Agent State\n- CURRENT_WORKING_DIRECTORY: {self.current_cwd}\nCURRENT_CONTAINER_DIRECTORY: {self.container_cwd}"

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
                """

    def reset_conversation(self):
        self.current_plan = None  # /clear 時一併清掉進行中的計畫，避免舊計畫殘留誤導新任務
        self.current_task = None  # 同上，避免舊任務敘述殘留誤導下一次的摘要 session
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

            # 關閉 Ollama 的獨立 thinking 模式：此版本 Ollama 會把推理過程放進
            # message.thinking 欄位，而非像舊版把 <thought> 內嵌在 content 裡。
            # 若不關閉，模型有時會把整個決策都留在 thinking 裡，
            # 導致 content 回傳空字串（並非被截斷，而是模型判斷自己已經回答完畢）。
            # num_ctx：若不指定，Ollama 會用內建預設值（4096），而非模型實際支援的上限。
            # 4096 遠小於我們的壓縮門檻，代表對話還沒到門檻 Ollama 就已經在背後截斷最舊的
            # 內容。這裡統一用 NUM_CTX，TOKEN_THRESHOLD 定義為它的 70%（同一尺度：真實 token），
            # 保留空間給模型輸出與下一則訊息。
            response = ollama.chat(
                model=self.model,
                messages=snapshot,
                options={'temperature': 0.2, 'num_ctx': NUM_CTX},
                think=False
            )
            self._record_usage(response, snapshot)

            raw_content = response['message']['content'].strip()

            if "<thought>" in raw_content:
                raw_content = raw_content.split("</thought>")[-1].strip()
            elif "...done thinking." in raw_content:
                raw_content = raw_content.split("...done thinking.")[-1].strip()

            return raw_content

        except Exception as e:
            return f"Ollama 連線錯誤: {e}"

    def _extract_execute_payload(self, ai_response):
        """從 AI 回應中取出 EXECUTE: 後面的內容；去除 code fence／註解殘留後為空則回傳 None。"""
        start_marker = "EXECUTE:"
        start_idx = ai_response.find(start_marker)
        payload = ai_response[start_idx + len(start_marker):].strip()

        if "```" in payload:
            payload = payload.split("```")[0].strip()
        if "# ---" in payload:
            payload = payload.split("# ---")[0].strip()

        return payload or None

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
            f"{SKILL_DOC_PREFIX} '{name}' 的規格文件（使用者從選單手動載入，等同你以技能名稱 EXECUTE 後"
            f"系統回傳的規格；依此內容才可執行，請使用其中標明的實際腳本路徑）：\n{doc.rstrip()}"
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
        """依腳本類型解析參數字串。manage_skill 的參數含 `|` 分隔符，需保留原始字串；
        其餘技能才用 shlex 依空白／引號拆分成參數列表。"""
        if "manage_skill" in script_name:
            return [remainder.strip()] if remainder else []
        try:
            return shlex.split(remainder)
        except Exception:
            return remainder.split()

    def _sync_state_from_tool_output(self, output_text):
        """解析工具輸出中的狀態標記，同步更新目前的工作目錄／容器目錄。"""
        lines = output_text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("[CWD_CHANGED]"):
                self.current_cwd = line.replace("[CWD_CHANGED]", "").strip()
            # 抓取 [CONTAINER_CWD] 標記的下一行作為路徑
            if line.startswith("[CONTAINER_CWD]") and i + 1 < len(lines):
                self.container_cwd = lines[i + 1].strip()

    def run_tool(self, ai_response):
        """解析 AI 回應中的 EXECUTE: 指令並執行，行為分兩種：

        1. 若目標字串對應到 tools/<name>.md 的技能規格文件（代表 AI 用的是
           SKILLS.md 索引裡的技能名稱），直接把規格文件內容當作系統回傳注入
           上下文，不執行任何腳本——這就是按需載入 (Progressive Disclosure)。
           不做技能名稱 -> 腳本檔名的猜測或對照；AI 讀完規格後，下一輪需改用
           規格書中標明的實際腳本路徑（例如 scripts/cd_cmd.py）才會真正執行。
        2. 否則將目標字串視為實際腳本路徑，執行對應的 CLI 腳本並回傳結果。
        """
        if "EXECUTE:" not in ai_response:
            return None

        try:
            payload = self._extract_execute_payload(ai_response)
            if not payload:
                return None

            parts = payload.split(maxsplit=1)
            raw_token = os.path.basename(parts[0])
            remainder = parts[1] if len(parts) > 1 else ""

            skill_doc = self._load_skill_doc(raw_token)
            if skill_doc is not None:
                n_memory = len(self._skill_memory_entries(raw_token[:-3] if raw_token.endswith(".md") else raw_token))
                memory_note = f"，附 {n_memory} 則技能經驗記憶" if n_memory else ""
                print(f"📖 Agent 選擇技能索引: {raw_token}（載入規格文件{memory_note}，尚未執行）")
                return f"{SKILL_DOC_PREFIX} '{raw_token}' 的規格文件（依此內容才可執行，請使用其中標明的實際腳本路徑）：\n{skill_doc}"

            script_name = self._normalize_script_name(raw_token)
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
                    f"這個名稱看起來是技能 {', '.join(candidates)}，請先 `EXECUTE: {candidates[0]}` 載入規格文件，"
                    f"再依規格標明的實際腳本路徑執行。"
                    if candidates else
                    "腳本路徑只能從規格文件取得，不可自行推測：請先 `EXECUTE: [SKILLS.md 裡的技能名稱]` 載入規格。"
                )
                return f"[ERROR] 找不到腳本 {script_name}。{hint}"

            clean_args = self._parse_script_args(script_name, remainder)

            # --- 執行工具 ---
            # 將目前的容器路徑作為環境變數注入，讓 docker_run.py 讀取
            env = os.environ.copy()
            env["CONTAINER_CWD"] = self.container_cwd
            cwd_before, container_before = self.current_cwd, self.container_cwd  # 軌跡記錄用：執行「前」的狀態

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
                output_text = (
                    f"[ERROR] 工具 {script_name} 執行逾時（超過 {TOOL_EXEC_TIMEOUT} 秒），已被系統強制終止。"
                    f"這是 harness 的最後防線，各腳本自身應有更短的逾時；若經常觸發請檢查該腳本。"
                )
                self._record_trajectory(script_name, clean_args, output_text, cwd_before, container_before)
                return output_text

            if res.returncode == 0:
                output_text = res.stdout.strip()
            else:
                # 腳本異常結束：優先用 stderr，沒有就用 stdout；兩者皆空也要給 AI 一個
                # 明確的失敗訊息，否則空字串會被誤判成「沒有工具需要執行」。
                # 統一補上 [ERROR] 前綴，讓 _content_for_context 能正確判定為失敗。
                output_text = res.stderr.strip() or res.stdout.strip() or "（沒有任何輸出）"
                if not output_text.startswith("[ERROR]"):
                    output_text = f"[ERROR] 腳本 {script_name} 異常結束（exit code {res.returncode}）:\n{output_text}"
            self._sync_state_from_tool_output(output_text)
            self._record_trajectory(script_name, clean_args, output_text, cwd_before, container_before)
            return output_text

        except Exception as e:
            return f"解析指令失敗: {e}"


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

    def _record_trajectory(self, script_name, args, output_text, cwd, container_cwd):
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
            "status": status,
            "output_head": output_text[:TRAJECTORY_OUTPUT_HEAD],
            "task": (self.current_task or "")[:200],
            "plan_active": bool(self.current_plan),
        }
        self.trajectory.append(record)
        self._append_trajectory_log(record)
        return record

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
                "args": args, "original_args": list(r["args"]), "cwd": r.get("cwd"), "container_cwd": r.get("container_cwd"),
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
                    "cwd": r.get("cwd"), "container_cwd": r.get("container_cwd"),
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
            f"之後 `EXECUTE: {draft['name']}` 載入規格、`{self._skill_example_call(draft)}` 執行；"
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
    避免它誤以為指令根本沒被處理而重複嘗試。"""
    agent.messages.append({
        'role': 'user',
        'content': "[tool result]\nTool execution completed, but the result was discarded by user request."
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
        plan_msg = agent.ask_ai()
        agent.total_ai_tokens += agent.last_ai_tokens(plan_msg)
        agent.messages.append({'role': 'assistant', 'content': plan_msg})
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
    "[tool result]", "【系統執行結果】", "[vision result]", SKILL_LOADED_MARKER,
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
TRAJECTORY_OUTPUT_HEAD = 300          # 每筆軌跡保留的輸出開頭字元數（讓草擬模型知道結果長什麼樣）
DEFAULT_SKILL_CATEGORY = "自建技能"   # 模型選的分類不在 SKILLS.md 裡時的落點（沒有這個段落會自動建立）
SKILL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{1,40}$")
# 會改變狀態（建容器、送工單、寫記憶、切換目錄／容器）的技能：組合技能含這些步驟時，預覽會提醒「重播驗證會真的執行」
NON_READONLY_SKILLS = {"docker_est", "workitem_est", "modify_memory", "change_dir", "docker_open"}

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
TOOL_RESULT_PREFIXES = ("[tool result]", "【系統執行結果】")

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

# 單一工具回傳內容的 token 門檻：超過此值時，不會把完整原始內容塞進 AI 的
# 上下文（避免一次搜尋/列目錄的大量輸出把 context 灌爆、干擾推理），而是
# 改用精簡的「成功／失敗」摘要餵給 AI，讓它的推理流程保持穩定；完整內容
# 仍會顯示給使用者（CLI 印出、或 web_console 的系統/工具回傳面板）。
# 約 1000 字元。（舊值 250 是「字元÷4」尺度，換成真實尺度即為 500。）
TOOL_RESULT_TOKEN_THRESHOLD = 500

# run_tool 載入技能規格文件時回傳字串的固定開頭。規格文件是「按需載入」機制的核心，
# 內容（尤其是實際腳本路徑與參數格式）必須完整進入上下文，因此 _content_for_context
# 對這類結果一律放行、不套用 TOOL_RESULT_TOKEN_THRESHOLD，Web Console 也不標記 ⚠️。
# 規格書本身仍應維持精簡（以 400 tokens／約 800 字元以內為原則），節省每次載入的上下文成本。
SKILL_DOC_PREFIX = "📘 已載入技能"


def is_skill_doc_result(result):
    """result 是否為 run_tool 載入規格文件的回傳（而非腳本執行結果）。"""
    return bool(result) and result.lstrip().startswith(SKILL_DOC_PREFIX)

# 單一工具腳本的總逾時（秒）：harness 的最後防線。各腳本自身應設定更短的逾時
# （容器類腳本可調的上限 570 秒就是為了低於這個值），這裡只處理腳本本身卡死
# （例如程序內無法中斷的重運算、讀取無回應的裝置）的情況，避免整個 Agent
# （CLI 與 Web Console 都在同一條執行緒上等待工具）被無限期卡住。
TOOL_EXEC_TIMEOUT = 600

def _content_for_context(result, tool_tokens, agent=None, use_summary=False):
    """決定要餵給 AI 上下文的內容：正常大小就原封不動放進去。超過
    TOOL_RESULT_TOKEN_THRESHOLD 時有兩種精簡方式：

    - use_summary=False（預設）：原本的行為，只回傳成功／失敗的判定，
      不需要額外呼叫模型，穩定、零延遲。
    - use_summary=True 且提供 agent：改用 agent.summarize_tool_result()
      開一個獨立 session 做語意摘要，讓主 session 拿到的不只是成功/失敗，
      還有內容重點。此為可選功能，摘要 session 若失敗會自動退回成功/失敗
      判定，不會讓主 session 的推理流程中斷。
    """
    if tool_tokens <= TOOL_RESULT_TOKEN_THRESHOLD or is_skill_doc_result(result):
        return result

    if use_summary and agent is not None:
        try:
            return agent.summarize_tool_result(result, tool_tokens)
        except Exception as e:
            print(f"⚠️ 摘要 session 執行失敗，改用精簡成功/失敗判定：{e}")

    status = "失敗" if result.lstrip().startswith("[ERROR]") else "成功"
    return (
        f"[tool result - 已精簡]\n"
        f"指令已{status}執行（原始輸出約 {tool_tokens} tokens，超過門檻 "
        f"{TOOL_RESULT_TOKEN_THRESHOLD}）。完整內容已顯示在剛才的系統回傳訊息中，"
        f"未直接加入上下文，以維持推理穩定。"
    )

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
    tool_summary_mode = False  # 👈 工具回傳超過門檻時，是否改用獨立 session 做語意摘要
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
                print("🧠 已開啟工具回傳摘要模式（超過門檻的結果會由獨立 session 摘要後再交給主對話）")
                continue
            if user_msg.lower() == '/summarize off':
                tool_summary_mode = False
                print("🧠 已關閉工具回傳摘要模式（超過門檻的結果改回精簡成功/失敗判定）")
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

            # 記錄這一輪任務最原始的使用者敘述，供獨立摘要 session 在沒有
            # objective／plan 可用時，當作「原始問題」聚焦摘要內容
            # （見 _build_task_anchor_text）
            agent.current_task = user_msg

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
                print(f"\n🧠 AI:\n{'-'*30}\n{ai_msg}\n{'-'*30}")
                print(f"📤 AI Tokens: {ai_tokens}")
                agent.messages.append({'role': 'assistant', 'content': ai_msg})

                # --- 🧰 TOOL 執行 ---
                result = agent.run_tool(ai_msg)
                tool_tokens = 0
                if result:
                    tool_tokens = agent.count_tokens(result)
                    agent.total_tool_tokens += tool_tokens
                    print(f"\n🚀 系統回傳:\n{'-'*30}\n{result}\n{'-'*30}")
                    print(f"🧰 Tool Tokens: {tool_tokens}")
                    if tool_tokens > TOOL_RESULT_TOKEN_THRESHOLD and not is_skill_doc_result(result):
                        print(f"⚠️ 此工具回傳約 {tool_tokens} tokens，超過門檻 {TOOL_RESULT_TOKEN_THRESHOLD}，"
                              f"加入上下文時將改用精簡摘要。")
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
                    agent.messages.append({'role': 'user', 'content': f"[tool result]\n{_content_for_context(result, tool_tokens, agent=agent, use_summary=tool_summary_mode)}"})
                    print("♻️ Auto Continue 中...")
                    continue

                # 2. Hybrid Mode
                if hybrid_mode:
                    choice = input("\n🤔 Hybrid Mode - 加入上下文？(y/n): ").lower()
                    if choice == 'y':
                        agent.messages.append({'role': 'user', 'content': f"[tool result]\n{_content_for_context(result, tool_tokens, agent=agent, use_summary=tool_summary_mode)}"})
                        continue
                    else:
                        _append_discarded_tool_result(agent)
                        print("🚫 該結果已被略過 (已告知 Agent 執行結束)")
                        # 這裡不使用 break，讓 AI 根據這個「工具執行完畢」的資訊繼續推論
                        continue

                # 3. Manual Mode
                choice = input("\n是否將系統結果加入上下文？(y/n/stop): ").lower()
                if choice == 'y':
                    agent.messages.append({'role': 'user', 'content': f"【系統執行結果】:\n{_content_for_context(result, tool_tokens, agent=agent, use_summary=tool_summary_mode)}"})
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