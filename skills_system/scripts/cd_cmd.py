import sys
import os
import shlex

def execute(args_str):
    """args_str 可以是參數清單（harness 已拆好）或整串字串（舊介面）。"""
    if isinstance(args_str, list):
        args_list = [a.strip("\"'") for a in args_str if a.strip("\"'")]
        args_str = " ".join(args_list)
    else:
        args_list = None
    if not args_str or args_str.strip() == "":
        target_path = os.path.expanduser("~")
    else:
        # --- AI Generated Code / CLI Executor ---
        try:
            parsed_args = args_list if args_list is not None else shlex.split(args_str)
            
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
            return f"[PASS] 已切換工作目錄: {abs_path}\n[CWD_CHANGED] {abs_path}"
        else:
            return f"[ERROR] 找不到目錄（路徑不存在或不是目錄）: {target_path}"
            
    except Exception as e:
        return f"[ERROR] 路徑評估異常: {str(e)}"
    # -------------------------

if __name__ == "__main__":
    try:
        print(execute(sys.argv[1:]))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)