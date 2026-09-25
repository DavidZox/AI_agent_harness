"""ROS2_topic_echo：讀取 topic 的一筆訊息（--once），或以 --duration 秒 擷取一段時間。

一筆訊息只是瞬間；使用者想知道「一段時間內有沒有變化、頻率多少、數值範圍」時，用 --duration：
容器內以 `timeout --preserve-status -s INT <秒> ros2 topic echo <topic>` 收集這段時間的所有訊息（輸出上限
MAX_CAPTURE_BYTES 避免高頻 topic 灌爆），宿主機端把 YAML 文件（以 --- 分隔）逐則解析、攤平成 a.b.c 欄位，
回報則數、頻率、每個欄位的變化（數值 min→max、字串有幾種不同值、固定不變的欄位），再附上全部原始訊息
（超過 MAX_RAW_OUTPUT_CHARS 時保留頭尾並註明省略幾則；統計仍涵蓋全部）。腳本回傳的是完整資訊，
「哪些重要」不在這裡決定：輸出超過工具回傳門檻時，harness 會交給知道使用者目標與這一步目的的獨立 session
做任務導向擷取（舊版只回固定格式的 [PASS][digest] 摘要，使用者問到摘要沒涵蓋的欄位或某一則就答不出來）。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import ros2_exec, pass_or_empty, parse_timeout, resolve_container, with_target_marker

try:
    import yaml
except ImportError:  # 宿主機沒有 PyYAML 時退回只做粗略統計
    yaml = None

DEFAULT_TIMEOUT_SECONDS = 15
MAX_TIMEOUT_SECONDS = 570        # 低於 Agent_Runner.TOOL_EXEC_TIMEOUT (600)
MIN_DURATION_SECONDS = 2
MAX_DURATION_SECONDS = 300
CAPTURE_GRACE_SECONDS = 20       # ros2 CLI 啟動／discovery 的餘裕，加在 duration 上當作外層逾時
MAX_CAPTURE_BYTES = 400000
MAX_RAW_OUTPUT_CHARS = 12000     # 原始訊息區的篇幅上限：超過就保留最前面與最後面的訊息、註明中間省略幾則（統計仍涵蓋全部）
MAX_CHANGED_FIELDS = 30          # 欄位變化清單最多列幾個欄位
MAX_CONSTANT_FIELDS = 15         # 固定不變欄位最多列幾個
USAGE = (
    "用法: scripts/ROS2_topic_echo_cmd.py [container_name] <topic_name> [timeout_seconds] [--duration 秒] [--where 欄位<值]...\n"
    "  container_name 可省略＝目前的目標容器（docker_open 選定、或最近一次成功操作的容器）；topic 以 / 開頭。\n"
    f"  不加 --duration：讀一筆就結束；timeout_seconds 預設 {DEFAULT_TIMEOUT_SECONDS} 秒（低頻 topic 請加大），上限 {MAX_TIMEOUT_SECONDS}。\n"
    f"  --duration 秒（{MIN_DURATION_SECONDS}～{MAX_DURATION_SECONDS}）：持續擷取這段時間的所有訊息，回傳欄位統計與全部原始訊息，適合「觀察一段時間／頻率／有沒有變化」。\n"
    "  --where 欄位<值（可重複，只能與 --duration 一起用）：由腳本判斷幾則符合、第一則符合的序號與值；運算子 < <= > >= == !=，"
    "欄位用攤平後的名稱（如 voltage、data.step_counter）。"
)

# ---------------------------------------------------------------- --where 門檻判斷（數值交給腳本算，模型照抄）
_OPS = {"<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b, "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b, "<": lambda a, b: a < b, ">": lambda a, b: a > b}
_WHERE_RE = re.compile(r"^\s*([^\s<>=!]+)\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$")


def parse_where(expr):
    """'voltage<24' → {"field","op","value","text"}；大小比較的值必須是數值。回傳 (cond, error)。"""
    m = _WHERE_RE.match(expr or "")
    if not m:
        return None, (f"[ERROR] --where 條件格式應為 欄位<值、欄位>=值、欄位==值（例如 voltage<24、data.status==RUNNING），收到 {expr!r}；"
                      f"只是想看某個欄位有沒有變化不需要 --where，--duration 的「欄位變化」統計本來就會列出每個欄位的變化")
    field, op, val = m.groups()
    val = val.strip().strip('"').strip("'")
    try:
        num = float(val)
    except ValueError:
        num = None
    if op in ("<", "<=", ">", ">=") and num is None:
        return None, f"[ERROR] --where {expr!r}：大小比較需要數值，收到 {val!r}"
    return {"field": field, "op": op, "value": num if num is not None else val, "text": f"{field}{op}{val}"}, None


def _fmt_val(v):
    return _num(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else _fmt(v)


def _ranges(indices):
    """[17,18,19,20,25] → '17～20、25'。"""
    out, start, prev = [], None, None
    for i in indices + [None]:
        if start is None:
            start = prev = i
            continue
        if i is not None and i == prev + 1:
            prev = i
            continue
        out.append(f"{start}～{prev}" if prev != start else f"{start}")
        start = prev = i
    return "、".join(out)


def render_where(cond, flats):
    """一行結論：N 則中幾則符合、第一則符合 #k（值）、前一則不符合的值、序號分佈。序號與原始訊息的 #序號一致。"""
    n = len(flats)
    if not any(cond["field"] in f for f in flats):
        keys = sorted({k for f in flats for k in f})
        return f"條件 {cond['text']}：找不到欄位「{cond['field']}」（可用欄位：{', '.join(keys[:25])}{'…' if len(keys) > 25 else ''}）"
    matched = []
    for i, f in enumerate(flats, 1):
        if cond["field"] not in f:
            continue
        v = f[cond["field"]]
        if isinstance(cond["value"], float):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            ok = _OPS[cond["op"]](float(v), cond["value"])
        else:
            ok = _OPS[cond["op"]](str(v), str(cond["value"]))
        if ok:
            matched.append(i)
    if not matched:
        return f"條件 {cond['text']}：{n} 則中 0 則符合"
    first = matched[0]
    parts = [f"條件 {cond['text']}：{n} 則中 {len(matched)} 則符合",
             f"第一則符合 #{first}（{cond['field']}={_fmt_val(flats[first - 1].get(cond['field']))}）"]
    if first > 1:
        parts.append(f"前一則 #{first - 1} 不符合（{cond['field']}={_fmt_val(flats[first - 2].get(cond['field']))}）")
    parts.append("之後連續符合到最後一則" if matched == list(range(first, n + 1)) else f"符合的序號：{_ranges(matched)}")
    return "；".join(parts)


