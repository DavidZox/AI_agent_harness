"""workpackage_send：對任務協調器（orchestrtor）發送多站點 work package。

走 web_console POST /api/send_tasks（→ topic incoming_work_packages，orchestrtor 逐站派給 distribute）；站點可寫代號或語意名稱
（由 /api/topology 對應）；--loop 不接數字＝無限循環（orchestrtor 的 loop_count None）、--loop N＝N 輪。
備用入口 --ros2 [容器]：不經 web_console，在 ROS2 容器內 `ros2 topic pub --once` 直接發布（容器省略＝目前的目標容器）。
查狀態／取消／地圖是別的技能：workpackage_status、workpackage_cancel、overpending_cancel、semantic_map；共用層見 _rmf_common.py。
"""
import json
import os
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _rmf_common import _http, fetch_topology, resolve_stations, station_text, split_url

SEND_PATH = "/api/send_tasks"
TOPIC = "/incoming_work_packages"
ROS2_WAIT_SUBSCRIBER_SECONDS = 10
ROS2_TIMEOUT_SECONDS = 30
TASK_TYPES = ("regular", "charge", "park")
LEVELS = ("normal", "middle", "emergency")

USAGE = (
    "用法: scripts/workpackage_send_cmd.py <task_id|auto> <站點1,站點2,...> [--amr 機器人] [--type regular|charge|park]\n"
    "      [--level normal|middle|emergency] [--weight 整數] [--loop [輪數]] [--desc 描述1,描述2,...] [--ros2 [容器]] [--dry-run] [--url 位址]\n"
    "  站點可寫代號（a3）或語意名稱（加工線通道-6），會自動對應成代號。\n"
    "  循環：--loop（後面不接數字）＝無限循環直到手動刪除；--loop 3＝跑 3 輪；不加 --loop＝只跑一輪。\n"
    "  範例: scripts/workpackage_send_cmd.py WP001 home,a3,a7 --amr tb1　　scripts/workpackage_send_cmd.py 巡邏 home,a3,a7 --amr tb1 --loop"
)

_VALUE_OPTS = {"--amr": "amr", "--type": "type", "--level": "level", "--weight": "weight", "--desc": "desc"}
# 這些是別的技能的選項；小模型記得舊的合併版技能時會帶進來，直接指路
_OTHER_SKILL_OPTS = {"--status": "workpackage_status", "--watch": "workpackage_status", "--cancel": "workpackage_cancel",
                     "--cancel-overpending": "overpending_cancel", "--wait": "overpending_cancel",
                     "--map": "semantic_map", "--stations": "semantic_map", "--robots": "semantic_map"}

# 循環：orchestrtor 的 loop_count None＝循環到手動刪除、正整數＝跑 N 輪（web 介面也是「留空＝無限」）。
# 小模型表達「無限循環」的寫法很多（--loop 不接數字、--loop 0、--loop 無限、--forever…），全部收下；
# 實測舊措辭「不接＝循環到取消」被讀成「不加 --loop」而送出單輪任務，所以規格與 USAGE 改成正面寫法。
_LOOP_FLAGS = ("--loop", "--loops", "--loop-count", "--repeat", "--cycle")
_INFINITE_FLAGS = ("--infinite", "--forever", "--endless", "--loop-forever", "--infinite-loop")
_INFINITE_WORDS = {"0", "-1", "inf", "infinite", "infinity", "forever", "endless", "always", "unlimited", "none", "null",
                   "true", "yes", "on", "∞", "無限", "無限循環", "不停", "永久", "一直", "是"}
_NO_LOOP_WORDS = {"false", "no", "off", "否", "不"}

