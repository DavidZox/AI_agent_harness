"""ToolUseIndexMixin：工具使用檢索清單（logs/tool_results/tools_use_index.md）的寫入、讀取、交給獨立 session 的清單與 system prompt 尾端的顯示。"""
import os
import re
import time
from .config import TOOL_USE_INDEX_HINT_MAX, TOOL_USE_INDEX_NAME, TOOL_USE_INDEX_SHOW


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
class ToolUseIndexMixin:

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
