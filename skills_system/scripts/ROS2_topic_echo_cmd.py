"""ROS2_topic_echo：讀取 topic 的一筆訊息（--once），或以 --duration 秒 擷取一段時間並整理變化。

一筆訊息只是瞬間；使用者想知道「一段時間內有沒有變化、頻率多少、數值範圍」時，用 --duration：
容器內以 `timeout --preserve-status -s INT <秒> ros2 topic echo <topic>` 收集這段時間的所有訊息（輸出上限
MAX_CAPTURE_BYTES 避免高頻 topic 灌爆），宿主機端把 YAML 文件（以 --- 分隔）逐則解析、攤平成 a.b.c 欄位，
回報則數、頻率、第一則與最後一則原文、每個欄位的變化（數值 min→max、字串有幾種不同值、固定不變的欄位），
讓 Agent 拿到的是可分析的摘要而不是幾百則原始訊息。
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import ros2_exec, pass_or_empty, parse_timeout

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
SAMPLE_CHARS = 320               # 第一則／最後一則原文各保留的字元數（整體回傳要留在工具門檻內）
USAGE = (
    "用法: scripts/ROS2_topic_echo_cmd.py <container_name> <topic_name> [timeout_seconds] [--duration 秒]\n"
    f"  不加 --duration：讀一筆就結束；timeout_seconds 預設 {DEFAULT_TIMEOUT_SECONDS} 秒（低頻 topic 請加大），上限 {MAX_TIMEOUT_SECONDS}。\n"
    f"  --duration 秒（{MIN_DURATION_SECONDS}～{MAX_DURATION_SECONDS}）：持續擷取這段時間的所有訊息並整理變化，適合「觀察一段時間／頻率／有沒有變化」。"
)


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
    return pass_or_empty(out, f"'{topic}' 回傳了空訊息")


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


def summarize_docs(topic, duration, docs, raw_docs):
    n = len(docs) if docs else len(raw_docs)
    lines = [f"[PASS][digest] 觀察 '{topic}' {duration} 秒：收到 {n} 則訊息（約 {n / duration:.2f} Hz）"]
    if raw_docs:
        lines.append(f"第一則：\n{raw_docs[0][:SAMPLE_CHARS]}{'…' if len(raw_docs[0]) > SAMPLE_CHARS else ''}")
        if len(raw_docs) > 1:
            lines.append(f"最後一則：\n{raw_docs[-1][:SAMPLE_CHARS]}{'…' if len(raw_docs[-1]) > SAMPLE_CHARS else ''}")
    if not docs:
        if yaml is None:
            lines.append("（宿主機沒有 PyYAML，無法逐欄位分析；以上為原文樣本）")
        return "\n".join(lines)
    flats = [flatten(d) for d in docs]
    keys = []
    for f in flats:
        for k in f:
            if k not in keys:
                keys.append(k)
    changed, constant = [], []
    for k in keys[:60]:
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
    lines += [f"- {c}" for c in changed[:20]] if changed else ["- 所有欄位在觀察期間都沒有變化"]
    if len(changed) > 20:
        lines.append(f"- …另有 {len(changed) - 20} 個欄位有變化")
    if constant:
        text = "；".join(constant[:10])
        lines.append(f"固定不變：{text}{'；…' if len(constant) > 10 else ''}")
    return "\n".join(lines)


def run_topic_capture(container, topic, duration):
    """擷取 duration 秒的所有訊息並整理成摘要。"""
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
    result = summarize_docs(topic, duration, docs, raw_docs)
    if len(out) >= MAX_CAPTURE_BYTES - 1:
        result += f"\n（輸出達上限 {MAX_CAPTURE_BYTES} 位元組，實際訊息數可能更多；高頻 topic 請縮短 --duration）"
    return result


def parse_cli(argv):
    """回傳 (container, topic, timeout, duration, error)。"""
    args = list(argv)
    duration = None
    if "--duration" in args:
        i = args.index("--duration")
        if i + 1 >= len(args):
            return None, None, None, None, f"[ERROR] --duration 後面需要秒數。\n{USAGE}"
        value, err = parse_timeout(args[i + 1], None, MAX_DURATION_SECONDS, name="--duration")
        if err or value is None or value < MIN_DURATION_SECONDS:
            return None, None, None, None, f"[ERROR] --duration 需介於 {MIN_DURATION_SECONDS}～{MAX_DURATION_SECONDS} 秒，收到: {args[i + 1]!r}\n{USAGE}"
        duration = value
        del args[i:i + 2]
    if len(args) < 2:
        return None, None, None, None, f"[ERROR] 參數不足。\n{USAGE}"
    if args[0].startswith("-") or args[1].startswith("-"):
        return None, None, None, None, f"[ERROR] 參數順序錯誤：前兩個位置參數必須是 <container_name> <topic_name>（收到 {args[0]!r} {args[1]!r}），除了 --duration 沒有其他選項。\n{USAGE}"
    timeout, err = parse_timeout(args[2] if len(args) > 2 else None, DEFAULT_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, name="timeout_seconds")
    if err:
        return None, None, None, None, f"{err}\n{USAGE}"
    return args[0], args[1], timeout, duration, None


if __name__ == "__main__":
    try:
        container, topic, timeout, duration, err = parse_cli(sys.argv[1:])
        if err:
            print(err)
            sys.exit(0)
        print(run_topic_capture(container, topic, duration) if duration else run_topic_echo(container, topic, timeout))
    except Exception as e:
        print(f"[ERROR] ROS2_topic_echo 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