def run_topic_echo(container, topic, timeout=DEFAULT_TIMEOUT_SECONDS):
    """讀取一筆訊息即結束（--once）。"""
    container = (container or "").strip()
    topic = (topic or "").strip()
    if not container or not topic:
        return f"[ERROR] 參數不足，需要容器名稱與 topic 名稱。\n{USAGE}"
    ok, out, err = ros2_exec(
        container, f"ros2 topic echo {topic} --once", timeout,
        timeout_hint=(f"在 {timeout} 秒內沒有收到 '{topic}' 的任何訊息：可能目前沒有 publisher 在發布、"
                      f"topic 名稱錯誤（請用 ROS2_topic_list 確認）、或 QoS 不相容；低頻 topic 可加大第三個參數的秒數。"),
    )
    if not ok:
        return err
    body = pass_or_empty(out, f"'{topic}' 回傳了空訊息")
    return body if body.startswith("[PASS]") else f"[PASS] '{topic}' 的一筆訊息:\n{body.strip()}"


# ---------------------------------------------------------------- 一段時間的擷取與摘要
def split_yaml_docs(text):
    """ros2 topic echo 的輸出以單獨一行 --- 分隔每則訊息；最後一則可能因輸出上限被截斷，解析失敗就丟掉。"""
    docs, raw_docs = [], []
    # ros2 CLI 的提示行（例如 "WARNING: topic [...] does not appear to be published yet"）不是訊息
    text = "\n".join(ln for ln in (text or "").splitlines() if not ln.startswith(("WARNING:", "[WARN", "[INFO", "[ERROR")))
    for chunk in re.split(r"^---\s*$", text, flags=re.M):
        chunk = chunk.strip()
        if not chunk:
            continue
        if yaml is None:
            raw_docs.append(chunk)
            continue
        try:
            parsed = yaml.safe_load(chunk)
        except yaml.YAMLError:
            continue
        if isinstance(parsed, dict):
            docs.append(parsed)
            raw_docs.append(chunk)   # 只保留解析成功的原文，被截斷的最後一則不算
    return docs, raw_docs


