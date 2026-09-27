import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import (docker_exec, pass_or_empty, parse_timeout, current_target_container,
                            list_container_names, with_target_marker, NO_TARGET_HINT)

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 570   # 必須低於 Agent_Runner.TOOL_EXEC_TIMEOUT (600)，否則會先被 harness 殺掉

USAGE = (
    "用法: scripts/docker_runcmd_cmd.py [--timeout <秒數>] [container_name] <command>\n"
    f"  --timeout 預設 {DEFAULT_TIMEOUT_SECONDS} 秒，上限 {MAX_TIMEOUT_SECONDS} 秒；長時間建置（如 colcon build）請明確加大。\n"
    "  container_name 可省略＝目前的目標容器（docker_open 選定、或最近一次成功操作的容器）；command 請用引號包住。"
)


def parse_args(argv, container_names=None):
    """回傳 (timeout, container, command, error_message)。
    container_names 供測試注入現有容器名稱清單；None 時需要判斷才向 docker 查。"""
    args = list(argv)
    timeout = DEFAULT_TIMEOUT_SECONDS
    if args and args[0] == "--timeout":
        if len(args) < 2:
            return None, None, None, f"[ERROR] --timeout 後面需要秒數。\n{USAGE}"
        timeout, err = parse_timeout(args[1], DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, name="--timeout")
        if err:
            return None, None, None, f"{err}\n{USAGE}"
        args = args[2:]
    if not args:
        return None, None, None, f"[ERROR] 參數不足，需要要執行的指令（容器名稱可省略＝目標容器）。\n{USAGE}"
    target = current_target_container()
    if len(args) == 1:
        # 只有一個參數：省略容器名稱、整串是指令 → 用目標容器；沒有目標容器就無法判斷
        if not target:
            return None, None, None, f"[ERROR] 只收到一個參數 {args[0]!r}，但目前沒有目標容器：{NO_TARGET_HINT}\n{USAGE}"
        return timeout, target, args[0], None
    container, command = args[0], " ".join(args[1:])
    if target and container != target:
        # 已有目標容器時，第一個參數若不是任何現有容器的完整名稱（模型忘記加引號把指令拆開、或省略了名稱），
        # 整串都當指令、容器用目標容器；docker exec 只認完整名稱，所以只做完全比對，不猜部分名稱
        names = container_names if container_names is not None else list_container_names()[0]
        if names is not None and container not in names:
            container, command = target, " ".join(args)
    return timeout, container, command, None


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
    body = pass_or_empty(out, "指令沒有任何標準輸出")
    if not body.startswith("[PASS]"):
        body = f"[PASS] 容器 '{target}' 內 `{command}` 執行完成（末行為執行後的容器內路徑）:\n{body}"
    return with_target_marker(body, target)


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
