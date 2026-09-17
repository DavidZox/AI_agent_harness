import sys
import subprocess

def run_ros2_topic_list(container_name):
    """
    在指定的 Docker 容器內執行 `ros2 topic list`，列出該容器目前所有
    可見的 ROS2 topic 名稱，供 ROS2_topic_echo_cmd 等指令進一步查詢
    個別 topic 內容時使用。

    透過 `docker exec <container_name> bash -ic "ros2 topic list"` 執行，
    用 `-ic`（interactive）確保容器內的 shell 會先載入 .bashrc /
    ROS2 的 setup.bash（例如 source /opt/ros/<distro>/setup.bash），
    否則容器內會找不到 `ros2` 指令。

    參數：
        container_name：目標 Docker 容器名稱。

    回傳：
        指令成功（returncode == 0）時回傳去除頭尾空白的 stdout
        （每行一個 topic 名稱）；指令失敗則回傳
        "[ERROR] 執行失敗: <stderr 內容>"。
        與同資料夾的其他 ROS2_*_cmd 指令不同，這裡額外包了
        try/except，若 subprocess 執行過程本身丟出例外（例如
        找不到 docker 指令），會被攔截並回傳
        "[ERROR] 異常: <例外內容>"，而不會讓例外往外傳。
    """
    # 使用 bash -ic 可以確保執行時會載入容器內的 .bashrc 或環境設定
    # 這樣才能找到 ros2 指令
    command = "ros2 topic list"
    
    # 組合 docker exec 指令
    # -i: interactive, -t: tty (雖然這裡輸出是字串，但互動式環境對 ROS2 指令較友善)
    cmd = ["docker", "exec", container_name, "bash", "-ic", command]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return f"[ERROR] 執行失敗: {result.stderr.strip()}"
    except Exception as e:
        return f"[ERROR] 異常: {str(e)}"

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 scripts/ROS2_topic_list_cmd.py <container_name>")
        sys.exit(1)
        
    container_name = sys.argv[1]
    
    # 執行並輸出結果
    output = run_ros2_topic_list(container_name)
    print(output)