import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import docker_exec, pass_or_empty, parse_timeout

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 570   # 必須低於 Agent_Runner.TOOL_EXEC_TIMEOUT (600)，否則會先被 harness 殺掉

USAGE = (
    "用法: scripts/docker_runcmd_cmd.py [--timeout <秒數>] <container_name> <command>\n"
    f"  --timeout 預設 {DEFAULT_TIMEOUT_SECONDS} 秒，上限 {MAX_TIMEOUT_SECONDS} 秒；長時間建置（如 colcon build）請明確加大。"
)


def parse_args(argv):
    """回傳 (timeout, container, command, error_message)。"""
    args = list(argv)
    timeout = DEFAULT_TIMEOUT_SECONDS
    if args and args[0] == "--timeout":
        if len(args) < 2:
            return None, None, None, f"[ERROR] --timeout 後面需要秒數。\n{USAGE}"
        timeout, err = parse_timeout(args[1], DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, name="--timeout")
        if err:
            return None, None, None, f"{err}\n{USAGE}"
        args = args[2:]
    if len(args) < 2:
        return None, None, None, f"[ERROR] 參數不足，需要容器名稱與指令。\n{USAGE}"
    return timeout, args[0], " ".join(args[1:]), None


def run_in_container(target, command, timeout=DEFAULT_TIMEOUT_SECONDS):
    # 以 && 串接 pwd：指令失敗或被逾時終止時不會印出誤導的路徑
    full_cmd = f"{command} && pwd"
    ok, out, err = docker_exec(
        target,
        full_cmd,
        timeout,
        what=f"容器 '{target}' 內的 `{command}`",
        timeout_hint=f"若是長時間的建置或安裝，請改用 --timeout 指定更長的秒數（上限 {MAX_TIMEOUT_SECONDS}）。",
    )
    if not ok:
        return err
    return pass_or_empty(out, "指令沒有任何標準輸出")


if __name__ == "__main__":
    try:
        timeout, target, command, err = parse_args(sys.argv[1:])
        if err:
            print(err)
            sys.exit(0)
        print(run_in_container(target, command, timeout))
    except Exception as e:
        print(f"[ERROR] docker_runcmd 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
