"""ContextModeMixin：兩種上下文管理模式的切換，以及 claude_code 模式專屬的行為。

- harness（原本的設計）：harness 替模型做決定——大量工具回傳交給獨立 session 依問題擷取重點、附下一步建議。
- claude_code：相信模型——工具回傳的原文直接進上下文（raw_result_for_context，太大時保留頭尾＋存檔編號），
  要查細節、要不要把一大份原文交給獨立 session 提煉（result_recall），由模型自己決定；harness 只提供安全網：
  截斷、上下文超過水位時先清除舊的工具回傳（clear_old_tool_results，原文在存檔、可還原），不夠才滾動摘要。
各模式給模型的規則文字在 context_modes/<模式>.md（get_system_prompt 依目前模式載入）。"""
import os
import re
from .config import (
    CLEAR_KEEP_RECENT_RESULTS,
    CLEAR_MIN_TOKENS,
    CONTEXT_MODES,
    CONTEXT_MODES_DIRNAME,
    DERIVED_RESULT_SCRIPTS,
    RAW_RESULT_MAX_TOKENS,
    TOOL_CLEARED_TAG,
    TOOL_RESULT_FRAME,
    TOOL_RESULT_TOKEN_THRESHOLD,
    TOOL_TRUNCATED_TAG,
)
from .tool_use_index import clip_hint


CONTEXT_MODE_DESCRIPTIONS = {
    "harness": "harness 替模型做決定：超過門檻的工具回傳交給獨立 session 依問題擷取重點，主對話收到重點與下一步建議",
    "claude_code": ("相信模型：工具回傳原文直接進上下文（超過上限保留頭尾＋存檔編號），要查細節、要不要委派獨立 session，"
                    "都由模型自己決定；上下文超過水位時先清除舊回傳（原文在存檔），不夠才滾動摘要"),
}
_ARCHIVE_REF = re.compile(r"完整原文存檔 #(\d+)")
_MODE_ALIASES = {"cc": "claude_code", "claude": "claude_code", "claudecode": "claude_code", "h": "harness"}


