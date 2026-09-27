import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import run_docker

TIMEOUT_SECONDS = 15   # docker images 通常一秒內完成；超過代表 daemon 無回應
USAGE = (
    "用法: scripts/docker_images_cmd.py [keyword]\n"
    "  keyword：以子字串過濾 repository 名稱或 tag（不分大小寫），只能給一個。"
)
FORMAT = "{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}\t{{.CreatedSince}}"
HEADERS = ("REPOSITORY", "TAG", "IMAGE ID", "SIZE", "CREATED")


def parse_args(argv):
    """回傳 (keyword, error_message)。"""
    keyword = ""
    for arg in argv:
        if arg.startswith("-"):
            return None, f"[ERROR] 不支援的選項: {arg}\n{USAGE}"
        if keyword:
            return None, f"[ERROR] 關鍵字只能有一個，收到: {keyword!r} 與 {arg!r}\n{USAGE}"
        keyword = arg
    return keyword, None


def format_table(rows, headers):
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def line(cols):
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)).rstrip()

    return "\n".join([line(headers)] + [line(r) for r in rows])


def execute(keyword=""):
    ok, out, err = run_docker(
        ["docker", "images", "--format", FORMAT],
        TIMEOUT_SECONDS,
        what="查詢映像檔清單 (docker images)",
    )
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
        rows = [r for r in rows if keyword.lower() in f"{r[0]}:{r[1]}".lower()]

    filt = f"（名稱或 tag 含 '{keyword}'）" if keyword else ""
    if not rows:
        return f"[PASS] 本機目前沒有任何映像檔{filt}。"

    dangling = sum(1 for r in rows if r[0] == "<none>")
    summary = f"[PASS] 本機映像檔{filt}：共 {len(rows)} 個"
    if dangling:
        summary += f"，其中 {dangling} 個為 <none> 懸空映像（通常是重建後殘留的舊版本，無法用名稱啟動）"
    return f"{summary}\n{format_table(rows, HEADERS)}"


if __name__ == "__main__":
    try:
        keyword, err = parse_args(sys.argv[1:])
        if err:
            print(err)
            sys.exit(0)
        print(execute(keyword))
    except Exception as e:
        print(f"[ERROR] docker_images 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
