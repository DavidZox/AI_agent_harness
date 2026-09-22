import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import ros2_exec, pass_or_empty

TIMEOUT_SECONDS = 30
USAGE = "用法: scripts/ROS2_node_info_cmd.py <container_name> <node_name>（node 名稱需含命名空間，如 /nav_node）"


def run_node_info(container, node):
    container = (container or "").strip()
    node = (node or "").strip()
    if not container or not node:
        return f"[ERROR] 參數不足，需要容器名稱與節點名稱。\n{USAGE}"

    ok, out, err = ros2_exec(
        container, f"ros2 node info {node}", TIMEOUT_SECONDS,
        timeout_hint="ros2 discovery 無回應，請確認容器內 ROS2 daemon 與網路（DDS）設定是否正常。",
    )
    if not ok:
        return err
    return pass_or_empty(out, f"節點 '{node}' 沒有回傳任何資訊")


if __name__ == "__main__":
    try:
        if len(sys.argv) < 3:
            print(f"[ERROR] 參數不足。\n{USAGE}")
            sys.exit(0)
        print(run_node_info(sys.argv[1], sys.argv[2]))
    except Exception as e:
        print(f"[ERROR] ROS2_node_info 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
