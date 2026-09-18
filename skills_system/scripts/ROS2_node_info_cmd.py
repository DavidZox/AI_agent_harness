import sys
import subprocess

def run_node_info(container, node):
    cmd = ["docker", "exec", container, "bash", "-ic", f"ros2 node info {node}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else result.stderr.strip()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python3 scripts/ROS2_node_info_cmd.py <container_name> <node_name>")
        sys.exit(1)
    print(run_node_info(sys.argv[1], sys.argv[2]))