def flatten(obj, prefix=""):
    """巢狀 dict → {"a.b": value}；數值陣列原樣保留（可比對變動），其他 list 只記長度。"""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
        return out
    key = prefix[:-1] if prefix.endswith(".") else prefix
    if isinstance(obj, str) and obj[:1] in ("{", "[") and len(obj) < 200000:
        # std_msgs/String 常裝 JSON（例如 *_ui_state），解開來逐欄位比對才有意義
        try:
            import json
            inner = json.loads(obj)
        except ValueError:
            inner = None
        if isinstance(inner, dict):
            return flatten(inner, key + ".")
    if isinstance(obj, list):
        if obj and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in obj):
            out[key] = obj
        else:
            out[key] = f"<list len={len(obj)}>"
    else:
        out[key] = obj
    return out


def _fmt(v):
    s = str(v)
    return s if len(s) <= 80 else s[:80] + "…"


def _num(v):
    """數值顯示：整數原樣（step_counter 130890 不能變 1.309e+05）；小數最多 4 位、去掉尾巴 0；極大／極小才用科學記號。"""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, int):
        return str(v)
    if v == 0 or 1e-4 <= abs(v) < 1e7:
        return f"{v:.4f}".rstrip("0").rstrip(".")
    return f"{v:.4g}"


def render_raw_messages(raw_docs, max_chars=MAX_RAW_OUTPUT_CHARS):
    """全部原始訊息依接收順序列出（--- #序號）。總篇幅超過 max_chars 時保留最前面與最後面各約一半篇幅的訊息，
    中間以一行註明省略了第幾到第幾則；單則就超過篇幅時截斷該則並註明。回傳 (lines, omitted_count)。"""
    n = len(raw_docs)
    items = [f"--- #{k + 1}\n{d}" for k, d in enumerate(raw_docs)]
    if sum(len(x) + 1 for x in items) <= max_chars:
        return [f"原始訊息（共 {n} 則，依接收順序）："] + items, 0
    half = max_chars // 2
    head, used = [], 0
    while len(head) < n and used + len(items[len(head)]) + 1 <= half:
        used += len(items[len(head)]) + 1
        head.append(items[len(head)])
    tail, used = [], 0
    while len(head) + len(tail) < n and used + len(items[n - 1 - len(tail)]) + 1 <= half:
        used += len(items[n - 1 - len(tail)]) + 1
        tail.insert(0, items[n - 1 - len(tail)])
    if not head:  # 第一則就超過一半篇幅：截斷後仍要給（第一則通常最能說明訊息結構）
        head = [items[0][:half] + "\n…（單則訊息過長，已截斷）"]
    omitted = n - len(head) - len(tail)
    if omitted <= 0:
        return [f"原始訊息（共 {n} 則，依接收順序）："] + head + tail, 0
    lines = [f"原始訊息（共 {n} 則，依接收順序；篇幅限制只列出前 {len(head)} 則與最後 {len(tail)} 則、"
             f"中間省略 {omitted} 則，上方欄位統計仍涵蓋全部訊息）："]
    return lines + head + [f"…（省略第 {len(head) + 1}～{n - len(tail)} 則，共 {omitted} 則）…"] + tail, omitted


