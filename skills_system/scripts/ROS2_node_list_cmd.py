import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import ros2_exec, pass_or_empty

TIMEOUT_SECONDS = 30


def run_node_list(container):
    container = (container or "").strip()
    if not container:
        return "[ERROR] 請提供容器名稱。用法: scripts/ROS2_node_list_cmd.py <container_name>"

    ok, out, err = ros2_exec(
        container, "ros2 node list", TIMEOUT_SECONDS,
        timeout_hint="ros2 discovery 無回應，請確認容器內 ROS2 daemon 與網路（DDS）設定是否正常。",
    )
    if not ok:
        return err
    return pass_or_empty(out, "目前沒有任何 node 在運行")


if __name__ == "__main__":
    try:
        print(run_node_list(sys.argv[1] if len(sys.argv) > 1 else ""))
    except Exception as e:
        print(f"[ERROR] ROS2_node_list 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
