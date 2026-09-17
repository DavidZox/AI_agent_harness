import sys
import subprocess
import os

def run_in_container(target, command):
    """
    在指定的 Docker 容器內執行一段任意 shell 指令，並讓輸出的最後
    一行帶出指令執行完之後容器內的當前路徑，用來讓上層持續追蹤
    「容器目前工作目錄」的狀態——因為每次呼叫都是獨立的 docker
    exec/subprocess，容器內下 cd 換目錄的效果不會留到下一次呼叫，
    只能靠回傳內容最後一行的 pwd 結果，讓外層自行記住並在下一次
    組指令時帶回去（原始碼註解中把這個概念稱為 [CONTAINER_CWD]，
    但實際上函式並不會在輸出裡插入字面上的 "[CONTAINER_CWD]"
    字串，單純是讓 pwd 的結果自然成為輸出的最後一行）。

    作法：把使用者的 command 接上 `&& pwd`（用 && 串接，所以只有在
    command 本身執行成功時 pwd 才會被跑、才會出現在輸出裡；command
    若失敗，整體 returncode 會是非 0，直接落入下面的錯誤分支），
    整段交給 `docker exec <target> bash -c "..."` 執行。因為是用
    bash -c 執行整個字串，command 裡可以包含管線、重導向、&&、;
    等 shell 語法，但相對地也完全不做跳脫或過濾——這是刻意的設計，
    讓 Agent 能對容器下任意組合指令，而不是每次只能跑單一程式。

    參數：
        target：目標 Docker 容器名稱。
        command：要在容器內執行的 shell 指令字串（可以是組合指令）。

    回傳：
        成功（returncode == 0）：回傳去除頭尾空白的 stdout，內容是
        command 本身的輸出，加上最後一行的新工作目錄。
        失敗：回傳 "[ERROR] 執行失敗: <stderr>"。
        執行過程中的例外：回傳 "[ERROR] 異常: <例外內容>"，不會
        往外拋出。
    """
    # 確保我們鎖定的是正確的容器名稱
    container_name = f"{target}"
    
    # 執行指令並同步路徑狀態
    # 1. 進入目標目錄
    # 2. 執行使用者命令
    # 3. 取得執行後的當前目錄 [CONTAINER_CWD]
    full_cmd = f"{command} && pwd"
    
    cmd = ["docker", "exec", container_name, "bash", "-c", full_cmd]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return f"[ERROR] 執行失敗: {result.stderr.strip()}"
    except Exception as e:
        return f"[ERROR] 異常: {str(e)}"

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: docker_run.py <container_name> <command>")
        sys.exit(1)
        
    target = sys.argv[1]
    command = " ".join(sys.argv[2:])
    print(run_in_container(target, command))