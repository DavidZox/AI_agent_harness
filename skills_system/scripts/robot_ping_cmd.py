"""
Generated Script: robot_ping
Description: 檢查 V4.9 機器人核心與語義層狀態
"""
import sys
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
if project_root not in sys.path:
    sys.path.append(project_root)

try:
    from skills.nav_core import NavBrain
except ImportError:
    NavBrain = None

def execute():
    """
    建立 NavBrain 實例並回傳 V4.9 機器人核心狀態字串，用於快速確認核心引擎與
    語義拓撲節點是否對齊正常。

    這是一次靜態狀態回報：只代表 skills.nav_core 模組可以正常載入與呼叫，
    不代表實體機器人硬體當下的即時狀態。

    不接受任何參數。若模組頂層 `from skills.nav_core import NavBrain` 失敗，
    NavBrain 會被設為 None，此時 `NavBrain()` 會因為呼叫 None 而拋出
    TypeError，並在 `__main__` 區塊的 try/except 中被攔截、印成
    "Error executing robot_ping: ..." 並以非 0 狀態碼結束；另外要注意
    `__main__` 會用 `execute(*args)` 呼叫本函式（args 為 CLI 參數），但本
    函式不接受任何位置參數，因此只要呼叫時多帶了任何一個參數，一樣會在這裡
    拋出 TypeError（"execute() takes 0 positional arguments but 1 was
    given"）並被同一個 except 攔截——本工具設計上就是「不需任何參數」，見
    tools/robot_ping.md。

    回傳值：NavBrain.get_v49_status() 回傳的固定描述字串。
    """
    brain = NavBrain()
    return brain.get_v49_status()

if __name__ == "__main__":
    try:
        args = sys.argv[1:]
        print(execute(*args))
    except Exception as e:
        print(f"Error executing robot_ping: {e}", file=sys.stderr)
        sys.exit(1)
