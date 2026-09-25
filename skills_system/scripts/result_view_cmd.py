"""result_view：讀某個工具結果存檔的一段原文（第 N 行起 M 行），配合 result_grep 的行號看命中處的整段內容。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _results_common import clip, describe, read_header, read_lines, resolve_result

DEFAULT_LINES, MAX_LINES = 80, 300
MAX_OUTPUT_CHARS = 12000
USAGE = (f"用法: scripts/result_view_cmd.py <編號|latest> [--from 行號] [--lines 行數]\n"
         f"  --from 預設從原文第一行；--lines 預設 {DEFAULT_LINES}（上限 {MAX_LINES}）。行號與 result_grep 顯示的 L 行號一致。")


def parse_args(argv):
    args = list(argv)
    ref, start, count = None, None, DEFAULT_LINES
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--from", "--lines"):
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                return None, f"[ERROR] {a} 後面需要數字。\n{USAGE}"
            if a == "--from":
                start = max(1, int(args[i + 1]))
            else:
                count = max(1, min(MAX_LINES, int(args[i + 1])))
            i += 2
            continue
        if a.startswith("-") and len(a) > 1:
            return None, f"[ERROR] 不認識的選項 {a!r}。\n{USAGE}"
        if ref is not None:
            return None, f"[ERROR] 只接受一個編號，收到 {ref!r} 與 {a!r}。\n{USAGE}"
        ref = a
        i += 1
    if ref is None:
        return None, f"[ERROR] 需要結果編號（或 latest）。\n{USAGE}"
    return {"ref": ref, "start": start, "count": count}, None


def main(argv):
    opts, err = parse_args(argv)
    if err:
        return err
    path, err = resolve_result(opts["ref"])
    if err:
        return err
    meta, body_start = read_header(path)
    lines = read_lines(path)
    total = len(lines)
    start = opts["start"] or body_start
    if start > total:
        return f"[ERROR] 起始行 {start} 超過檔案總行數 {total}（原文從第 {body_start} 行開始）。"
    end = min(total, start + opts["count"] - 1)
    head = (f"[PASS] {describe(meta, os.path.basename(path))}｜{meta.get('command') or ''}｜原文共 {total - body_start + 1} 行"
            f"（存檔 {total} 行，原文從第 {body_start} 行起）；顯示第 {start}～{end} 行"
            + (f"，之後還有 {total - end} 行（--from {end + 1} 繼續）" if end < total else "，已到檔尾") + "：")
    out = [head]
    if meta.get("task"):
        out.append(f"當時的任務：{meta['task']}")
    out += [f"L{j}: {clip(lines[j - 1])}" for j in range(start, end + 1)]
    text = "\n".join(out)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n（輸出達上限 {MAX_OUTPUT_CHARS} 字，減少 --lines）"
    return text


if __name__ == "__main__":
    try:
        print(main(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] result_view 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
