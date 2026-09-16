import sys
import os

def execute(data_list):
    # 物理邊界防護：只有在「不含逗號」且「看起來像數字」時才自動轉型
    try:
        if isinstance(data_list, str) and ',' not in data_list:
            data_list = float(data_list)
    except (ValueError, TypeError):
        pass
    
    # --- AI Generated Code ---
    data_list_float = [float(x) for x in data_list.split(',')]
    for x in data_list_float:
        if x > 100: return "[ALERT] 偵測到物理邊界突破，請立即手動介入"
    return "[PASS] 全數值正常"
    # -------------------------

if __name__ == "__main__":
    try:
        # 確保 sys.argv[1] 是原始字串進入 execute
        if len(sys.argv) > 1:
            print(execute(sys.argv[1]))
        else:
            print(f"Usage: {os.path.basename(__file__)} <data_list>")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
