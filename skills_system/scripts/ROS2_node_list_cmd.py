import sys
import subprocess

def run_node_list(container):
    cmd = ["docker", "exec", container, "bash", "-ic", "ros2 node list"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python3 scripts/ROS2_node_list_cmd.py <container_name>")
        sys.exit(1)
    print(run_node_list(sys.argv[1]))