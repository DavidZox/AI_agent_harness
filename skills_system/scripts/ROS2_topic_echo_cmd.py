import sys
import subprocess

def run_topic_echo(container, topic):
    # 使用 --once 僅讀取一筆資料，若要持續監聽請移除 --once
    cmd = ["docker", "exec", container, "bash", "-ic", f"ros2 topic echo {topic} --once"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python3 scripts/ROS2_topic_echo_cmd.py <container_name> <topic_name>")
        sys.exit(1)
    print(run_topic_echo(sys.argv[1], sys.argv[2]))