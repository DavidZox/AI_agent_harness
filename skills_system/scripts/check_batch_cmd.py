import sys
import os

def execute(data_list):
    """
    檢查一批數值是否超出安全的物理邊界（上限 100），用於批次驗證感測
    數據／速度等數值，任何一筆超過門檻就要求人工立即介入。

    參數：
        data_list：逗號分隔的數字字串，例如 "12,53,77"；單一數值（無逗號，
            例如 "50"）也接受，等同於一筆元素的清單
            （通常直接是 sys.argv[1] 的原始字串）。

    修正紀錄：先前這裡有一段「不含逗號就先轉成 float」的型別修正邏輯，會讓
    單一數值輸入在轉型後變成 float 物件，導致下一行 `data_list.split(',')`
    因 float 沒有 .split 方法而拋出 AttributeError（單一數值反而是唯一會
    崩潰的輸入形狀）。這段邏輯與本函式一律用 `.split(',')` 解析輸入的設計
    互斥，已直接移除，改由 `.split(',')` 自行處理——不含逗號的字串
    `.split(',')` 後就是只有一個元素的 list，行為與含逗號時一致，不需要
    額外的型別修正。

    回傳：
        只要其中任何一個數值大於 100，就回傳
        "[ALERT] 偵測到物理邊界突破，請立即手動介入"；
        全部都在門檻內則回傳 "[PASS] 全數值正常"。
    """
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
