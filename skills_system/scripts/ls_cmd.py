import sys
import os
import subprocess
import shlex

def execute(args_str):
    # 預設的基礎指令組合
    base_cmd = ["ls", "-laF"]
    clean_args = []

    # --- AI Generated Code / CLI Executor ---
    try:
        if args_str and args_str.strip():
            # 使用 shlex.split 安全地拆分字串（例如 "-la /opt" 拆成 ['-la', '/opt']）
            parsed_args = shlex.split(args_str)

            # 過濾掉已經內建的重複參數，避免變成 ls -laF -la
            for arg in parsed_args:
                if arg.startswith('-'):
                    # 提取非重複的參數字元（例如如果輸入 -la，我們只補上不重複的，或直接跳過）
                    # 這裡為了彈性，如果使用者輸入了 -h 等新參數則保留，若是 -la 或 -l 則忽略
                    stripped = arg.lstrip('-')
                    remaining = "".join([c for c in stripped if c not in "lafF"])
                    if remaining:
                        clean_args.append(f"-{remaining}")
                else:
                    clean_args.append(arg)
            
            # 組合最終指令
            final_cmd = base_cmd + clean_args
        else:
            final_cmd = base_cmd

        # 執行指令
        result = subprocess.run(
            final_cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            timeout=5
        )
        
        # 取得目前顯示的相對或絕對路徑名稱
        display_path = clean_args[0] if (clean_args and not clean_args[0].startswith('-')) else "."
        
        if result.returncode == 0:
            return f"[PASS] 目錄列表 ({display_path}):\n{result.stdout}"
        else:
            return f"[ERROR] 無法讀取目錄: {result.stderr.strip()}"
            
    except Exception as e:
        return f"[ERROR] 執行異常: {str(e)}"
    # -------------------------

if __name__ == "__main__":
    try:
        # 將 sys.argv[1:] 後面所有的參數重新用空白接起來處理
        input_str = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
        print(execute(input_str))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)