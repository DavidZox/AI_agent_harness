import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import ros2_exec, pass_or_empty, parse_timeout

# `ros2 topic echo --once` 在沒有 publisher 時會永遠等待，逾時是必要的
DEFAULT_TIMEOUT_SECONDS = 15
MAX_TIMEOUT_SECONDS = 570   # 低於 Agent_Runner.TOOL_EXEC_TIMEOUT (600)
USAGE = (
    "用法: scripts/ROS2_topic_echo_cmd.py <container_name> <topic_name> [timeout_seconds]\n"
    f"  timeout_seconds 預設 {DEFAULT_TIMEOUT_SECONDS} 秒（低頻 topic 請加大），上限 {MAX_TIMEOUT_SECONDS} 秒。"
)


def run_topic_echo(container, topic, timeout=DEFAULT_TIMEOUT_SECONDS):
    """讀取一筆訊息即結束（--once）。"""
    container = (container or "").strip()
    topic = (topic or "").strip()
    if not container or not topic:
        return f"[ERROR] 參數不足，需要容器名稱與 topic 名稱。\n{USAGE}"

    ok, out, err = ros2_exec(
        container, f"ros2 topic echo {topic} --once", timeout,
        timeout_hint=(
            f"在 {timeout} 秒內沒有收到 '{topic}' 的任何訊息：可能目前沒有 publisher 在發布、"
            f"topic 名稱錯誤（請用 ROS2_topic_list 確認）、或 QoS 不相容；低頻 topic 可加大第三個參數的秒數。"
        ),
    )
    if not ok:
        return err
    return pass_or_empty(out, f"'{topic}' 回傳了空訊息")


if __name__ == "__main__":
    try:
        if len(sys.argv) < 3:
            print(f"[ERROR] 參數不足。\n{USAGE}")
            sys.exit(0)
        timeout, err = parse_timeout(
            sys.argv[3] if len(sys.argv) > 3 else None,
            DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, name="timeout_seconds",
        )
        if err:
            print(f"{err}\n{USAGE}")
            sys.exit(0)
        print(run_topic_echo(sys.argv[1], sys.argv[2], timeout))
    except Exception as e:
        print(f"[ERROR] ROS2_topic_echo 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
