import sys
import os
import shlex

def execute(args_str):
    """
    模擬 shell 的 `cd`：把 Agent 傳入的路徑字串解析成目標目錄，
    驗證後回傳一個特殊的 [CWD_CHANGED] 標記，讓上層流程能把它
    追蹤的「目前工作目錄」狀態（訊息中稱為「環境語義單元」）同步
    更新——因為每個指令都是獨立的 subprocess，cd 沒辦法像在真正
    shell 裡一樣自動影響下一個指令，所以才需要這個標記讓外層記住
    新路徑。

    參數解析（args_str，通常是 sys.argv[1:] 用空白 join 回來的原始
    字串）：
        - 空字串／全空白：預設切換到使用者家目錄 (~)。
        - 非空字串：先用 shlex.split 依 shell 引號規則拆成多個
          token，並濾掉字面上等於 "cd" 的 token（防範 Agent 誤把
          整段 "cd path" 當成參數傳進來）；濾完後若沒有剩下任何
          token，視為 "."（目前目錄）；否則取「最後一個」token
          當目標路徑，以容錯 Agent 可能夾帶多餘參數的情況。
        - 若 shlex 解析過程丟例外（例如引號不成對），退回直接用
          args_str.strip() 當整個路徑。

    驗證：把解析出的路徑展開成絕對路徑後，必須「存在且是目錄」
    才算成功；路徑不存在或其實是檔案都視為錯誤。

    回傳：
        成功時回傳兩行：第一行 "[CWD_CHANGED] <絕對路徑>" 是給外層
        程式解析用的狀態同步標記，第二行
        "[PASS] 成功切換至環境語義單元: <絕對路徑>" 是給人看的訊息。
        失敗則回傳單行 "[ERROR] ..."，說明路徑不存在/不是目錄，或
        評估過程中發生例外（例外會被攔截並轉成 [ERROR] 訊息，不會
        往外拋出）。
    """
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