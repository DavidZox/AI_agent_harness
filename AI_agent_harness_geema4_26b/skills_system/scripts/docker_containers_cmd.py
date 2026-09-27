import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import run_docker

TIMEOUT_SECONDS = 15   # docker ps 通常一秒內完成；超過代表 daemon 無回應
USAGE = (
    "用法: scripts/docker_containers_cmd.py [--running] [keyword]\n"
    "  --running：只列運行中的容器（預設含已停止的）。\n"
    "  keyword：以子字串過濾容器名稱（不分大小寫），只能給一個。"
)
FORMAT = "{{.Names}}\t{{.Status}}\t{{.Image}}\t{{.ID}}"
HEADERS = ("NAME", "STATUS", "IMAGE", "CONTAINER ID")


def parse_args(argv):
    """回傳 (running_only, keyword, error_message)。"""
    running_only = False
    keyword = ""
    for arg in argv:
        if arg == "--running":
            running_only = True
        elif arg.startswith("-"):
            return None, None, f"[ERROR] 不支援的選項: {arg}\n{USAGE}"
        elif keyword:
            return None, None, f"[ERROR] 關鍵字只能有一個，收到: {keyword!r} 與 {arg!r}\n{USAGE}"
        else:
            keyword = arg
    return running_only, keyword, None


def format_table(rows, headers):
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def line(cols):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)).rstrip()

    return "\n".join([line(headers)] + [line(r) for r in rows])


def execute(running_only=False, keyword=""):
    cmd = ["docker", "ps", "--format", FORMAT]
    if not running_only:
        cmd.insert(2, "-a")

    ok, out, err = run_docker(cmd, TIMEOUT_SECONDS, what="查詢容器清單 (docker ps)")
    if not ok:
        return err

    rows = []
    for ln in out.splitlines():
        if not ln.strip():
            continue
        cols = ln.split("\t")
        cols += [""] * (len(HEADERS) - len(cols))
        rows.append(tuple(cols[:len(HEADERS)]))

    if keyword:
        rows = [r for r in rows if keyword.lower() in r[0].lower()]

    scope = "運行中的容器" if running_only else "容器（含已停止）"
    filt = f"，名稱含 '{keyword}'" if keyword else ""
    if not rows:
        return f"[PASS] 目前沒有任何{scope}{filt}。"

    running = sum(1 for r in rows if r[1].startswith("Up"))
    table = format_table(rows, HEADERS)
    return f"[PASS] {scope}{filt}：共 {len(rows)} 個，運行中 {running} 個\n{table}"


if __name__ == "__main__":
    try:
        running_only, keyword, err = parse_args(sys.argv[1:])
        if err:
            print(err)
            sys.exit(0)
        print(execute(running_only, keyword))
    except Exception as e:
        print(f"[ERROR] docker_containers 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
