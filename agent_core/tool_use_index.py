"""ToolUseIndexMixin：工具使用檢索清單（logs/tool_results/tools_use_index.md）的寫入、讀取、交給獨立 session 的清單，
以及 system prompt 裡的兩區顯示：工具回傳與附圖分析（尾端）、過去的對話片段（被壓縮的對話原文存檔）。"""
import os
import re
import time
from .config import (
    CONVERSATION_ARCHIVE_SCRIPT,
    CONVERSATION_ARCHIVES_SHOW,
    TOOL_USE_INDEX_NAME,
    TOOL_USE_INDEX_SHOW,
    VISION_ARCHIVE_SCRIPT,
)


_TRAILING_ELLIPSIS_RE = re.compile(r"(\s*(…|\.{3,}))+$")


def tidy_hint(text):
    """檢索清單描述的整理：只把換行與連續空白收成一個空格、去掉模型自己加在結尾的「…」，不截斷。
    以前 clip_hint 超過 50 字就在標點處切掉加「…」，實測切掉的正好是後半段的名稱與結論（例如對話片段只剩
    「本次對話的重點是…」），main session 就認不出這筆跟現在的問題有關；長度改由 prompt 要求模型寫精簡。"""
    return _TRAILING_ELLIPSIS_RE.sub("", " ".join(str(text or "").split())).strip()


# 存檔的三種來源：工具回傳、附圖的視覺分析、被壓縮的對話片段（交給獨立 session 的清單用這些名稱標種類）
KIND_LABELS = {"tool": "工具回傳", "vision": "附圖分析", "conversation": "過去的對話片段"}
VISION_MARK = "〔附圖分析〕"   # system prompt 的工具使用檢索清單裡，附圖分析那幾行的開頭


def result_kind(filename):
    """存檔（或檢索清單的一行）是哪一種：看檔名的腳本欄（<session>_<編號>_<腳本>.md），不另外加欄位，舊的清單行也分得出來。"""
    name = str(filename or "").strip()
    if name.endswith(f"_{CONVERSATION_ARCHIVE_SCRIPT}.md"):
        return "conversation"
    if name.endswith(f"_{VISION_ARCHIVE_SCRIPT}.md"):
        return "vision"
    return "tool"



