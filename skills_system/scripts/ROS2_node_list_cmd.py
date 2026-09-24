import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import ros2_exec, pass_or_empty, resolve_container, with_target_marker

TIMEOUT_SECONDS = 30
USAGE = "用法: scripts/ROS2_node_list_cmd.py [container_name]（省略＝目前的目標容器）"


def run_node_list(container):
    container, err = resolve_container(container, USAGE)   # 省略＝目標容器
    if err:
        return err

    ok, out, err = ros2_exec(
        container, "ros2 node list", TIMEOUT_SECONDS,
        timeout_hint="ros2 discovery 無回應，請確認容器內 ROS2 daemon 與網路（DDS）設定是否正常。",
    )
    if not ok:
        return err
    return with_target_marker(pass_or_empty(out, "目前沒有任何 node 在運行"), container)


if __name__ == "__main__":
    try:
        print(run_node_list(sys.argv[1] if len(sys.argv) > 1 else ""))
    except Exception as e:
        print(f"[ERROR] ROS2_node_list 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
