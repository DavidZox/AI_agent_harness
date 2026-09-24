import sys
import os
import subprocess
import shlex

def execute(args_str):
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
            hits = [ln for ln in result.stdout.splitlines() if ln.strip()]
            targets = [a for a in clean_args if not a.startswith('-')][1:]   # 第一個非旗標參數是關鍵字，其餘是路徑
            if targets and all(os.path.isfile(t) for t in targets):
                n_files = len(targets)           # 只搜單一檔案時 grep 不印檔名前綴，命中行數不能當檔案數
            else:
                n_files = len({ln.split(":", 1)[0] for ln in hits})
            return f"[PASS] 搜尋結果：共 {len(hits)} 筆命中，分布在 {n_files} 個檔案:\n{result.stdout}"
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