# ---------------------------------------------------------------- 參數
def parse_args(argv):
    """回傳 (opts, base_url, error)。"""
    argv, base_url, err = split_url(argv)
    if err:
        return None, None, err
    opts = {"task_id": None, "stations": None, "amr": "", "type": "regular", "level": "normal", "weight": "0",
            "loop": False, "loop_count": None, "desc": "", "ros2": None, "dry_run": False}
    positional = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _VALUE_OPTS:
            if i + 1 >= len(argv):
                return None, None, f"[ERROR] {tok} 後面需要一個值。\n{USAGE}"
            opts[_VALUE_OPTS[tok]] = argv[i + 1]
            i += 2
        elif tok == "--dry-run":
            opts["dry_run"] = True
            i += 1
        elif tok == "--ros2":
            # 值可省略＝目前的目標容器（見 _docker_common 的 TARGET_CONTAINER）
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts["ros2"] = argv[i + 1]
                i += 2
            else:
                opts["ros2"] = ""
                i += 1
        elif tok in _LOOP_FLAGS or tok in _INFINITE_FLAGS:
            # 無限循環：--loop 不接數字（或接 0／無限／forever 等字眼）、或 --forever 這類旗標；--loop N＝跑 N 輪
            opts["loop"], opts["loop_count"] = True, None
            i += 1
            if tok in _LOOP_FLAGS and i < len(argv) and not argv[i].startswith("--"):
                val = argv[i].strip().lower()
                if val.isdigit() and int(val) > 0:
                    opts["loop_count"] = int(val)
                    i += 1
                elif val in _INFINITE_WORDS:
                    i += 1
                elif val in _NO_LOOP_WORDS:
                    opts["loop"] = False
                    i += 1
                # 其他 token（例如站點清單）不是輪數，留給位置參數
        elif tok in _OTHER_SKILL_OPTS:
            return None, None, f"[ERROR] {tok} 不是發送的選項，那是 {_OTHER_SKILL_OPTS[tok]} 技能的功能，請改用該技能。\n{USAGE}"
        elif tok.startswith("--"):
            return None, None, f"[ERROR] 不認識的選項 {tok}。\n{USAGE}"
        else:
            positional.append(tok)
            i += 1
    if len(positional) != 2:
        hint = "（看起來是舊版 workitem_est 的 5 個位置參數；新版只有 task_id 與站點兩個位置參數，其餘改用選項）" if len(positional) == 5 else ""
        return None, None, f"[ERROR] 發送需要 2 個位置參數 <task_id> <站點清單>，收到 {len(positional)} 個{hint}。\n{USAGE}"
    opts["task_id"], opts["stations"] = positional
    return opts, base_url, None

# ---------------------------------------------------------------- 組 payload 與回傳文字
def build_payload(opts, topo=None):
    task_id = (opts["task_id"] or "").strip()
    if task_id.lower() == "auto":
        task_id = "WP-" + time.strftime("%Y%m%d-%H%M%S")
    if not task_id:
        return None, None, f"[ERROR] task_id 不可為空（可填 auto 自動產生）。\n{USAGE}"
    tokens = [s.strip() for s in (opts["stations"] or "").split(",") if s.strip()]
    if not tokens:
        return None, None, f"[ERROR] 至少需要一個站點（逗號分隔，例如 home,a3,a7；站點可用 semantic_map --stations 查）。\n{USAGE}"
    stations, notes, err = resolve_stations(tokens, topo)
    if err:
        return None, None, err
    descs = [d.strip() for d in (opts["desc"] or "").split(",")] if opts["desc"] else []
    if len(descs) > len(stations):
        return None, None, f"[ERROR] --desc 有 {len(descs)} 筆描述，但只有 {len(stations)} 站；描述數不可多於站點數。"
    descs += [""] * (len(stations) - len(descs))
    task_type = (opts["type"] or "").strip()
    if task_type not in TASK_TYPES:
        return None, None, f"[ERROR] --type 必須是 {'/'.join(TASK_TYPES)} 之一，收到: {task_type!r}。"
    level = (opts["level"] or "").strip()
    if level not in LEVELS:
        return None, None, f"[ERROR] --level 必須是 {'/'.join(LEVELS)} 之一，收到: {level!r}。"
    try:
        weight = int(str(opts["weight"]).strip())
    except ValueError:
        return None, None, f"[ERROR] --weight 必須是整數（0＝依類型與等級自動計算），收到: {opts['weight']!r}。"
    if weight < 0:
        return None, None, "[ERROR] --weight 不可為負數。"
    amr = (opts["amr"] or "").strip()
    for name, value in (("task_id", task_id), ("--amr", amr), *[("站點", s) for s in stations]):
        if "," in value:
            return None, None, f"[ERROR] {name} 不可含逗號（orchestrtor 派工給 distribute 時以逗號分隔欄位）: {value!r}"
    return {
        "task_id": task_id, "task_type": task_type, "level": level, "weight": weight, "assign_amr": amr,
        "loop": bool(opts["loop"]), "loop_count": opts["loop_count"] if opts["loop"] else None,
        "stops": [{"station": s, "description": d} for s, d in zip(stations, descs)],
    }, notes, None

def loop_text(pkg):
    if not pkg["loop"]:
        return "否（只跑一輪）"
    return f"是（{pkg['loop_count']} 輪）" if pkg["loop_count"] else "是（無限，直到手動刪除）"

def headline(pkg, verb):
    """回傳第一行：把使用者最會核對的幾件事（站數、循環設定、機器人）寫在最前面。整段回傳要控制在工具門檻
    （500 tokens）內——舊版約 570 tokens 會被精簡成「指令已成功執行」，模型看不到 loop: 否，也就無法自我修正。"""
    amr = pkg["assign_amr"] or "交給 distribute 挑"
    return f"[PASS] work package「{pkg['task_id']}」{verb}：{len(pkg['stops'])} 站，循環：{loop_text(pkg)}，機器人：{amr}"