class ContextModeMixin:

    def set_context_mode(self, mode):
        """切換上下文模式（/context_mode）。回傳正規化後的模式名稱；不認得的名稱 raise ValueError。
        已經在上下文裡的內容不會回頭改寫：之後的工具回傳才照新模式處理。"""
        name = str(mode or "").strip().lower().replace("-", "_")
        name = _MODE_ALIASES.get(name, name)
        if name not in CONTEXT_MODES:
            raise ValueError(f"不認得的上下文模式「{mode}」，可選：{'、'.join(CONTEXT_MODES)}")
        self.context_mode = name
        return name

    def context_mode_rules(self):
        """context_modes/<模式>.md 的內容（接在 AGENT.md 後面進 system prompt）；檔案不在時回空字串。"""
        path = os.path.join(self.script_dir, CONTEXT_MODES_DIRNAME, f"{self.context_mode}.md")
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def _trajectory_record(self, result_id):
        return next((r for r in reversed(self.trajectory) if r.get("id") == result_id), None)

    # ---------------------------------------------------------------- claude_code：原文進上下文
    def raw_result_for_context(self, result, tool_tokens):
        """claude_code 模式下工具回傳怎麼進主對話：第一行註明存檔編號；不超過 RAW_RESULT_MAX_TOKENS 就是完整原文，
        超過就保留頭尾（6:4），中間換成一行「省略多少、原文在哪、用什麼查」。不呼叫任何模型。
        大量回傳（> TOOL_RESULT_TOKEN_THRESHOLD）照樣記進檢索清單，只是描述由程式組（使用者的問題＋執行的指令），
        不是 harness 模式那種獨立 session 寫的語意描述。"""
        rid = self.last_result_id
        if not rid:
            return result
        record = self._trajectory_record(rid) or {}
        if tool_tokens > TOOL_RESULT_TOKEN_THRESHOLD and record.get("script") not in DERIVED_RESULT_SCRIPTS:
            what = f"{record.get('skill') or record.get('script') or ''} {' '.join(record.get('args') or [])}".strip()
            hint = clip_hint(clip_hint(self.current_task or "（沒有使用者訊息）", 26) + "：" + clip_hint(what, 22))
            self._append_tool_use_index(rid, self.last_result_file, hint)
        header = f"（完整原文存檔 #{rid}，約 {tool_tokens} tokens）"
        if tool_tokens <= RAW_RESULT_MAX_TOKENS:
            return f"{header}\n{result}"
        max_chars = int(RAW_RESULT_MAX_TOKENS * self.chars_per_token)
        # 在行邊界切（存檔的行號＝原文的行號，result_view 可以直接用）：頭留到第 head_last 行，尾從第 tail_first 行開始
        lines = result.split("\n")
        head, used = [], 0
        while len(head) < len(lines) and used + len(lines[len(head)]) + 1 <= max_chars * 0.6:
            used += len(lines[len(head)]) + 1
            head.append(lines[len(head)])
        tail, used = [], 0
        while len(head) + len(tail) < len(lines) and used + len(lines[-1 - len(tail)]) + 1 <= max_chars * 0.4:
            used += len(lines[-1 - len(tail)]) + 1
            tail.insert(0, lines[-1 - len(tail)])
        if head:
            first, last = len(head) + 1, len(lines) - len(tail)
            where, view_from = f"中間第 {first}～{last} 行", first
        else:   # 第一行就超過篇幅（單行的巨大輸出）：退回字元切
            head, tail = [result[:int(max_chars * 0.6)]], [result[-int(max_chars * 0.4):]]
            where, view_from = "中間（單行過長）", 1
        omitted = max(0, tool_tokens - self.count_tokens("\n".join(head + tail)))
        # 實測（評測 fleet_battery）：只寫「中間省略、原文在存檔」時，4B 模型看到頭尾沒有 tb6 就回答「資料裡沒有 tb6」，
        # 不會回存檔查。所以明講省略的行號範圍、看不到不代表不存在、以及問題要的東西不在頭尾時先查再答。
        gap = (f"\n…（{where}省略，約 {omitted} tokens。上面看不到的內容不代表不存在：使用者問的東西不在頭尾時，"
               f"先查存檔 #{rid} 再回答——找字串用 result_grep {rid} \"<關鍵字>\"、看這一段用 result_view {rid} --from {view_from} --lines 80、"
               f"要依問題整理這一大份內容用 result_recall {rid} \"<問題>\"）…\n")
        return f"{TOOL_TRUNCATED_TAG}\n{header}\n" + "\n".join(head) + gap + "\n".join(tail)

    # ---------------------------------------------------------------- claude_code：清除舊的工具回傳（可還原）
    def clear_old_tool_results(self, keep_recent=None):
        """把較舊的工具回傳換成一行佔位（「原文在存檔 #N，需要時怎麼取回」），回傳騰出的 token 數（估算）。
        只清 claude_code 格式、帶「完整原文存檔 #N」的回傳（清了還能還原），最新 keep_recent 則與小於 CLEAR_MIN_TOKENS
        的不清。第一行 [tool result] 與框架句保留（壓縮切點、HARNESS_MARKERS 都認它）。
        這是 Anthropic 所說最輕量的壓縮（tool result clearing）：不呼叫模型、可還原。代價是從被清的那一則開始
        KV cache 失效，所以只在超過水位時做（見 ensure_context_budget／after_turn_compression），不是每輪都做。"""
        keep = CLEAR_KEEP_RECENT_RESULTS if keep_recent is None else keep_recent
        freed, cleared = 0, 0
        with self.messages_lock:
            idxs = [i for i, m in enumerate(self.messages) if i > 0 and self._is_tool_result_message(m)]
            for i in idxs[:len(idxs) - keep] if keep > 0 else idxs:
                content = self.messages[i]['content']
                if TOOL_CLEARED_TAG in content:
                    continue
                m = _ARCHIVE_REF.search(content)
                tokens = self.count_tokens(content)
                if not m or tokens < CLEAR_MIN_TOKENS:
                    continue
                rid = int(m.group(1))
                lines = content.splitlines()
                frame = lines[1] if len(lines) > 1 and lines[1].startswith(TOOL_RESULT_FRAME) else ""
                tail = lines[-1] if lines and lines[-1].startswith(TOOL_RESULT_FRAME) else ""
                record = self._trajectory_record(rid) or {}
                what = record.get("command") or record.get("script") or ""
                body = (f"{TOOL_CLEARED_TAG}\n這則舊的工具回傳（約 {tokens} tokens）已從上下文清除以節省空間；完整原文仍在存檔 #{rid}"
                        + (f"（當時執行：{what}）" if what else "")
                        + f"。需要時用 result_view {rid}、result_grep {rid} \"<關鍵字>\" 或 result_recall {rid} \"<問題>\" 取回，"
                          f"不要重新執行同一個工具。")
                new = "\n".join(x for x in ("[tool result]", frame, body, tail) if x)
                self.messages[i] = {'role': self.messages[i]['role'], 'content': new}
                freed += tokens - self.count_tokens(new)
                cleared += 1
        if cleared:
            self.last_clearing = {"results": cleared, "tokens": freed}
            self._history_rewritten = True   # perf.jsonl：下一次主對話呼叫的 KV cache 會從被清的那一則開始失效
        return freed
