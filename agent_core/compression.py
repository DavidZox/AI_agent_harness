"""CompressionMixin：上下文壓縮——雙水位線、滾動融合摘要、背景壓縮。"""
import json
import ollama
import os
import threading
import time
from .config import (
    HARNESS_MARKERS,
    KEEP_RECENT_TOKENS,
    NUM_CTX,
    SUMMARY_ARCHIVE_KEEP,
    SUMMARY_MAX_CHARS,
    SUMMARY_MAX_PREDICT,
    TOKEN_THRESHOLD,
    TOOL_RESULT_FRAME,
    TOOL_RESULT_PREFIXES,
)
from .protocol import action_text, parse_agent_reply
from .schemas import SUMMARY_SCHEMA


class CompressionMixin:

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
