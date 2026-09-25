"""list_dir：列出目錄清單（純 Python，不再包 ls）。

數值交給腳本算、不交給模型數：標頭給「共 N 項（檔案／目錄各幾個）」，永遠附一行「最新修改」；--filter 只列名稱含關鍵字的
項目並在標頭給數量；--newest N 依修改時間新→舊只列前 N 項。舊版的 ls 旗標（-la、-h）照樣接受但忽略。
每行：權限、大小（bytes）、修改時間、名稱（目錄加 /、連結加 @）。逾時 5 秒（掛載裝置無回應時）。
"""
import os
import shlex
import signal
import stat
import sys
import time

TIMEOUT_SECONDS = 5
DEFAULT_NEWEST = 5
NEWEST_MENTION = 3       # 「最新修改」行最多提到幾個
USAGE = ("用法: scripts/ls_cmd.py [path] [--filter 關鍵字] [--newest [N]]\n"
         "  path 預設目前工作目錄；--filter 只列名稱含關鍵字的項目（不分大小寫）並在標頭計數；"
         f"--newest 依修改時間新→舊只列前 N 項（預設 {DEFAULT_NEWEST}）。")


def parse_args(argv):
    """回傳 (path, keyword, newest, error)。argv 可以是 list 或整串字串。"""
    if isinstance(argv, str):
        argv = shlex.split(argv)
    path, keyword, newest = ".", None, None
    args = [a for a in argv if a != ""]   # 空字串參數（模型常寫 `. ""`）直接忽略
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--filter":
            if i + 1 >= len(args):
                return None, None, None, f"[ERROR] --filter 後面需要關鍵字。\n{USAGE}"
            keyword = args[i + 1].strip("*")   # 模型常寫成 glob（*.md）：--filter 是子字串比對，去掉星號即可
            i += 2
            continue
        if a == "--newest":
            newest = DEFAULT_NEWEST
            if i + 1 < len(args) and args[i + 1].isdigit():
                newest = max(1, int(args[i + 1]))
                i += 1
            i += 1
            continue
        if a.startswith("--"):
            return None, None, None, f"[ERROR] 不認識的選項 {a}。\n{USAGE}"
        if a.startswith("-") and len(a) > 1:
            i += 1          # 舊版 ls 旗標（-la、-h 等）：忽略
            continue
        path = a
        i += 1
    return path, keyword, newest, None


def _fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def scan(path):
    entries = []
    with os.scandir(path) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                "name": e.name, "is_dir": e.is_dir(follow_symlinks=False), "is_link": e.is_symlink(),
                "size": st.st_size, "mtime": st.st_mtime, "mode": stat.filemode(st.st_mode),
            })
    return entries


def _display_name(e):
    return e["name"] + ("/" if e["is_dir"] else "@" if e["is_link"] else "")


def newest_line(entries):
    """「最新修改：A（時間）；其次：B（時間）、C（時間）」——以列出的項目為範圍，優先只算檔案，沒有檔案才算目錄。"""
    files = [e for e in entries if not e["is_dir"]] or list(entries)
    if not files:
        return None
    ranked = sorted(files, key=lambda e: e["mtime"], reverse=True)[:NEWEST_MENTION]
    line = f"最新修改：{_display_name(ranked[0])}（{_fmt_time(ranked[0]['mtime'])}）"
    if len(ranked) > 1:
        line += "；其次：" + "、".join(f"{_display_name(e)}（{_fmt_time(e['mtime'])}）" for e in ranked[1:])
    return line


def render(path, entries, keyword=None, newest=None):
    total = len(entries)
    n_dirs = sum(1 for e in entries if e["is_dir"])
    head = f"[PASS] 目錄列表 ({path})：共 {total} 項（{total - n_dirs} 個檔案、{n_dirs} 個目錄）"
    shown = entries
    if keyword:
        kw = keyword.lower()
        shown = [e for e in entries if kw in e["name"].lower()]
        head += f"；名稱含「{keyword}」的 {len(shown)} 項"
    if newest:
        shown = sorted(shown, key=lambda e: e["mtime"], reverse=True)[:newest]
        head += f"；依修改時間新→舊只列前 {len(shown)} 項"
    else:
        shown = sorted(shown, key=lambda e: e["name"].lower())
    lines = [head]
    if not shown:
        # 實測模型會用 list_dir --filter 在根目錄數「專案裡」的檔案，0 項就回報 0：明說這裡只看一層、指路 find_file
        lines.append(f"（{path} 這一層沒有名稱含「{keyword}」的項目。注意：list_dir 不看子目錄，如果使用者問的是整個專案或所有子目錄，"
                     f"這個 0 不能當答案，請改用 scripts/find_file_cmd.py {keyword} {path} 遞迴搜尋後再回答）"
                     if keyword else "（空目錄）")
        return "\n".join(lines)
    nl = newest_line(shown)
    if nl:
        lines.append(nl)
    lines += [f"{e['mode']} {e['size']:>10} {_fmt_time(e['mtime'])} {_display_name(e)}" for e in shown]
    return "\n".join(lines)


def _timeout_handler(signum, frame):
    raise TimeoutError


def execute(argv):
    path, keyword, newest, err = parse_args(argv)
    if err:
        return err
    if not os.path.exists(path):
        return f"[ERROR] 無法讀取目錄: {path} 不存在（目前工作目錄 {os.getcwd()}）"
    if not os.path.isdir(path):
        return f"[ERROR] 無法讀取目錄: {path} 不是目錄（看檔案內容請用 view_file）"
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(TIMEOUT_SECONDS)
    try:
        entries = scan(path)
    except TimeoutError:
        return (f"[ERROR] 目錄列表逾時（超過 {TIMEOUT_SECONDS} 秒），目標目錄可能過大，"
                f"或位於無回應的網路／掛載裝置上，請改指定較小的子目錄。")
    except PermissionError:
        return f"[ERROR] 無法讀取目錄: 沒有權限讀取 {path}"
    except OSError as e:
        return f"[ERROR] 無法讀取目錄: {e}"
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
    return render(path, entries, keyword, newest)


if __name__ == "__main__":
    try:
        print(execute(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] list_dir 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
