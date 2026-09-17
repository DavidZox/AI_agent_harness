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
    """依「問題種類」標籤決定要寫入 semantic / episodic / procedural 何者。回傳 (memory_type, warn or None)。"""
    tag = tag.strip()
    if tag in CANONICAL_TAGS:
        return CANONICAL_TAGS[tag], None

    for keywords, mem_type in FALLBACK_KEYWORDS:
        if any(kw in tag for kw in keywords):
            return mem_type, f"[WARN] 標籤 '{tag}' 非標準標籤，已依關鍵字規則歸類為 {mem_type}"

    return "semantic", f"[WARN] 標籤 '{tag}' 非標準標籤，已預設歸類為 semantic"


def execute(memory_content):
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
