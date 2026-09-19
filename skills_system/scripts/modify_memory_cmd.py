import sys
import os
from datetime import datetime

# 固定指向 harness 專案根目錄下的 Memory.md，
# 不可用相對路徑：腳本執行時的 cwd 是 AI 的虛擬工作目錄（可被 change_dir 改變），
# 與 harness 自身安裝位置無關。
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_SCRIPTS_DIR))
MEMORY_FILE = os.path.join(_PROJECT_ROOT, "Memory.md")

def execute(memory_content):
    try:
        # 檢查輸入
        if not memory_content or not memory_content.strip():
            return "[ERROR] 記憶內容不可為空"

        # 取得目前時間
        current_time = datetime.now().strftime("%m-%d %H:%M")

        # 建立格式化記憶
        formatted_memory = f"\n[{current_time}] {memory_content}\n"

        # 如果 Memory.md 不存在則建立
        if not os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                f.write("# Long Term Memory\n\n")

        # Append 新記憶
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(formatted_memory)

        return (
            "[PASS] 成功寫入長期記憶\n"
            f"Memory: {formatted_memory.strip()}"
        )

    except Exception as e:
        return f"[ERROR] 記憶寫入失敗: {str(e)}"


if __name__ == "__main__":
    try:
        input_str = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
        print(execute(input_str))

    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)