def summarize_docs(topic, duration, docs, raw_docs, wheres=None):
    """狀態列 + 欄位統計（涵蓋全部訊息）+ --where 門檻判斷 + 全部原始訊息。"""
    n = len(docs) if docs else len(raw_docs)
    lines = [f"[PASS] 觀察 '{topic}' {duration} 秒：收到 {n} 則訊息（約 {n / duration:.2f} Hz）"]
    if docs:
        flats = [flatten(d) for d in docs]
        keys = []
        for f in flats:
            for k in f:
                if k not in keys:
                    keys.append(k)
        changed, constant = [], []
        for k in keys[:80]:
            values = [f.get(k) for f in flats if k in f]
            if not values:
                continue
            distinct = []
            for v in values:
                if v not in distinct:
                    distinct.append(v)
            if len(distinct) <= 1:
                constant.append(f"{k}={_fmt(values[0])}")
                continue
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
                changed.append(f"{k}：數值 {_num(values[0])} → {_num(values[-1])}（最小 {_num(min(values))}、最大 {_num(max(values))}、{len(distinct)} 種值）")
            elif all(isinstance(v, list) for v in values):
                changed.append(f"{k}：數值陣列變動 {len(distinct)} 次（長度 {len(values[-1])}）")
            else:
                changed.append(f"{k}：{len(distinct)} 種不同值，最後 = {_fmt(values[-1])}" + (f"（例：{'、'.join(_fmt(v) for v in distinct[:3])}）" if len(distinct) <= 6 else ""))
        lines.append(f"欄位變化（{len(docs)} 則、{len(keys)} 個欄位）：")
        lines += [f"- {c}" for c in changed[:MAX_CHANGED_FIELDS]] if changed else ["- 所有欄位在觀察期間都沒有變化"]
        if len(changed) > MAX_CHANGED_FIELDS:
            lines.append(f"- …另有 {len(changed) - MAX_CHANGED_FIELDS} 個欄位有變化")
        if constant:
            lines.append(f"固定不變：{'；'.join(constant[:MAX_CONSTANT_FIELDS])}{'；…' if len(constant) > MAX_CONSTANT_FIELDS else ''}")
        for cond in (wheres or []):
            lines.append(render_where(cond, flats))
    elif yaml is None:
        lines.append("（宿主機沒有 PyYAML，無法逐欄位統計；以下為原始訊息）")
        if wheres:
            lines.append("（沒有 PyYAML 無法評估 --where 條件）")
    raw_lines, _ = render_raw_messages(raw_docs)
    return "\n".join(lines + raw_lines)


def run_topic_capture(container, topic, duration, wheres=None):
    """擷取 duration 秒的所有訊息：狀態列 + 欄位統計 + --where 判斷 + 全部原始訊息。"""
    container = (container or "").strip()
    topic = (topic or "").strip()
    if not container or not topic:
        return f"[ERROR] 參數不足，需要容器名稱與 topic 名稱。\n{USAGE}"
    import shlex
    # --preserve-status + `; true`：時間到被 INT 終止是預期行為，不能讓 docker_exec 當成錯誤；head -c 限制輸出量
    # --full-length：ros2 預設把字串截到 128 字元，String 型 JSON 狀態 topic 會被切掉；輸出量由 head -c 與 SAMPLE_CHARS 控制
    cmd = f"timeout --preserve-status -s INT {duration} ros2 topic echo --full-length {shlex.quote(topic)} | head -c {MAX_CAPTURE_BYTES}; true"
    ok, out, err = ros2_exec(
        container, cmd, duration + CAPTURE_GRACE_SECONDS,
        timeout_hint=f"擷取 {duration} 秒外加 {CAPTURE_GRACE_SECONDS} 秒餘裕仍未結束，容器內的 ros2 CLI 可能卡住。",
    )
    if not ok:
        return err
    docs, raw_docs = split_yaml_docs(out)
    if not raw_docs:
        return (f"[PASS] 觀察 '{topic}' {duration} 秒：沒有收到任何訊息。可能目前沒有 publisher 在發布、topic 名稱錯誤"
                f"（請用 ROS2_topic_list 確認）、QoS 不相容，或發布間隔比 {duration} 秒還長。")
    result = summarize_docs(topic, duration, docs, raw_docs, wheres)
    if len(out) >= MAX_CAPTURE_BYTES - 1:
        result += f"\n（輸出達上限 {MAX_CAPTURE_BYTES} 位元組，實際訊息數可能更多；高頻 topic 請縮短 --duration）"
    return result


