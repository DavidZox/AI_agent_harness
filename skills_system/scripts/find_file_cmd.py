import sys
import os
import subprocess
import shlex

def execute(args_str):
    """
    將 Agent 傳入、格式不固定的原始參數字串轉換成正規的 `find -iname` 指令並執行，
    用於依「檔案名稱」關鍵字做模糊、不分大小寫的搜尋（與搜內容的 grep_cmd 互補，
    本函式完全不檢視檔案內容）。

    Agent 傳入的參數順序不保證（可能是「關鍵字 路徑」或「路徑 關鍵字」），因此不
    假設固定順序：以 shlex.split(args_str) 拆出 token 後，先濾掉不小心夾帶的字面
    "find"，再逐一檢查每個 token——是既存目錄（或字面 "."）就當作 target_path，
    其餘當作 search_pattern；若沒有任何 token 被判定為關鍵字，退而使用最後一個
    token。"-" 開頭的 token 視為其他工具的 flag，直接忽略。

    參數 args_str 為完整未拆分的原始字串（例如 "Modelfile ."）；若為空字串或只有
    空白，或最終解析不出 search_pattern，回傳 [ERROR]。

    安全防護：target_path 精準等於 "/" 時直接阻斷並回傳 [ERROR]，避免對全系統根
    目錄做檔名搜尋而耗盡系統資源；subprocess 執行 find 另有 5 秒逾時保護。

    回傳字串：find 執行成功且有結果回傳 [PASS] 附檔案路徑清單；執行成功但無符合
    檔案一樣回傳 [PASS]（附「找不到」訊息，因為這是正常結果而非錯誤）；find 指令
    本身失敗（非 0 return code）回傳 [ERROR] 附 stderr；逾時或其他例外分別回傳
    對應的 [ERROR] 訊息。
    """
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