class ToolUseIndexMixin:

    def _tool_use_index_block(self):
        """system prompt 尾端的『工具使用檢索清單』：讀 tools_use_index.md 最近 TOOL_USE_INDEX_SHOW 行組成。
        這份檔案只有觸發過任務導向擷取（summarize_tool_result／_result_recall）的回傳才有一筆，不是每次工具
        呼叫都有；檔案本身不裁剪、跨 session（甚至跨這支程式的重新啟動）持續累加，這裡只裁「顯示視窗」——
        目的是讓再久以前的工具回傳，只要現在的問題看起來有關，都有機會被發現，而不必模型自己記得存檔編號。
        每次組 system prompt 都重新讀檔案、重新塞入，不會被滑動視窗或壓縮摘要沖掉；只在多一筆時才變，KV cache 大多命中
        （最新一筆另外在動態區提示一次，見 tool_use_index_pointer）。說明文字依上下文模式不同：harness 模式照獨立 session 的
        判斷下指令，claude_code 模式只列線索、怎麼取回由模型決定。TOOL_USE_INDEX_SHOW<=0 時關閉（環境變數可調／可關）。"""
        rows = self._tool_use_index_rows(kind="tool")
        if not rows:
            return ""
        rows_text = "\n".join(f"- #{rid}（{ts}）：{hint}" for rid, ts, hint in rows)
        if getattr(self, "context_mode", "harness") == "claude_code":
            # claude_code 模式：清單只是線索（程式寫的「當時的問題＋指令」），怎麼取回、要不要取回由模型決定
            return f"""
            ## 工具使用檢索清單（Tool Use Index）
            以下每筆是過去某次工具回傳的存檔（可能是很早之前、甚至上次啟動的）：當時使用者的問題與執行的指令，最上面最新；
            開頭標〔附圖分析〕的是使用者附圖時系統的影像分析（原圖沒有保存，只有這份文字）。原文不在這裡：
            {rows_text}

            需要舊資料時由你決定怎麼取回：看某一段用 result_view <編號>、找某個字串用 result_grep <編號> "<關鍵字>"、
            要依問題整理一大份內容用 result_recall <編號> "<問題>"（交給獨立 session，只回重點）。
            問的是「現在」「目前」的狀態 → 重新執行工具；跟清單都無關 → 當作新問題。
            """
        return f"""
            ## 工具使用檢索清單（Tool Use Index）
            以下每筆是過去某次工具回傳的存檔（可能是很早之前、甚至上次啟動的）跟當時問題的關聯描述，最上面最新；開頭標〔附圖分析〕的
            是使用者附圖時系統的影像分析（原圖沒有保存，只有這份文字，追問一樣用 result_recall）。只是線索，原文不在這裡：
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

    def _conversation_index_block(self):
        """system prompt「過去的對話片段」一節：被壓縮的對話原文存檔，每筆附 segment_hint（這一段談了什麼、決定了什麼）。
        跟工具回傳分開列、名額獨立（CONVERSATION_ARCHIVES_SHOW），不會被大量工具回傳擠出顯示視窗；問的是「之前討論過／
        說過什麼」時找這裡，問「之前查到的東西」時找工具回傳那區。編號跟工具回傳同一個序列，取回一樣用 result_recall。
        滾動摘要記的片段編號（conversation_archives）若不在清單裡（例如寫清單失敗），也列出來，只是沒有描述。"""
        rows = self._tool_use_index_rows(kind="conversation", limit=CONVERSATION_ARCHIVES_SHOW)
        listed = {rid for rid, _, _ in rows}
        extra = [i for i in reversed(getattr(self, "conversation_archives", None) or []) if i not in listed]
        extra = extra[:max(0, CONVERSATION_ARCHIVES_SHOW - len(rows))]
        if not rows and not extra:
            return ""
        lines = [f"- #{rid}（{ts}）：{hint}" for rid, ts, hint in rows] + [f"- #{rid}：（沒有描述）" for rid in extra]
        return ("## 過去的對話片段（被壓縮的對話原文存檔，最上面最新）\n"
                "以下每筆是一段已經被壓縮掉的對話（使用者說的話、你當時的回覆與當時的工具回傳），描述是那一段談了什麼、"
                "決定了什麼；原文不在這裡：\n" + "\n".join(lines) + "\n"
                "使用者問的是之前「討論過／說過／決定過」什麼，而上面的摘要與「使用者說過的話」沒有寫到那個細節時，"
                "執行 result_recall <編號> \"<使用者這句話的原文>\" 取回那一段；問的是之前查到的資料，看工具使用檢索清單。")

    def tool_use_index_pointer(self):
        """送出內容最尾端（動態區）的一行：檢索清單最新一筆。完整清單留在 system prompt（一千多 tokens、很少變，放那裡
        KV cache 才划算）；但對話一長，system prompt 的尾端就落在整段上下文的中間，模型容易忽略，這一行把它拉回眼前。"""
        rows = self._tool_use_index_rows(kind="tool")
        if not rows:
            return ""
        rid, ts, hint = rows[0]
        return (f"工具使用檢索清單（完整的在 system prompt 尾端，共 {len(rows)} 筆）最新一筆：#{rid}（{ts}）：{hint}。"
                f"使用者這句話若在接續清單裡的某一筆，照清單的規則取回原文再回答。")

    def _index_entries(self, exclude=(), kinds=("tool", "vision"), limit=None):
        """tools_use_index.md 最近 limit 筆（預設 TOOL_USE_INDEX_SHOW；新→舊）→ [{"id", "ts", "hint", "kind"}]，同一編號只留
        最新一筆；kinds 決定要哪幾種（result_kind），各自計名額。exclude 排除特定編號（例如正在被摘要或被 recall 的那一筆自己）。"""
        limit = TOOL_USE_INDEX_SHOW if limit is None else limit
        if limit <= 0:
            return []
        try:
            with open(os.path.join(self.results_dir(), TOOL_USE_INDEX_NAME), encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
        except OSError:
            return []
        excluded = {str(x).lstrip("#") for x in exclude if x}
        entries, seen = [], set()
        for ln in reversed(lines):
            parts = ln.split(" | ", 4)
            rid = parts[0].strip().lstrip("#") if len(parts) >= 5 else ""
            if not rid.isdigit() or rid in seen or rid in excluded:
                continue
            seen.add(rid)
            kind = result_kind(parts[3])
            if kind not in kinds:
                continue
            entries.append({"id": int(rid), "ts": parts[1].strip(), "hint": parts[4].strip(), "kind": kind})
            if len(entries) >= limit:
                break
        return entries

    def _tool_use_index_rows(self, exclude=(), kind="tool", limit=None):
        """system prompt 顯示用的 [(編號, 時間, 描述)]：kind="tool" 是工具使用檢索清單那一區（工具回傳與附圖分析混在一起、
        依時間排，附圖分析的描述前面加〔附圖分析〕）；"conversation" 是過去的對話片段那一區；None 是全部。
        system prompt 的兩區（_tool_use_index_block、_conversation_index_block）與動態區的最新一筆共用。"""
        kinds = {"tool": ("tool", "vision"), "conversation": ("conversation",)}.get(kind, ("tool", "vision", "conversation"))
        return [(e["id"], e["ts"], (VISION_MARK if e["kind"] == "vision" else "") + e["hint"])
                for e in self._index_entries(exclude, kinds, limit)]

    def _tool_use_catalog(self, exclude=()):
        """交給獨立 session 的檢索清單文字與可接受的編號集合（_validate_related 用）；清單空時回 ("", set())。"""
        entries = self._index_entries(exclude, ("tool", "vision"))
        entries += self._index_entries(exclude, ("conversation",), CONVERSATION_ARCHIVES_SHOW)
        lines = [f"#{e['id']}（{e['ts']}，{KIND_LABELS[e['kind']]}）：{e['hint']}" for e in entries]
        return "\n".join(lines), {e["id"] for e in entries}

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
        四個地方會呼叫：summarize_tool_result 處理「原始工具」的大量回傳（result_* 這類看舊存檔的衍生輸出不記，見
        DERIVED_RESULT_SCRIPTS）、claude_code 模式的大量原文（raw_result_for_context）、壓縮時存下的對話片段
        （_archive_conversation_segment，描述是 segment_hint）、附圖的視覺分析（archive_vision_result，描述是視覺模型寫的
        index_hint）。永遠 append、不受 _prune_tool_results 影響，即使原始檔
        之後被清掉也留著當歷史軌跡；system prompt 依種類分兩區各顯示最近幾筆（_tool_use_index_block、_conversation_index_block）。沒有 result_id、拿不到檔名、或
        模型沒給出 index_hint 時不寫——沒檔名代表原文根本沒存成功，寫了也是死線索。描述整句照存、不截斷（tidy_hint）。
        檔名只是給人看／除錯用，模型只需要抄編號（編號從啟動時的最大存檔編號續編，跨 session 不重複）。"""
        hint = tidy_hint(index_hint)
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
