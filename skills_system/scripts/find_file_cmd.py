import sys
import os
import subprocess
import shlex

def execute(args_str):
    if not args_str or args_str.strip() == "":
        return "[ERROR] 缺少參數。請提供要搜尋的檔案名稱與路徑，例如: \"Modelfile\" ."
    
    # --- AI Generated Code / CLI Executor ---
    try:
        # 使用 shlex 拆分 Agent 丟進來的原始指令字串
        parsed_args = shlex.split(args_str)
        
        # 移除可能不小心混入的 "find" 字眼
        clean_args = [arg for arg in parsed_args if arg.lower() != 'find']
        
        # 解析參數：通常 Agent 會傳入 ["檔名關鍵字", "路徑"] 或 ["路徑", "檔名關鍵字"]
        # 為了讓 Agent 隨便帶參數都能通，我們自動幫它優化成正規的 find 指令
        target_path = "."
        search_pattern = ""
        
        # 簡單的參數辨識邏輯
        for arg in clean_args:
            if arg.startswith('-'):
                continue # 忽略其他複雜的 flag
            elif os.path.isdir(arg) or arg == ".":
                target_path = arg
            else:
                search_pattern = arg
                
        if not search_pattern:
            # 如果分不出來，就拿最後一個當關鍵字
            search_pattern = clean_args[-1] if clean_args else ""
            
        if not search_pattern or search_pattern.strip() == "":
            return "[ERROR] 無法解析搜尋的檔案名稱關鍵字。"

        # 🚨 物理邊界防護：嚴禁對全系統根目錄 '/' 執行搜尋
        if target_path == "/":
            return "[ERROR] 安全邊界阻斷：嚴禁對全系統根目錄 (/) 執行檔案搜尋，這會消耗大量系統資源。請指定具體目錄。"

        # 重新組合標準的 find 指令
        # -iname 代表「不區分大小寫」搜尋檔名，且前後加上 * 符號實現模糊搜尋
        cmd = ["find", target_path, "-iname", f"*{search_pattern}*"]
        
        # 執行 find
        result = subprocess.run(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            timeout=5  # 檔名搜尋很快，5 秒防護綽綽有餘
        )
        
        if result.returncode == 0:
            output = result.stdout.strip()
            if output:
                return f"[PASS] 找到符合的檔案路徑:\n{output}"
            else:
                return f"[PASS] 找不到檔名包含 \"{search_pattern}\" 的檔案。"
        else:
            return f"[ERROR] Find 執行錯誤: {result.stderr.strip()}"
            
    except subprocess.TimeoutExpired:
        return "[ERROR] 搜尋超時！目錄結構可能過於龐大。"
    except Exception as e:
        return f"[ERROR] 執行異常: {str(e)}"
    # -------------------------

if __name__ == "__main__":
    try:
        input_str = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
        print(execute(input_str))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)