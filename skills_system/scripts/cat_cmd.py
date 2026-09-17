import sys
import os

def execute(file_path):
    """
    以類似 Unix `cat` 的行為讀出單一檔案的完整內容，供 Agent 查看
    設定檔、log 檔等文字內容時使用。

    處理流程與安全防護：
        1. file_path 為空字串/None 時直接回傳 [ERROR]。
        2. 用 os.path.expanduser + os.path.abspath 把路徑正規化
           （處理 ~ 與相對路徑）。
        3. 路徑不存在，或是目錄而非檔案，都回傳對應的 [ERROR]
           （目錄不能用 cat 讀，避免誤用）。
        4. 大小防護：超過 1MB 直接拒絕讀取並提示改用其他過濾
           工具，避免把巨大的 log 檔整個讀進記憶體造成問題。
        5. 以 utf-8 開檔並用 errors='replace'，遇到無法解碼的
           bytes 不會整個丟例外中斷，而是用替代字元顯示，
           確保非純文字/編碼異常的檔案也能印出概略內容。

    參數：
        file_path：要讀取的檔案路徑（可為相對路徑或含 ~ 的路徑）。

    回傳：
        成功時回傳 "[PASS] 檔案內容 (<file_path>):" 加上換行與檔案
        完整內容；任何驗證失敗或讀檔過程中的例外都會回傳對應的
        "[ERROR] ..." 字串（不會讓例外往外拋出）。
    """
    if not file_path:
        return "[ERROR] 請指定要讀取的檔案路徑。"
        
    # --- AI Generated Code / CLI Executor ---
    try:
        abs_path = os.path.abspath(os.path.expanduser(file_path))
        
        if not os.path.exists(abs_path):
            return f"[ERROR] 檔案不存在: {file_path}"
        if os.path.isdir(abs_path):
            return f"[ERROR] '{file_path}' 是一個目錄，無法使用 cat 讀取。"
            
        # 安全邊界防護：避免讀取巨大的 Log 爆記憶體 (限制 1MB)
        file_size = os.path.getsize(abs_path)
        if file_size > 1 * 1024 * 1024:
            return f"[ERROR] 檔案大小 ({file_size} bytes) 超過安全邊界 (1MB)，請改用其他過濾工具。"
            
        with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return f"[PASS] 檔案內容 ({file_path}):\n{content}"
        
    except Exception as e:
        return f"[ERROR] 讀取檔案異常: {str(e)}"
    # -------------------------

if __name__ == "__main__":
    try:
        if len(sys.argv) > 1:
            print(execute(sys.argv[1]))
        else:
            print(f"Usage: {os.path.basename(__file__)} <file_path>")
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)