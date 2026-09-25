import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import ros2_exec, pass_or_empty, resolve_container, with_target_marker

TIMEOUT_SECONDS = 30
USAGE = ("用法: scripts/ROS2_node_info_cmd.py [container_name] <node_name>"
         "（container_name 省略＝目前的目標容器；node 名稱需含命名空間，如 /nav_node）")


def parse_cli(argv):
    """回傳 (container, node, error)。只有一個參數、或第一個參數像 node 名稱（以 / 開頭）時，容器用目標容器。"""
    args = [a for a in argv if a.strip()]
    if not args:
        return None, None, f"[ERROR] 參數不足，需要節點名稱。\n{USAGE}"
    if len(args) == 1 or args[0].startswith("/"):
        container, node = "", args[0]
    else:
        container, node = args[0], args[1]
    container, err = resolve_container(container, USAGE)
    return container, node, err


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
    body = pass_or_empty(out, f"節點 '{node}' 沒有回傳任何資訊")
    if not body.startswith("[PASS]"):
        body = f"[PASS] 節點 {node} 的資訊（ros2 node info）:\n{body}"
    return with_target_marker(body, container)


if __name__ == "__main__":
    try:
        container, node, err = parse_cli(sys.argv[1:])
        if err:
            print(err)
            sys.exit(0)
        print(run_node_info(container, node))
    except Exception as e:
        print(f"[ERROR] ROS2_node_info 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
