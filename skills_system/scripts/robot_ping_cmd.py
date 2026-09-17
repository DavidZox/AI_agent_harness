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

    不接受任何參數——本工具設計上就是「不需任何參數」，見 tools/robot_ping.md。

    修正紀錄：先前 `__main__` 會用 `execute(*args)` 呼叫本函式（args 為
    CLI 參數），但本函式簽名不接受任何位置參數，只要呼叫時多帶了任何一個
    參數就會拋出 TypeError（"execute() takes 0 positional arguments but 1
    was given"），只能靠 `__main__` 的 try/except 攔截、印成非標準格式的
    錯誤訊息。現在 `__main__` 一律呼叫 `execute()`、不再轉傳 CLI 參數，
    多餘的參數會被安靜忽略而不是讓工具直接報錯——與系統其他地方「未知輸入
    採取軟性容錯」的一貫原則一致。

    若模組頂層 `from skills.nav_core import NavBrain` 失敗，NavBrain 會被
    設為 None，此時 `NavBrain()` 仍會因為呼叫 None 而拋出 TypeError，並在
    `__main__` 的 try/except 中被攔截、印成 "Error executing robot_ping: ..."
    並以非 0 狀態碼結束——這是環境/部署層級的問題（skills 套件無法載入），
    不屬於本次修正範圍。

    回傳值：NavBrain.get_v49_status() 回傳的固定描述字串。
    """
    brain = NavBrain()
    return brain.get_v49_status()

if __name__ == "__main__":
    try:
        print(execute())
    except Exception as e:
        print(f"Error executing robot_ping: {e}", file=sys.stderr)
        sys.exit(1)
