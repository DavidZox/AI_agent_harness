import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import ros2_exec, pass_or_empty, resolve_container, with_target_marker

# ROS2 探索（discovery）通常幾秒內完成；daemon 冷啟動時會慢一點
TIMEOUT_SECONDS = 30
USAGE = "用法: scripts/ROS2_topic_list_cmd.py [container_name]（省略＝目前的目標容器）"


def run_ros2_topic_list(container_name):
    """在指定的 Docker 容器內執行 `ros2 topic list`（bash -ic + ROS2 環境 fallback）。"""
    container_name, err = resolve_container(container_name, USAGE)   # 省略＝目標容器
    if err:
        return err

    ok, out, err = ros2_exec(
        container_name, "ros2 topic list", TIMEOUT_SECONDS,
        timeout_hint="ros2 discovery 無回應，請確認容器內 ROS2 daemon 與網路（DDS）設定是否正常。",
    )
    if not ok:
        return err
    return with_target_marker(pass_or_empty(out, "目前沒有任何 topic 被發布"), container_name)


if __name__ == "__main__":
    try:
        print(run_ros2_topic_list(sys.argv[1] if len(sys.argv) > 1 else ""))
    except Exception as e:
        print(f"[ERROR] ROS2_topic_list 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
