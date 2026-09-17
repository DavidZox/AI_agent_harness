import sys
import os
import subprocess
import shlex

def execute(args_str):
    """
    包裝 `ls -laF` 並執行，用於列出指定路徑（或目前目錄）下的檔案與子目錄清單，
    是最基礎的環境探索工具。

    若 args_str 非空，以 shlex.split 拆解成 token：對 "-" 開頭的 flag token，
    先濾掉其中屬於 l/a/f/F 的字元（因為 base_cmd 已內建 -laF），只有濾除後仍有
    剩餘字元才保留成新 flag（例如 "-la" 會被整個濾掉、"-lh" 會濾成 "-h"），藉此
    避免組出 `ls -laF -la` 這種重複旗標；非 flag 的 token（路徑）則原樣保留。
    最終指令為 base_cmd 加上濾過的 token 清單，執行時有 5 秒逾時保護。

    已知邊界情況：當 args_str 為空或全是空白時，程式改走 else 分支、只設定
    final_cmd，不會建立 clean_args 這個區域變數；但函式稍後為了組出
    display_path 仍會讀取 clean_args[0]，因此「完全不帶參數呼叫」時會觸發
    UnboundLocalError，並被外層 except 攔截，回傳 [ERROR] 執行異常: ...
    （而非預期中列出目前目錄），呼叫時應至少帶入 "." 以避免觸發此路徑。

    回傳字串：ls 執行成功回傳 [PASS] 附目錄列表與判定出的 display_path；ls
    失敗（如路徑不存在或無權限）回傳 [ERROR] 附 stderr 內容；其餘例外（含上述
    UnboundLocalError）一併回傳 [ERROR] 執行異常訊息。
    """
    # 預設的基礎指令組合
    base_cmd = ["ls", "-laF"]
    
    # --- AI Generated Code / CLI Executor ---
    try:
        if args_str and args_str.strip():
            # 使用 shlex.split 安全地拆分字串（例如 "-la /opt" 拆成 ['-la', '/opt']）
            parsed_args = shlex.split(args_str)
            
            # 過濾掉已經內建的重複參數，避免變成 ls -laF -la
            clean_args = []
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