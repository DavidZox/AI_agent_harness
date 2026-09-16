import sys
import os
import shlex

def execute(args_str):
    if not args_str or args_str.strip() == "":
        target_path = os.path.expanduser("~")
    else:
        # --- AI Generated Code / CLI Executor ---
        try:
            # 使用 shlex 拆分字串，防範 Agent 把 "cd" 字眼或多行指令混進來
            parsed_args = shlex.split(args_str)
            
            # 過濾掉 "cd" 指令本身（有些 Agent 會輸出成 "cd path" 傳進來）
            clean_args = [arg for arg in parsed_args if arg.lower() != 'cd']
            
            if not clean_args:
                target_path = "."
            else:
                # 拿最後一個參數作為目標路徑（應對多個參數的容錯）
                target_path = clean_args[-1]
                
        except Exception:
            target_path = args_str.strip()

    try:
        # 展開路徑
        abs_path = os.path.abspath(os.path.expanduser(target_path))
        
        if os.path.exists(abs_path) and os.path.isdir(abs_path):
            return f"[CWD_CHANGED] {abs_path}\n[PASS] 成功切換至環境語義單元: {abs_path}"
        else:
            return f"[ERROR] 找不到指定的環境語義單元（路徑不存在或非目錄）: {target_path}"
            
    except Exception as e:
        return f"[ERROR] 路徑評估異常: {str(e)}"
    # -------------------------

if __name__ == "__main__":
    try:
        input_str = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""
        print(execute(input_str))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)