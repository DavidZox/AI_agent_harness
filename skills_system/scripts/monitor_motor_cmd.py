import sys
import os

def execute(temp):
    # 物理邊界防護：只有在「不含逗號」且「看起來像數字」時才自動轉型
    try:
        if isinstance(temp, str) and ',' not in temp:
            temp = float(temp)
    except (ValueError, TypeError):
        pass
    
    # --- AI Generated Code ---
    if temp > 80: return "[CRITICAL] 觸發物理邊界約束，請立即停機"
    elif temp >= 60: return "[WARNING] 負載異常，建議降速 30%"
    elif temp >= 20: return "[NORMAL] 運作正常"
    else: return "[NOTICE] 正在進行環境語義單元預熱"
    # -------------------------

if __name__ == "__main__":
    try:
        # 確保 sys.argv[1] 是原始字串進入 execute
        if len(sys.argv) > 1:
            print(execute(sys.argv[1]))
        else:
            print(f"Usage: {os.path.basename(__file__)} <temp>")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
