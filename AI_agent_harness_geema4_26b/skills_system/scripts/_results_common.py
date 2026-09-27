"""工具結果存檔（logs/tool_results/）的共用層，給 result_list／result_grep／result_view 三個技能用。

harness（Agent_Runner._archive_tool_result）每次腳本執行都把完整原始輸出存成 <session>_<id>_<腳本>.md（key: value 檔頭 + 原文），
並在 index.md 記一行。這裡負責：找存檔目錄（環境變數 TOOL_RESULTS_DIR，預設 <harness>/logs/tool_results）、目前 session
（HARNESS_SESSION）、把「16／#16／latest／檔名」解析成檔案、讀檔頭（不用 PyYAML）、安全的逐行比對（長度上限＋時間預算，
防災難性回溯；路徑一律鎖在存檔目錄內）。
"""
import os
import re
import time

INDEX_NAME = "index.md"
FILE_RE = re.compile(r"^(\d{8}_\d{6})_(\d{3,})_(.+)\.md$")
MAX_PATTERN_LEN = 200
TIME_BUDGET_SECONDS = 3.0
MAX_LINE_CHARS = 300          # 單行超過就截斷（高頻 topic 的 JSON 一行可能上千字）


def results_dir():
    d = (os.environ.get("TOOL_RESULTS_DIR") or "").strip()
    if not d:
        harness = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        d = os.path.join(harness, "logs", "tool_results")
    return d


def current_session():
    return (os.environ.get("HARNESS_SESSION") or "").strip()


def list_result_files(d=None):
    """[(session, id, stem, filename)]，依 session、id 排序（舊→新）。"""
    d = d or results_dir()
    if not os.path.isdir(d):
        return []
    out = []
    for fn in os.listdir(d):
        m = FILE_RE.match(fn)
        if m:
            out.append((m.group(1), int(m.group(2)), m.group(3), fn))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def resolve_result(ref, d=None):
    """'16'／'#16'（優先目前 session 的 #16，沒有就取最新一個有 #16 的 session）、'latest'、或 result_list 列出的檔名。
    回傳 (path, error)。路徑一定在存檔目錄內（只取 basename）。"""
    d = d or results_dir()
    files = list_result_files(d)
    if not files:
        return None, f"[ERROR] 目前沒有任何工具結果存檔（{d}）。要先執行過技能才有存檔。"
    ref = (ref or "").strip()
    if ref.lower() in ("", "latest", "last", "最新"):
        return os.path.join(d, files[-1][3]), None
    if ref.lstrip("#").isdigit():
        rid = int(ref.lstrip("#"))
        cands = [f for f in files if f[1] == rid]
        if not cands:
            return None, f"[ERROR] 找不到結果 #{rid}（用 result_list 看現有的編號）。"
        mine = [f for f in cands if f[0] == current_session()]
        return os.path.join(d, (mine or cands)[-1][3]), None
    name = os.path.basename(ref)
    if name in {f[3] for f in files}:
        return os.path.join(d, name), None
    return None, f"[ERROR] 找不到結果 {ref!r}：請給編號（如 16）、latest，或 result_list 列出的檔名。"


def read_header(path, max_lines=40):
    """key: value 檔頭 → dict；回傳 (meta, body_start)：body_start 是原文第一行的 1-based 行號。"""
    meta, body_start = {}, 1
    with open(path, encoding="utf-8", errors="replace") as f:
        first = f.readline()
        if first.strip() != "---":
            return meta, 1
        for i, line in enumerate(f, 2):
            if line.strip() == "---":
                body_start = i + 1
                break
            if i > max_lines:
                break
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip()] = v.strip()
    return meta, body_start


def read_lines(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def describe(meta, fn):
    """'#16 semantic_map_cmd.py（2026-09-25 10:34，PASS）'"""
    m = FILE_RE.match(fn)
    rid = meta.get("id") or (m.group(2).lstrip("0") if m else "?")
    return f"#{rid} {meta.get('script') or fn}（{meta.get('ts') or '?'}，{meta.get('status') or '?'}）"


def compile_pattern(pattern):
    """安全的 regex：長度上限、不分大小寫；不是合法 regex 就當純文字比對。回傳 (regex, error)。"""
    pattern = (pattern or "").strip()
    if not pattern:
        return None, "[ERROR] 需要搜尋關鍵字（可用 | 分隔同義詞，例如 error|fail|timeout）。"
    if len(pattern) > MAX_PATTERN_LEN:
        return None, f"[ERROR] 關鍵字太長（{len(pattern)} > {MAX_PATTERN_LEN} 字元），請精簡。"
    try:
        return re.compile(pattern, re.IGNORECASE), None
    except re.error:
        return re.compile(re.escape(pattern), re.IGNORECASE), None


def search_lines(lines, regex, budget=TIME_BUDGET_SECONDS):
    """逐行比對、帶時間預算（模型或使用者給的正則可能災難性回溯）。回傳 (hit_indices 0-based, timed_out)。"""
    hits, t0 = [], time.monotonic()
    for i, ln in enumerate(lines):
        if regex.search(ln):
            hits.append(i)
        if (i & 127) == 0 and time.monotonic() - t0 > budget:
            return hits, True
    return hits, False


def clip(line):
    return line if len(line) <= MAX_LINE_CHARS else line[:MAX_LINE_CHARS] + "…"
