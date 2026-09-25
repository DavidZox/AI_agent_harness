"""result_grep：在工具結果存檔裡搜關鍵字（多關鍵字 a|b|c、正則、不分大小寫），回每個命中行的前後幾行；標頭給每個檔的命中數。

用途：主對話只拿到摘要，使用者追問「那 motor_rear_left 呢」「第 17 則長什麼樣」時，不重跑工具（觀察型工具一次要等幾十秒），
直接在存檔裡找。輸出超過工具回傳門檻時 harness 會交給獨立 session 依使用者的問題擷取，控制在門檻內就直接進主對話。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _results_common import (clip, compile_pattern, current_session, describe, list_result_files, read_header, read_lines,
                             resolve_result, results_dir, search_lines)

DEFAULT_CONTEXT, MAX_CONTEXT = 3, 10
DEFAULT_MAX_HITS, MAX_MAX_HITS = 40, 200
ALL_FILES_LIMIT = 20          # all：只搜目前 session 最近 20 個存檔
MAX_OUTPUT_CHARS = 12000
BLOCK_RE = re.compile(r"^--- #\d+")   # ROS2_topic_echo 存檔裡每則訊息的分隔行
USAGE = (f"用法: scripts/result_grep_cmd.py <編號|latest|all> <關鍵字> [-C 前後行數] [--max 命中上限] [--block]\n"
         f"  關鍵字：可用 | 分隔同義詞（error|fail|timeout）、可含正則、不分大小寫；含空白請用引號。\n"
         f"  -C 預設 {DEFAULT_CONTEXT}（上限 {MAX_CONTEXT}）；--max 預設 {DEFAULT_MAX_HITS}（上限 {MAX_MAX_HITS}）；"
         f"--block 命中時回整則訊息（以 --- #序號 分隔的存檔，例如 ROS2_topic_echo）而不是前後幾行。\n"
         f"  all＝目前 session 最近 {ALL_FILES_LIMIT} 個存檔（新→舊）。")


def parse_args(argv):
    args = list(argv)
    ctx, max_hits, block = DEFAULT_CONTEXT, DEFAULT_MAX_HITS, False
    pos = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "--context"):
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                return None, f"[ERROR] {a} 後面需要行數。\n{USAGE}"
            ctx = max(0, min(MAX_CONTEXT, int(args[i + 1])))
            i += 2
            continue
        if a == "--max":
            if i + 1 >= len(args) or not args[i + 1].isdigit():
                return None, f"[ERROR] --max 後面需要數字。\n{USAGE}"
            max_hits = max(1, min(MAX_MAX_HITS, int(args[i + 1])))
            i += 2
            continue
        if a == "--block":
            block = True
            i += 1
            continue
        if a.startswith("-") and len(a) > 1 and not pos:
            return None, f"[ERROR] 不認識的選項 {a!r}。\n{USAGE}"
        pos.append(a)
        i += 1
    if len(pos) < 2:
        return None, f"[ERROR] 需要 <編號|latest|all> 與 <關鍵字> 兩個參數。\n{USAGE}"
    if len(pos) > 2:
        return None, f"[ERROR] 關鍵字含空白請用引號包住（收到多個參數：{pos[1:]}）。\n{USAGE}"
    return {"ref": pos[0], "pattern": pos[1], "ctx": ctx, "max_hits": max_hits, "block": block}, None


def target_files(ref):
    """要搜哪些檔：all → 目前 session 最近 N 個（新→舊）；否則單一檔。回傳 (paths, error)。"""
    if ref.lower() == "all":
        d = results_dir()
        files = list_result_files(d)
        sess = current_session()
        mine = [f for f in files if not sess or f[0] == sess] or files
        return [os.path.join(d, f[3]) for f in mine[-ALL_FILES_LIMIT:][::-1]], None
    path, err = resolve_result(ref)
    return ([path] if path else []), err


def block_bounds(lines, i, body_start):
    """--block：找包住第 i 行（0-based）的 --- #k 區塊；沒有分隔行就回 None。"""
    start = None
    for j in range(i, body_start - 2, -1):
        if BLOCK_RE.match(lines[j]):
            start = j
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(i + 1, len(lines)):
        if BLOCK_RE.match(lines[j]):
            end = j
            break
    return start, end


def render_file(path, regex, ctx, budget_hits, block):
    """回傳 (lines_out, n_hits, timed_out, shown)。行號是存檔裡的實際行號（含檔頭），result_view --from 可直接用。"""
    meta, body_start = read_header(path)
    lines = read_lines(path)
    body_idx = list(range(body_start - 1, len(lines)))
    hits, timed_out = search_lines([lines[i] for i in body_idx], regex)
    hits = [body_start - 1 + h for h in hits]
    fn = os.path.basename(path)
    if not hits:
        return [f"{describe(meta, fn)}：0 行命中（原文共 {len(lines) - body_start + 1} 行）"], 0, timed_out, 0
    out = [f"{describe(meta, fn)}：命中 {len(hits)} 行（原文共 {len(lines) - body_start + 1} 行）"
           + (f"，列出前 {min(len(hits), budget_hits)} 個" if len(hits) > budget_hits else "")]
    shown = 0
    last_end = -1
    for h in hits[:budget_hits]:
        if block:
            b = block_bounds(lines, h, body_start)
            s, e = b if b else (max(body_start - 1, h - ctx), min(len(lines), h + ctx + 1))
        else:
            s, e = max(body_start - 1, h - ctx), min(len(lines), h + ctx + 1)
        if s <= last_end:          # 與前一段重疊：接著印，不重複
            s = last_end + 1
        if s < e and last_end >= 0 and s > last_end + 1:
            out.append("--")
        for j in range(s, e):
            out.append(f"{'>' if j == h else ' '} L{j + 1}: {clip(lines[j])}")
        last_end = max(last_end, e - 1)
        shown += 1
    return out, len(hits), timed_out, shown


def main(argv):
    opts, err = parse_args(argv)
    if err:
        return err
    regex, err = compile_pattern(opts["pattern"])
    if err:
        return err
    paths, err = target_files(opts["ref"])
    if err:
        return err
    total_hits, budget = 0, opts["max_hits"]
    sections, timed = [], False
    for p in paths:
        block, n, t, shown = render_file(p, regex, opts["ctx"], budget, opts["block"])
        timed = timed or t
        total_hits += n
        budget -= shown
        sections.append("\n".join(block))
        if budget <= 0:
            sections.append(f"（命中數達上限 {opts['max_hits']}，縮小關鍵字或加 --max）")
            break
    head = (f"[PASS] 在 {len(paths)} 個結果檔中搜「{opts['pattern']}」：共 {total_hits} 行命中"
            f"（每個命中附前後 {opts['ctx']} 行{'／整則訊息' if opts['block'] else ''}；行號可用 result_view <編號> --from 行號 看更多）")
    if total_hits == 0:
        head += "；可換同義詞（error|fail|warn）、用 result_view 看原文的欄位名稱，或確認編號是否正確"
    if timed:
        head += f"；⚠️ 比對超過時間預算，部分內容未搜完，請簡化關鍵字（避免複雜的正則）"
    text = "\n".join([head] + sections)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n（輸出達上限 {MAX_OUTPUT_CHARS} 字，縮小關鍵字、減少 -C 或 --max）"
    return text


if __name__ == "__main__":
    try:
        print(main(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] result_grep 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
