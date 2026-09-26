"""ConversationMixin：對話與主模型呼叫——set_current_task（任務線）、/clear、ask_ai（含空白回覆重試）。"""
import ollama
from .config import NUM_CTX, TASK_HISTORY_KEEP
from .protocol import action_text, parse_agent_reply
from .schemas import AGENT_REPLY_SCHEMA


class ConversationMixin:

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
