import sys
import os

def execute(data_list):
    """
    檢查一批數值是否超出安全的物理邊界（上限 100），用於批次驗證感測
    數據／速度等數值，任何一筆超過門檻就要求人工立即介入。

    參數：
        data_list：預期是逗號分隔的數字字串，例如 "12,53,77"
            （通常直接是 sys.argv[1] 的原始字串）。

    型別修正：若傳入的是「不含逗號」的字串，會先嘗試用 float()
    轉型；若轉型失敗（ValueError/TypeError）則忽略，維持原字串。
    注意：這段轉型只在輸入是單一數值字串（無逗號）時觸發，轉型
    成功後 data_list 會變成 float 物件，但接下來的
    `data_list.split(',')` 只有 str 才有 .split 方法——也就是說
    單一數值（無逗號）的輸入實際上會在這裡引發
    AttributeError，並不會被本函式內的 try/except 攔截，而是
    往外拋給呼叫端（本檔 __main__ 的 try/except）處理並印到
    stderr。真正能被此函式正常解析並檢查的輸入，是「至少含一個
    逗號」的字串。

    回傳：
        只要其中任何一個數值大於 100，就回傳
        "[ALERT] 偵測到物理邊界突破，請立即手動介入"；
        全部都在門檻內則回傳 "[PASS] 全數值正常"。
    """
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
