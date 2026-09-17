import sys
import os
from datetime import datetime

# 專案根目錄以本檔案位置動態推導 (scripts/ -> skills_system/ -> 專案根目錄)
# 修正舊版問題：舊版用相對路徑 "Memory.md"，會依 Agent 當下模擬的 CWD 而漂移，
# 導致記憶可能寫入非預期的目錄。
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MEMORY_DIR = os.path.join(PROJECT_ROOT, "memory")

# Tier 1：與 ROBOT_AGENT.md 定義的 5 種標準標籤精確比對
CANONICAL_TAGS = {
    "工具使用": "procedural",
    "錯誤執行": "episodic",
    "專案經驗": "episodic",
    "偏好問題": "procedural",  # 屬於「往後應如何應對」的持續性指令，而非單純事實
    "其他": "semantic",
}

# Tier 2：非標準標籤時的關鍵字備援，避免因標籤誤寫而遺失記憶
FALLBACK_KEYWORDS = [
    (("流程", "步驟", "規範", "SOP", "程序", "風格", "溝通", "偏好", "習慣"), "procedural"),
    (("事件", "發生", "錯誤", "失敗", "異常", "經驗"), "episodic"),
]

MEMORY_HEADERS = {
    "semantic": "# Semantic Memory｜語意記憶（概念・事實・規則）\n\n不隨時間變化、可泛化重用的事實、定義、規則與偏好。不強調時間點，強調「是什麼」。\n\n",
    "episodic": "# Episodic Memory｜情節記憶（特定事件・經驗）\n\n特定時間點發生過的事件、錯誤、專案經驗。強調「發生了什麼」。\n\n",
    "procedural": "# Procedural Memory｜程序記憶（操作流程・SOP）\n\n操作步驟、執行順序、溝通風格等「怎麼做」的持續性規則。\n\n",
}


def classify_tag(tag):
    """
    依 ROBOT_AGENT.md「Memory Write Protocol」定義的「問題種類」標籤，決定這筆
    記憶要寫入 semantic（語意）/ episodic（情節）/ procedural（程序）三個長期
    記憶檔案中的哪一個，採兩層判斷：

    第一層（Tier 1，精確比對）：tag 去除頭尾空白後，若精準等於 5 個標準標籤之
    一，直接查表 CANONICAL_TAGS 回傳對應分類——"工具使用" → procedural；
    "錯誤執行"、"專案經驗" → episodic；"偏好問題" → procedural（因為這屬於
    「往後應如何應對」的持續性指令，而非單純事實，不歸類到 semantic）；
    "其他" → semantic。此時 warn 為 None（標籤合法，不需警告）。

    第二層（Tier 2，關鍵字備援）：當 tag 不是上述 5 種標準標籤之一時（例如
    Agent 誤寫了非標準字樣），改用「子字串包含」比對 FALLBACK_KEYWORDS——依序
    檢查 tag 是否含有「流程/步驟/規範/SOP/程序/風格/溝通/偏好/習慣」中任一
    關鍵字，有的話歸類為 procedural；否則檢查是否含「事件/發生/錯誤/失敗/
    異常/經驗」，有的話歸類為 episodic。兩組關鍵字皆有命中可能時，procedural
    那組因為在 FALLBACK_KEYWORDS 中排序在前而優先勝出。這一層無論命中哪一組，
    都會附上 [WARN] 訊息告知標籤非標準、已依關鍵字規則歸類，避免因標籤誤寫而
    讓記憶被誤存卻毫無提示。

    若上述兩層都沒有任何命中，預設歸類為 semantic 並同樣附上 [WARN]，確保
    「未知標籤」永遠有地方可存、不會遺失，只是使用者會被提醒去修正標籤命名。

    參數 tag 為記憶內容開頭、以 "|" 分隔出的「問題種類」欄位（字串）。
    回傳 (memory_type, warn) 二元組：memory_type 為 "semantic"/"episodic"/
    "procedural" 三者之一；warn 在精確比對成功時為 None，其餘情況為對應的
    [WARN] 字串。
    """
    tag = tag.strip()
    if tag in CANONICAL_TAGS:
        return CANONICAL_TAGS[tag], None

    for keywords, mem_type in FALLBACK_KEYWORDS:
        if any(kw in tag for kw in keywords):
            return mem_type, f"[WARN] 標籤 '{tag}' 非標準標籤，已依關鍵字規則歸類為 {mem_type}"

    return "semantic", f"[WARN] 標籤 '{tag}' 非標準標籤，已預設歸類為 semantic"


def execute(memory_content):
    """
    將 Agent 傳入的一段格式化記憶內容（`[問題種類] | [問題描述] | [解決方法或
    結論]`）寫入對應的長期記憶檔案（memory/semantic.md、episodic.md、
    procedural.md 之一），對應 ROBOT_AGENT.md「Memory Write Protocol」定義的
    流程。

    先取 memory_content 中第一個 "|" 之前的部分當作「問題種類」標籤（若整段
    內容沒有 "|"，就把整段內容都當作標籤），交給 classify_tag() 做分類，取得
    目標 memory_type 與可能的 [WARN] 訊息。接著加上 "MM-DD HH:MM" 時間戳記
    組成 formatted_memory，若目標檔案（memory/<memory_type>.md）尚不存在，
    先寫入 MEMORY_HEADERS 中對應的說明性表頭，再以附加（append）模式寫入這筆
    記憶，確保既有記憶不會被覆蓋。

    參數 memory_content 為完整未拆分的原始字串；若為空字串或只有空白，直接
    回傳 [ERROR] 不做任何檔案寫入。

    回傳字串：成功寫入回傳 [PASS] 附目標檔案路徑與寫入內容；若
    classify_tag() 回傳了 warn（標籤非標準 5 種之一），會把 [WARN] 訊息附加
    在 [PASS] 訊息前面一併回傳，而不是視為錯誤中止；檔案 I/O 或其他任何例外
    皆被攔截，回傳 [ERROR] 記憶寫入失敗: ... ，不會讓例外往外傳播。
    """
    try:
        if not memory_content or not memory_content.strip():
            return "[ERROR] 記憶內容不可為空"

        # 取出開頭的「問題種類」標籤（第一個 | 之前的部分）決定歸類目標
        tag = memory_content.split("|", 1)[0].strip() if "|" in memory_content else memory_content.strip()
        mem_type, warn = classify_tag(tag)

        current_time = datetime.now().strftime("%m-%d %H:%M")
        formatted_memory = f"[{current_time}] {memory_content}\n"

        os.makedirs(MEMORY_DIR, exist_ok=True)
        target_file = os.path.join(MEMORY_DIR, f"{mem_type}.md")

        if not os.path.exists(target_file):
            with open(target_file, "w", encoding="utf-8") as f:
                f.write(MEMORY_HEADERS[mem_type])

        # Append 新記憶
        with open(target_file, "a", encoding="utf-8") as f:
            f.write(formatted_memory)

        result = (
            f"[PASS] 成功寫入長期記憶 (memory/{mem_type}.md)\n"
            f"Memory: {formatted_memory.strip()}"
        )
        if warn:
            result = f"{warn}\n{result}"
        return result

    except Exception as e:
        return f"[ERROR] 記憶寫入失敗: {str(e)}"


if __name__ == "__main__":
    try:
        input_str = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
        print(execute(input_str))

    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
