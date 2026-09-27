import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import ros2_exec, count_and_filter, parse_filter_args, resolve_container, with_target_marker

TIMEOUT_SECONDS = 30
USAGE = ("用法: scripts/ROS2_node_list_cmd.py [container_name] [--filter 關鍵字]...\n"
         "  container_name 省略＝目前的目標容器；--filter 只列名稱含關鍵字的 node 並在標頭計數（可重複）。")


def run_node_list(argv):
    container, keywords, err = parse_filter_args(argv, USAGE)
    if err:
        return err
    container, err = resolve_container(container, USAGE)   # 省略＝目標容器
    if err:
        return err

    ok, out, err = ros2_exec(
        container, "ros2 node list", TIMEOUT_SECONDS,
        timeout_hint="ros2 discovery 無回應，請確認容器內 ROS2 daemon 與網路（DDS）設定是否正常。",
    )
    if not ok:
        return err
    return with_target_marker(count_and_filter(out, keywords, "node", container), container)


if __name__ == "__main__":
    try:
        print(run_node_list(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] ROS2_node_list 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
