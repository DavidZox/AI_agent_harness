import sys
import subprocess

def run_ros2_topic_list(container_name):
    """
    在指定的 Docker 容器內執行 'ros2 topic list'。
    透過 bash -ic 確保載入 ROS2 環境變數 (source /opt/ros/<distro>/setup.bash)。
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