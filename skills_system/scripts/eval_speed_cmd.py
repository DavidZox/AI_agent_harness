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

def execute(speed=0.0):
    """
    建立一個 NavBrain（導航/風險評估核心，定義於
    skills/nav_core.py）實例，並用它評估在給定速度下的風險程度，
    是把「速度」這個單一數值交給導航大腦做安全判斷的簡單包裝。

    參數：
        speed：要評估的速度數值，預設 0.0。透過本檔 CLI 進入點呼叫
            時，sys.argv 裡的參數一律是字串，所以實際傳入
            calculate_risk 的可能是字串而非 float，是否轉型由
            NavBrain.calculate_risk 內部負責。

    回傳：
        brain.calculate_risk(speed) 的回傳值（實際型別/內容由
        NavBrain 的實作決定，本函式不做任何加工）。

    注意：檔案開頭若因為找不到 skills.nav_core 模組而 import
    失敗，NavBrain 會被設成 None，這裡的 `NavBrain()` 會因此
    拋出 TypeError（'NoneType' object is not callable），此例外
    不會在本函式內被攔截，而是交由 __main__ 區塊的 try/except
    捕捉並印到 stderr。
    """
    brain = NavBrain()
    return brain.calculate_risk(speed)

if __name__ == "__main__":
    try:
        args = sys.argv[1:]
        print(execute(*args))
    except Exception as e:
        print(f"Error executing eval_speed: {e}", file=sys.stderr)
        sys.exit(1)
