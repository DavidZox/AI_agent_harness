import sys
import os

def execute(temp):
    """
    依馬達溫度數值分級回報運作狀態，用於快速判斷是否需要降速或停機（純數值
    分級運算，不讀取即時感測器）。

    先嘗試型別轉換：僅當 temp 是字串、且不含逗號時，才嘗試 float(temp) 轉型
    （不含逗號的限制是為了避免把「70,80」這種以逗號分隔的多值字串誤當成單一
    數字轉換）；轉型失敗（ValueError）或型別不支援（TypeError）時，直接放棄
    轉型、保留原始輸入值。這代表：若呼叫時傳入無法轉成數字的字串（如 "abc"）
    或含逗號的字串，temp 會維持字串型別進入下方的 `>` 比較，導致從此函式內部
    拋出 TypeError（str 與 int 無法比較），此例外不會被本函式攔截，只能靠
    呼叫端的 try/except 處理（`__main__` 區塊有包，直接呼叫則會往外傳播）。

    門檻分級（temp 需為可比較的數值）：
    > 80 → [CRITICAL] 觸發物理邊界約束，請立即停機；
    >= 60 → [WARNING] 負載異常，建議降速 30%；
    >= 20 → [NORMAL] 運作正常；
    其餘（< 20）→ [NOTICE] 正在進行環境語義單元預熱。

    回傳對應分級的字串（無例外狀況時一定會回傳其中一種，不會回傳 None）。
    """
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
