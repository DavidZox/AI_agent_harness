import sys
import subprocess

def run_node_list(container):
    """
    在指定的 Docker 容器內執行 `ros2 node list`，列出該容器裡目前
    所有正在執行的 ROS2 節點名稱，供後續（例如 ROS2_node_info_cmd）
    進一步查詢個別節點使用。

    用 `docker exec <container> bash -ic "ros2 node list"` 執行，
    選用 `-ic` 是為了讓容器內的 shell 先跑過 .bashrc / ROS2 的
    setup.bash，否則找不到 `ros2` 指令。

    參數：
        container：目標 Docker 容器名稱。

    回傳：
        指令成功（returncode == 0）時回傳去除頭尾空白的 stdout
        （每行一個節點名稱）；失敗則回傳 stderr。本函式與呼叫端
        的 __main__ 都沒有包 try/except，若 docker 指令不存在等
        例外會直接以未捕捉例外往外拋，不會被轉成 [ERROR] 訊息。
    """
    cmd = ["docker", "exec", container, "bash", "-ic", "ros2 node list"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 scripts/ROS2_node_list_cmd.py <container_name>")
        sys.exit(1)
    print(run_node_list(sys.argv[1]))