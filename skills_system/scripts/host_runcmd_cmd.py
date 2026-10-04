#!/usr/bin/env python3
"""本機通用 Shell 指令執行腳本 (host_runcmd_cmd.py)

允許 Agent 在本機環境下達自由 Shell 指令，自動維護工作目錄 (CWD) 狀態，
並提供逾時控制與防錯修復機制。
"""

import os
import subprocess
import sys

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 570  # 低於 Harness 的 600 秒總逾時

USAGE = (
    "用法: scripts/host_runcmd_cmd.py [--timeout <秒數>] <command>\n"
    f"  --timeout 預設 {DEFAULT_TIMEOUT_SECONDS} 秒，上限 {MAX_TIMEOUT_SECONDS} 秒；長時間建置請明確加大。\n"
    "  command 請用引號包住，多個指令請用 && 串接。"
)


def parse_timeout(val, default, max_val):
    """解析並驗證 timeout 參數"""
    try:
        t = int(val)
        if 1 <= t <= max_val:
            return t, None
        return default, f"[ERROR] timeout 必須介於 1~{max_val} 秒，收到 {val!r}"
    except (ValueError, TypeError):
        return default, f"[ERROR] timeout 格式不正確: {val!r}"


def get_current_cwd():
    """取得當前 Harness 維護的本機 CWD（可依你的 Harness 狀態檔調整）"""
    # 預設讀取環境變數或回歸目前進程路徑
    return os.environ.get("AGENT_HOST_CWD", os.getcwd())


def parse_args(argv):
    """解析命令列參數 (timeout, command, error_message)"""
    args = list(argv)
    timeout = DEFAULT_TIMEOUT_SECONDS

    if args and args[0] == "--timeout":
        if len(args) < 2:
            return None, None, f"[ERROR] --timeout 後面需要秒數。\n{USAGE}"
        timeout, err = parse_timeout(args[1], DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS)
        if err:
            return None, None, f"{err}\n{USAGE}"
        args = args[2:]

    if not args:
        return None, None, f"[ERROR] 參數不足，需要要執行的指令。\n{USAGE}"

    # 若模型忘記加引號把指令拆散，自動重組所有剩餘參數
    command = " ".join(args)
    return timeout, command, None


def run_host_cmd(command, timeout=DEFAULT_TIMEOUT_SECONDS):
    """在本機執行 shell 指令，末行附帶 pwd 取得最新路徑"""
    cwd = get_current_cwd()
    
    # 指令後串接 && pwd，確保成功時能拿回最後的工作目錄
    full_cmd = f"{command} && pwd"

    try:
        res = subprocess.run(
            ["/bin/bash", "-c", full_cmd],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=timeout
        )

        stdout = res.stdout.rstrip()
        stderr = res.stderr.rstrip()

        if res.returncode != 0:
            err_msg = stderr if stderr else stdout
            return f"[ERROR] 本機執行 `{command}` 失敗（exit code {res.returncode}）:\n{err_msg}"

        if not stdout:
            return f"[PASS] 本機內 `{command}` 執行完成（無輸出）"

        # 分割末行取得最新路徑
        lines = stdout.splitlines()
        new_cwd = lines[-1] if lines else cwd
        output_body = "\n".join(lines[:-1]) if len(lines) > 1 else ""

        result = f"[PASS] 本機內 `{command}` 執行完成（末行為執行後的工作目錄）:\n"
        if output_body:
            result += f"{output_body}\n"
        result += f"[HOST_CWD] {new_cwd}"

        return result

    except subprocess.TimeoutExpired:
        return (
            f"[ERROR] 本機執行 `{command}` 逾時（超過 {timeout} 秒）。\n"
            f"若是長時間建置或編譯，請改用 --timeout 指定更長的秒數（上限 {MAX_TIMEOUT_SECONDS}）。"
        )
    except Exception as e:
        return f"[ERROR] 本機執行未預期的例外: {e}"


if __name__ == "__main__":
    try:
        timeout, command, err = parse_args(sys.argv[1:])
        if err:
            print(err)
            sys.exit(0)
        
        print(run_host_cmd(command, timeout))
    except Exception as e:
        print(f"[ERROR] host_runcmd 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)