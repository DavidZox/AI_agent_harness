"""modify_memory：把使用者明確要求記住的經驗寫進長期記憶（專案根目錄的 Memory.md）。

Memory.md 每一輪都會透過 SkillAgent.load_long_term_memory() 進入 system prompt 的「Long Term Memory」區塊，
所以寫在這裡的是通用原則、選技能的規則、溝通風格、專案經驗——之後每一次對話都看得到。

用法：
    modify_memory_cmd.py "問題種類 | 問題描述 | 解決方法或結論"
回傳：成功以 [PASS] 開頭、失敗以 [ERROR] 開頭（專案共同慣例）。純本機檔案讀寫，不需要逾時機制。

檔案路徑一律由腳本自身位置推算：腳本執行時的 cwd 是 AI 的虛擬工作目錄（可被 change_dir 改變），
與 harness 安裝位置無關，不可用相對路徑。
"""
import os
import re
import sys
from datetime import datetime

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SCRIPTS_DIR))
MEMORY_FILE = os.path.join(_PROJECT_ROOT, "Memory.md")

# 記憶條目：「[MM-DD HH:MM] 內容」
_ENTRY_RE = re.compile(r"^\[\d{2}-\d{2} \d{2}:\d{2}\]\s*(.*)$")


def existing_entries(path):
    """讀出檔案中所有記憶條目的內容（去掉時間戳），用來判斷重複。"""
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = _ENTRY_RE.match(line.strip())
            if m and m.group(1).strip():
                entries.append(m.group(1).strip())
    return entries


def execute(memory_content):
    try:
        if not memory_content or not memory_content.strip():
            return "[ERROR] 記憶內容不可為空。格式：\"問題種類 | 問題描述 | 解決方法或結論\""
        content = " ".join(memory_content.split())  # 壓成單行，避免多行內容破壞條目格式
        if content in existing_entries(MEMORY_FILE):
            return "[PASS] 相同內容已存在於長期記憶，未重複寫入。"
        now = datetime.now().strftime("%m-%d %H:%M")
        if not os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                f.write("# Long Term Memory\n\n")
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n[{now}] {content}\n")
        return f"[PASS] 成功寫入長期記憶（每輪常駐 system prompt）\nMemory: [{now}] {content}"
    except Exception as e:
        return f"[ERROR] 記憶寫入失敗: {e}"


if __name__ == "__main__":
    try:
        argv = sys.argv[1:]
        # 只有一種寫入目標：帶了選項（例如舊的 --skill）就明確拒絕，不要把選項當成記憶內容寫進去
        if any(a.startswith("--") for a in argv):
            print("[ERROR] modify_memory 沒有任何選項：記憶一律寫入 Memory.md。請只給記憶內容重新執行："
                  "scripts/modify_memory_cmd.py \"問題種類 | 問題描述 | 解決方法或結論\"")
            sys.exit(0)
        print(execute(" ".join(argv).strip()))
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
