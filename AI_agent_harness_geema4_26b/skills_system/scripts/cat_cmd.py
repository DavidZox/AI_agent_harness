import sys
import os

def execute(file_path):
    if not file_path:
        return "[ERROR] 請指定要讀取的檔案路徑。"
        
    # --- AI Generated Code / CLI Executor ---
    try:
        abs_path = os.path.abspath(os.path.expanduser(file_path))
        
        if not os.path.exists(abs_path):
            return f"[ERROR] 檔案不存在: {file_path}"
        if os.path.isdir(abs_path):
            return f"[ERROR] '{file_path}' 是一個目錄，無法使用 cat 讀取。"
        if not os.path.isfile(abs_path):
            return f"[ERROR] '{file_path}' 不是一般檔案（可能是裝置、socket 或 FIFO），讀取可能無限等待，已拒絕。"
            
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