def parse_cli(argv):
    """回傳 (container, topic, timeout, duration, error, wheres)。wheres 是 parse_where 解析後的條件清單。"""
    args = list(argv)
    duration = None
    wheres = []
    while "--where" in args:
        i = args.index("--where")
        if i + 1 >= len(args):
            return None, None, None, None, f"[ERROR] --where 後面需要條件（例如 voltage<24）。\n{USAGE}", []
        cond, err = parse_where(args[i + 1])
        if err:
            return None, None, None, None, f"{err}\n{USAGE}", []
        wheres.append(cond)
        del args[i:i + 2]
    if "--duration" in args:
        i = args.index("--duration")
        if i + 1 >= len(args):
            return None, None, None, None, f"[ERROR] --duration 後面需要秒數。\n{USAGE}", []
        value, err = parse_timeout(args[i + 1], None, MAX_DURATION_SECONDS, name="--duration")
        if err or value is None or value < MIN_DURATION_SECONDS:
            return None, None, None, None, f"[ERROR] --duration 需介於 {MIN_DURATION_SECONDS}～{MAX_DURATION_SECONDS} 秒，收到: {args[i + 1]!r}\n{USAGE}", []
        duration = value
        del args[i:i + 2]
    if wheres and not duration:
        return None, None, None, None, f"[ERROR] --where 只能與 --duration 一起用（要有一段時間的訊息才有得判斷）。\n{USAGE}", []
    bad = [a for a in args if a.startswith("-")]
    if bad:
        return None, None, None, None, f"[ERROR] 不認識的選項 {bad[0]!r}：位置參數依序是 [container_name] <topic_name> [timeout_seconds]，選項只有 --duration 與 --where。\n{USAGE}", []
    if not args:
        return None, None, None, None, f"[ERROR] 參數不足，需要 topic 名稱。\n{USAGE}", []
    # 判斷第一個參數是容器還是 topic：只有一個參數、第一個以 / 開頭（topic 名稱）、或形如「<topic> <秒數>」時，容器省略＝目標容器
    if len(args) == 1 or args[0].startswith("/") or (len(args) == 2 and args[1].isdigit()):
        container, rest = "", args
    else:
        container, rest = args[0], args[1:]
    container, err = resolve_container(container, USAGE)
    if err:
        return None, None, None, None, err, []
    timeout, err = parse_timeout(rest[1] if len(rest) > 1 else None, DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, name="timeout_seconds")
    if err:
        return None, None, None, None, f"{err}\n{USAGE}", []
    return container, rest[0], timeout, duration, None, wheres


if __name__ == "__main__":
    try:
        container, topic, timeout, duration, err, wheres = parse_cli(sys.argv[1:])
        if err:
            print(err)
            sys.exit(0)
        out = run_topic_capture(container, topic, duration, wheres) if duration else run_topic_echo(container, topic, timeout)
        print(out if out.lstrip().startswith("[ERROR]") else with_target_marker(out, container))
    except Exception as e:
        print(f"[ERROR] ROS2_topic_echo 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