def describe(pkg, topo=None):
    stations = [station_text(s["station"], topo) for s in pkg["stops"]]
    return (f"stops: {' → '.join(stations)}\n"
            f"task_type: {pkg['task_type']}｜level: {pkg['level']}｜weight: {pkg['weight'] if pkg['weight'] else '0（自動計算）'}｜"
            f"loop: {loop_text(pkg)}")

FOLLOW_UP = "後續：workpackage_status <task_id> 看進度、workpackage_status --watch 秒 觀察一段時間、workpackage_cancel <task_id> 撤銷；沒有機器人可承接時停在當前站等待（⏳），不是錯誤。"

# ---------------------------------------------------------------- 送出
def send_http(base_url, pkg, notes, topo):
    status, text, err = _http("POST", base_url, SEND_PATH, [pkg])
    if err:
        return err
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    note = ""
    if isinstance(data, dict):
        details = data.get("details") or []
        echoed = None
        if details:
            try:
                echoed = json.loads(details[0])
            except (ValueError, TypeError):
                echoed = None
        if data.get("count") != 1 or not isinstance(echoed, dict) or echoed.get("task_id") != pkg["task_id"]:
            note = "\n⚠️ web_console 的回應內容與送出的 work package 對不上，請用 --status 確認是否真的收到。"
    mapped = f"站點名稱對應：{'；'.join(notes)}\n" if notes else ""
    # web_console 的回應只是把 payload 回顯一次，摘要成 status／count 即可（整段回傳要留在工具門檻內）
    reply = f"status={data.get('status')}, count={data.get('count')}" if isinstance(data, dict) else text[:200]
    return (f"{headline(pkg, f'已送出（HTTP {status}，web_console 已轉發到 {TOPIC}）')}\n{mapped}{describe(pkg, topo)}\n"
            f"payload: {json.dumps(pkg, ensure_ascii=False)}\nweb_console 回應: {reply}{note}\n{FOLLOW_UP}")

def ros2_pub_command(pkg):
    yaml_arg = "data: '" + json.dumps(pkg, ensure_ascii=False).replace("'", "''") + "'"
    return (f"ros2 topic pub --once -w 1 --max-wait-time-secs {ROS2_WAIT_SUBSCRIBER_SECONDS} "
            f"{TOPIC} std_msgs/msg/String {shlex.quote(yaml_arg)}")

def send_ros2(container, pkg, notes):
    from _docker_common import ros2_exec
    container = (container or "").strip()
    if not container:
        return "[ERROR] --ros2 後面需要容器名稱（可用 docker_containers 查完整名稱）。"
    ok, out, err = ros2_exec(container, ros2_pub_command(pkg), ROS2_TIMEOUT_SECONDS,
                             timeout_hint=(f"最可能是 {TOPIC} 沒有訂閱者（orchestrtor 沒在跑），ros2 topic pub 等不到訂閱者；"
                                           "請先確認 orchestrtor 節點已啟動（ROS2_node_list）。"))
    if not ok:
        return err
    mapped = f"站點名稱對應：{'；'.join(notes)}\n" if notes else ""
    return (f"{headline(pkg, f'已直接發布到 {TOPIC}（容器 {container}，未經 web_console）')}\n{mapped}{describe(pkg)}\n"
            f"payload: {json.dumps(pkg, ensure_ascii=False)}\nros2 輸出: {(out or '').strip()[:300]}\n{FOLLOW_UP}")

def main(argv):
    opts, base_url, err = parse_args(argv)
    if err:
        return err
    topo, _ = fetch_topology(base_url)   # 站點名稱對應與驗證；web_console 拿不到時放行（--ros2 路徑也一樣）
    pkg, notes, err = build_payload(opts, topo)
    if err:
        return err
    container = None
    if opts["ros2"] is not None:
        from _docker_common import resolve_container
        container, err = resolve_container(opts["ros2"], "用法: --ros2 [容器名稱]（省略＝目前的目標容器）")
        if err:
            return err
    if opts["dry_run"]:
        target = f"ros2（容器 {container}）：{ros2_pub_command(pkg)}" if container else f"POST {base_url}{SEND_PATH}"
        mapped = f"站點名稱對應：{'；'.join(notes)}\n" if notes else ""
        return f"{headline(pkg, 'dry-run（尚未送出）')}\n{mapped}{describe(pkg, topo)}\npayload: {json.dumps(pkg, ensure_ascii=False)}\n目標: {target}\n拿掉 --dry-run 即可真正送出。"
    if container:
        return send_ros2(container, pkg, notes)
    return send_http(base_url, pkg, notes, topo)


if __name__ == "__main__":
    try:
        print(main(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] workpackage_send 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
