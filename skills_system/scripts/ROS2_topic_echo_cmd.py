import sys
import subprocess

def run_topic_echo(container, topic):
    """
    在指定的 Docker 容器內對某個 topic 執行 `ros2 topic echo <topic> --once`，
    只抓一筆訊息就結束（而不是像互動模式那樣持續監聽），適合拿來快速確認
    某個 topic 目前有沒有資料、資料長什麼樣子。

    透過 `docker exec <container> bash -ic "..."` 執行，`-ic` 讓容器內的
    shell 讀取 .bashrc / ROS2 的 setup.bash，否則找不到 `ros2` 指令。
    若要改成持續監聽模式，需要拿掉指令字串中的 `--once`（見下方註解）。

    參數：
        container：目標 Docker 容器名稱。
        topic：要監聽的 ROS2 topic 名稱，通常來自 ROS2_topic_list_cmd
            的輸出。

    回傳：
        指令成功（returncode == 0）時回傳去除頭尾空白的 stdout
        （該筆訊息內容）；失敗則回傳 stderr。本函式與呼叫端的
        __main__ 都沒有包 try/except，例外會直接往外拋。
    """
    # 使用 --once 僅讀取一筆資料，若要持續監聽請移除 --once
    cmd = ["docker", "exec", container, "bash", "-ic", f"ros2 topic echo {topic} --once"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python3 scripts/ROS2_topic_echo_cmd.py <container_name> <topic_name>")
        sys.exit(1)
    print(run_topic_echo(sys.argv[1], sys.argv[2]))