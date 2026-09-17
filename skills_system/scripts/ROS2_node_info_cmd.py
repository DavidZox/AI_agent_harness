import sys
import subprocess

def run_node_info(container, node):
    """
    在指定的 Docker 容器內對某個 ROS2 節點執行 `ros2 node info <node>`，
    查出該節點目前訂閱/發布哪些 topic、提供哪些 service 與 action。

    透過 `docker exec <container> bash -ic "ros2 node info <node>"` 執行，
    刻意用 `-ic`（interactive shell 並帶入指令字串）而不是 `-c`，
    是為了讓容器內的 bash 載入 .bashrc / ROS2 的 setup.bash，
    不然容器內會找不到 `ros2` 這個指令。

    參數：
        container：目標 Docker 容器名稱。
        node：要查詢的 ROS2 節點名稱，通常來自 `ros2 node list` 的輸出。

    回傳：
        指令成功（returncode == 0）時回傳去除頭尾空白的 stdout；
        失敗則回傳 stderr。

    修正紀錄：先前本函式沒有包 try/except，若 docker 指令不存在或
    subprocess 執行本身拋出例外，會以未處理例外的形式直接往外拋、印出
    原始 traceback。現在比照同資料夾 ROS2_topic_list_cmd.py 的作法，
    補上 try/except，例外時回傳 "[ERROR] 異常: ..." 字串。
    """
    cmd = ["docker", "exec", container, "bash", "-ic", f"ros2 node info {node}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()
    except Exception as e:
        return f"[ERROR] 異常: {str(e)}"

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python3 scripts/ROS2_node_info_cmd.py <container_name> <node_name>")
        sys.exit(1)
    print(run_node_info(sys.argv[1], sys.argv[2]))