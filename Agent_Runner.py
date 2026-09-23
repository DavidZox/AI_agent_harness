import os
import sys
import json
import subprocess
import threading
import ollama  # 導入官方庫
import shlex
import time

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
        """渲染成給摘要模型看的純文字：[user]／[assistant] 標頭 + 原文。
        舊版直接把 list 的 repr 塞進 prompt，夾帶 {'role': ...} 與 \\n 轉義，浪費 token 又難讀。"""
        return "\n\n".join(f"[{m['role']}]\n{m['content'].strip()}" for m in msgs)

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
6. 使用繁體中文。"""
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
        try:
            markdown = self._render_summary_markdown(json.loads(raw))
        except (ValueError, TypeError):
            structured = False
            markdown = raw
        get = getattr(res, "get", None)
        meta = {
            'structured': structured,
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
        return file_path

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
        return f"""請先不要執行任何指令。請依照你目前看到的 SKILLS.md 技能索引，
針對下面的任務規劃出所需的步驟清單，列出來讓使用者確認後才會開始執行。

規則：
- 用條列式（1. 2. 3. ...）列出步驟，簡短清楚即可
- 每個步驟盡量標明會用到的技能名稱（來自 SKILLS.md），以及這步要做什麼
- 如果某步驟不需要任何技能，直接說明要做什麼即可
- 這一輪絕對不要輸出 EXECUTE: 指令，只列出計畫，等待使用者確認

任務：
{user_task}
"""

    def build_plan_revision_request(self, feedback):
        """/plan 模式用：使用者對計畫不滿意時，帶著回饋重新規劃一次。規則同上。"""
        return f"""使用者對你剛才列出的計畫有以下修改意見，請依照意見重新規劃一份新的步驟清單。
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
            - 不會自動判斷「計畫已完成」而清除這個區塊；即使所有步驟都做完了，
              這裡仍會持續出現，直到使用者輸入 /plan done 手動清除為止
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

    def _load_skill_doc(self, skill_name):
        """若 skill_name 對應到 tools/<skill_name>.md，回傳其內容；否則回傳 None。
        容忍 AI 直接照抄索引連結而帶上 .md 後綴（例如 list_dir.md）。"""
        if skill_name.endswith(".md"):
            skill_name = skill_name[:-3]
        doc_path = os.path.join(self.tools_dir, f"{skill_name}.md")
        if not os.path.exists(doc_path):
            return None
        with open(doc_path, "r", encoding="utf-8") as f:
            return f.read()

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
                print(f"📖 Agent 選擇技能索引: {raw_token}（載入規格文件，尚未執行）")
                return f"{SKILL_DOC_PREFIX} '{raw_token}' 的規格文件（依此內容才可執行，請使用其中標明的實際腳本路徑）：\n{skill_doc}"

            script_name = self._normalize_script_name(raw_token)
            script_path = os.path.join(self.base_path, "scripts", script_name)

            print(f"🛠️  Agent 啟動工具: {script_name}")
            if not os.path.exists(script_path):
                return f"錯誤：找不到腳本 {script_path}"

            clean_args = self._parse_script_args(script_name, remainder)

            # --- 執行工具 ---
            # 將目前的容器路徑作為環境變數注入，讓 docker_run.py 讀取
            env = os.environ.copy()
            env["CONTAINER_CWD"] = self.container_cwd

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
                output_text = res.stdout.strip()
            else:
                # 腳本異常結束：優先用 stderr，沒有就用 stdout；兩者皆空也要給 AI 一個
                # 明確的失敗訊息，否則空字串會被誤判成「沒有工具需要執行」。
                # 統一補上 [ERROR] 前綴，讓 _content_for_context 能正確判定為失敗。
                output_text = res.stderr.strip() or res.stdout.strip() or "（沒有任何輸出）"
                if not output_text.startswith("[ERROR]"):
                    output_text = f"[ERROR] 腳本 {script_name} 異常結束（exit code {res.returncode}）:\n{output_text}"
            self._sync_state_from_tool_output(output_text)
            return output_text

        except Exception as e:
            return f"解析指令失敗: {e}"

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
            agent.current_plan = plan_msg
            agent.messages.append({
                'role': 'user',
                'content': "[PLAN_CONFIRMED]\n使用者已核准上述計畫，現在開始依計畫執行第一個步驟。"
            })
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
NUM_CTX = int(os.environ.get("AGENT_NUM_CTX", "12288"))

# 整體上下文超過此 token 數時自動壓縮並歸檔（見 compress_context_to_file）。取 NUM_CTX 的 70%，
# 保留約 30% 給模型輸出與下一則使用者／工具訊息，確保壓縮一定發生在 Ollama 靜默截斷之前。
# （舊版寫死 8000 且以「字元÷4」計量，換算成真實 token 約 17000，早已超過 num_ctx，壓縮永遠來不及觸發。）
TOKEN_THRESHOLD = int(NUM_CTX * 0.70)

# 🌊 雙水位線（dual watermark）。硬水位 = 上面的 TOKEN_THRESHOLD（呼叫模型前的最後防線，同步）。
# 軟水位：回合結束後（最終答案已經送給使用者、模型閒著）若上下文超過此值就順手壓縮，
# 切點落在任務邊界，不會把一個任務切成兩半，也不擋在下一次回覆前面（見 after_turn_compression）。
# /parallel_cal on 時改在背景執行緒做（start_background_compression）。
SOFT_TOKEN_THRESHOLD = int(NUM_CTX * 0.50)

# low watermark：壓縮時保留最新這麼多 token 的原文（在訊息邊界切、不拆開 EXECUTE／tool result
# 這一組），其餘與上一份滾動摘要融合成新摘要。舊版固定「保留最新 2 則」，兩則可能只有 50 tokens
# 也可能 1500 tokens，銜接感不穩定。
KEEP_RECENT_TOKENS = int(NUM_CTX * 0.15)

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
    if ctx_before <= SOFT_TOKEN_THRESHOLD or not agent.has_compressible_history():
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
    plan_mode = False  # 👈 開啟後，每個新任務都要先規劃、經使用者核准才會執行

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
            if user_msg.lower() == '/plan on':
                plan_mode = True
                print("📝 已開啟 Plan 模式（新任務會先規劃步驟，經你核准後才會執行）")
                continue
            if user_msg.lower() == '/plan off':
                plan_mode = False
                print("📝 已關閉 Plan 模式（恢復直接執行）")
                continue
            if user_msg.lower() == '/plan done':
                if agent.current_plan:
                    agent.current_plan = None
                    print("✅ 已清除目前進行中的計畫（system prompt 不再提醒 AI 依計畫執行）")
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

            # 記錄這一輪任務最原始的使用者敘述，供獨立摘要 session 在沒有
            # objective／plan 可用時，當作「原始問題」聚焦摘要內容
            # （見 _build_task_anchor_text）
            agent.current_task = user_msg

            # --- 📝 PLAN 模式：先規劃、經使用者核准才進入下面的執行迴圈 ---
            if plan_mode:
                if not _run_plan_flow(agent, user_msg):
                    after_turn_compression(agent, parallel_cal, print)  # 取消也算回合結束
                    continue  # 使用者取消了計畫，回到最上層等待新的輸入
            else:
                agent.messages.append({'role': 'user', 'content': user_msg})

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