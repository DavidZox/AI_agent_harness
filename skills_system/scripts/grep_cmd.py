import sys
import os
import subprocess
import shlex

def execute(args_str):
    """
    將 Agent 傳入的關鍵字/路徑參數字串轉成正規的 `grep` 指令並執行，用於在檔案
    「內容」中搜尋關鍵字（與只比對檔名的 find_file_cmd 互補）。

    先以 shlex.split(args_str) 拆解，並濾掉使用者可能不小心夾帶的字面 "grep"。
    接著檢查已拆出的參數中是否已包含 -r（遞迴）/-n（顯示行號）/-i（忽略大小寫）/
    -I（略過二進位檔，注意是大寫 I）這幾個 flag，缺哪個就自動補上——特別是 -I，
    用意是遇到 Miniconda 等巨大二進位檔時能直接跳過內容比對，避免掃描逾時；
    使用者自行帶的 flag 一律保留、不會被覆蓋或去除。

    參數 args_str 為完整未拆分的原始字串（例如 "model ."）；若為空字串或只有
    空白，直接回傳 [ERROR]。

    安全防護：組好完整指令陣列後，逐一檢查其中每個「非 flag」參數，只要有任何
    一個精準等於 "/"（代表搜尋路徑被設成全系統根目錄），就阻斷並回傳 [ERROR]，
    因為對 / 做遞迴內容搜尋可能拖垮系統；執行時另有 8 秒逾時保護。

    回傳值：grep 的 return code 0（找到相符內容）與 1（grep 定義的「沒找到」，
    屬正常結果而非錯誤）都視為成功，分別回傳 [PASS] 附完整搜尋結果，或 [PASS]
    附「找不到」訊息；其餘 return code 視為 grep 本身出錯，回傳 [ERROR] 附
    stderr 內容；逾時或其他例外分別回傳對應的 [ERROR] 訊息。
    """
    if not args_str or args_str.strip() == "":
        return "[ERROR] 缺少參數。請提供關鍵字與路徑，例如: \"model\" ."
    
    # --- AI Generated Code / CLI Executor ---
    try:
        # 使用 shlex 拆分 Agent 丟進來的原始指令字串
        parsed_args = shlex.split(args_str)
        
        # 移除可能不小心混入的 "grep" 字眼
        clean_args = [arg for arg in parsed_args if arg.lower() != 'grep']
        
        # 預設參數防護：檢查是否有相關 flag
        has_r = any(arg.startswith('-') and 'r' in arg for arg in clean_args)
        has_i = any(arg.startswith('-') and 'i' in arg for arg in clean_args)
        has_n = any(arg.startswith('-') and 'n' in arg for arg in clean_args)
        has_I = any(arg.startswith('-') and 'I' in arg for arg in clean_args) # 檢查大寫 I
        
        # 組合需要的 flags 
        # 💡 強制補上 -I，遇到 Miniconda 等巨大二進位檔直接跳過，防超時
        flags = "-"
        if not has_r: flags += "r"
        if not has_n: flags += "n"
        if not has_i: flags += "i"
        if not has_I: flags += "I" 
        
        # 重新組合指令
        cmd = ["grep"]
        if flags != "-":
            cmd.append(flags)
            
        # 將過濾後的參數補上
        cmd.extend(clean_args)
        
        # 🚨 物理邊界防護：升級成更嚴格的路徑檢查
        # 嚴禁對系統根目錄 '/' 執行遞迴搜尋，這會導致系統崩潰
        for arg in cmd:
            # 如果參數不是以 '-' 開頭（代表它是關鍵字或路徑），且它精準等於 '/'
            if not arg.startswith('-') and arg == "/":
                return "[ERROR] 安全邊界阻斷：嚴禁對全系統根目錄 (/) 執行遞迴搜尋，這會導致系統崩潰。請指定具體目錄（如 '.' 或 './skills_system'）。"

        # 執行 Grep
        result = subprocess.run(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            timeout=8  # 限制 8 秒超時防護
        )
        
        if result.returncode == 0:
            return f"[PASS] 搜尋結果:\n{result.stdout}"
        elif result.returncode == 1:
            return f"[PASS] 找不到符合該關鍵字的內容。"
        else:
            return f"[ERROR] Grep 執行錯誤: {result.stderr.strip()}"
            
    except subprocess.TimeoutExpired:
        return "[ERROR] 搜尋超時！可能是因為搜尋範圍過大或不小心掃描到大型目錄，請縮小搜尋範圍。"
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