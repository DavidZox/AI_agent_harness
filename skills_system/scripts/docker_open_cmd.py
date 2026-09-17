import sys
import subprocess
import json

def get_real_container_name(display_name):
    """
    查詢本機所有 Docker 容器（含已停止的），找出名稱中「包含」
    display_name 這個子字串的真實容器名稱，讓使用者/Agent 不用記住
    或輸入完整精確的容器名稱，打個關鍵字片段就能找到對應容器。

    作法：執行 `docker ps -a --format "{{.Names}}"` 取得所有容器
    名稱（-a 表示連已停止的容器也列出來，所以找到的容器不一定
    正在執行中），逐一比對是否包含 display_name，回傳第一個符合的
    名稱。

    參數：
        display_name：使用者輸入的容器名稱片段（可以是完整名稱，
            也可以只是其中一段）。

    回傳：
        找到時回傳第一個符合的完整容器名稱（str）；找不到符合項目、
        或 docker 指令執行過程中出現任何例外，都回傳 None
        （例外會被吞掉，呼叫端只需要判斷回傳值是否為 None）。
    """
    # 執行 docker inspect，查詢所有容器的詳細資訊
    cmd = ["docker", "ps", "-a", "--format", "{{.Names}}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        names = result.stdout.strip().split('\n')
        
        # 尋找是否有名稱完全符合或包含 display_name
        for name in names:
            if display_name in name:
                return name
        return None
    except:
        return None

def enter_container(target_name):
    """
    以「顯示名稱片段」連線進入 Docker 容器，做一次連線健檢：
    確認容器存在、可以 exec 進去，並回報進去之後的 pwd 與 whoami，
    但不會保留一個持續開著的互動式 session（每次呼叫都是各自獨立
    的一次性 docker exec）。

    流程：
        1. 呼叫 get_real_container_name 把 target_name 這個片段
           解析成真實容器名稱；找不到就直接回傳 [ERROR]，提示
           確認容器是否已啟動。
        2. 對解析出的真實容器名稱執行
           `docker exec <real_name> bash -c "echo '連線成功'; pwd; whoami"`，
           用這三個無副作用的指令當作連線是否成功的驗證。

    參數：
        target_name：容器名稱或其中一段子字串。

    回傳：
        連線成功：回傳 [PASS] 訊息，附上 echo/pwd/whoami 的輸出
        （容器內目前路徑與使用者身分）。
        連線失敗（docker exec 非 0 結束碼，例如容器其實沒在跑）：
        回傳 [ERROR] 並附上 stderr。
        執行過程中的例外：回傳通用 [ERROR] 訊息，不會往外拋出。
    """
    # 1. 偵測真實名稱
    real_name = get_real_container_name(target_name)
    
    if not real_name:
        return f"[ERROR] 在 Docker 中找不到名稱包含 '{target_name}' 的容器。請確認容器是否已啟動。"
    
    # 2. 使用找到的真實名稱執行
    cmd = ["docker", "exec", real_name, "bash", "-c", "echo '連線成功'; pwd; whoami"]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return f"[PASS] 已成功連線至 '{real_name}'！\n{result.stdout.strip()}"
        else:
            return f"[ERROR] 連線失敗: {result.stderr.strip()}"
    except Exception as e:
        return f"[ERROR] 執行異常: {str(e)}"

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    print(enter_container(target))