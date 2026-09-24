"""workpackage_est：對任務協調器（orchestrtor）發送、取消、查詢 work package。

對齊 fih_rmf_system（amr_base/src/fih_rmf_system）的 distribute_task.FastAPIBridgeNode 全部對外能力：
  1) 轉發 work package      POST   /api/send_tasks              → topic incoming_work_packages
  2) 刪除 work package      DELETE /api/work_packages/{id}      → topic cancel_work_package
  3) 刪除 OverPending 任務  DELETE /api/overpending_tasks/{id}  → topic cancel_overpending_task
  4) 鏡射狀態快照           WebSocket /api/ws（每 0.5 秒推 distribute 快照，orchestrtor 快照放在 "orchestrtor" 鍵）
  5) 場域參數（map_*）是 ROS 參數、不可經 API 設定，這裡只提供 GET /api/stations、/api/robots 查目前場域的站點與機隊。
web_console（FastAPI，port 8020）在容器內以 network_mode: host 執行，宿主機直接連 localhost:8020。
備用入口 --ros2 <容器>：不經 web_console，在 ROS2 容器內以 `ros2 topic pub --once` 直接把 work package JSON 發布到
/incoming_work_packages（-w 1 等到有訂閱者才送）。

work package JSON（orchestrtor_node.work_package_callback 讀取的鍵，與網頁表單 submitTasks 送出的完全相同）：
  task_id（=package_id，重複的會被 orchestrtor 靜默忽略）、task_type（regular|charge|park）、level（normal|middle|emergency）、
  weight（0＝依 type/level 由 distribute 自動算）、assign_amr（空＝交給 distribute 挑）、loop、loop_count（null＝循環到手動刪除）、
  stops=[{station, description}]。所有欄位不可含逗號：orchestrtor 派工給 distribute 時以逗號串欄位。

回傳以 [PASS]/[ERROR] 開頭（專案慣例）。純標準庫（urllib + 迷你 WebSocket 客戶端），不依賴 requests／websockets。
"""
import base64
import json
import os
import shlex
import socket
import struct
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

DEFAULT_URL = os.environ.get("RMF_WEB_CONSOLE_URL", "http://localhost:8020").rstrip("/")
SEND_PATH = "/api/send_tasks"
TOPIC = "/incoming_work_packages"
HTTP_TIMEOUT_SECONDS = 15      # urllib 只有單一逾時（連線 + 回應）
WS_TIMEOUT_SECONDS = 6         # web_console 每 0.5 秒推一幀，6 秒收不到就是沒在推
ROS2_WAIT_SUBSCRIBER_SECONDS = 10
ROS2_TIMEOUT_SECONDS = 30

TASK_TYPES = ("regular", "charge", "park")            # distribute type_weights / 網頁表單選項
LEVELS = ("normal", "middle", "emergency")            # 網頁表單選項（tuning.yaml 只定義 normal/emergency 倍率，middle 視為 1.0）

USAGE = (
    "用法:\n"
    "  發送: scripts/workpackage_est_cmd.py <task_id|auto> <站點1,站點2,...> [--amr 機器人] [--type regular|charge|park]\n"
    "        [--level normal|middle|emergency] [--weight 整數] [--loop [輪數]] [--desc 描述1,描述2,...] [--ros2 容器名稱] [--dry-run]\n"
    "  取消: scripts/workpackage_est_cmd.py --cancel <task_id>            （orchestrtor 停止派下一站）\n"
    "        scripts/workpackage_est_cmd.py --cancel-overpending <task_id> （distribute 的 OverPending 佇列）\n"
    "  查詢: scripts/workpackage_est_cmd.py --status [task_id] | --stations | --robots\n"
    "  共用: [--url http://localhost:8020]\n"
    "  範例: scripts/workpackage_est_cmd.py WP001 home,a3,a7 --amr tb1\n"
    "        scripts/workpackage_est_cmd.py auto a3,a7 --loop 3 --weight 5 --desc \"取料,卸料\"\n"
    "        scripts/workpackage_est_cmd.py --status WP001"
)

_VALUE_OPTS = {"--amr": "amr", "--type": "type", "--level": "level", "--weight": "weight",
               "--desc": "desc", "--url": "url", "--ros2": "ros2",
               "--cancel": "cancel", "--cancel-overpending": "cancel_overpending"}
_FLAG_OPTS = {"--dry-run": "dry_run", "--stations": "stations_mode", "--robots": "robots_mode"}


# ---------------------------------------------------------------- 參數
def parse_args(argv):
    """回傳 (opts, error)。發送模式有兩個位置參數（task_id、stations）；其他模式由選項決定。"""
    opts = {"task_id": None, "stations": None, "amr": "", "type": "regular", "level": "normal", "weight": "0",
            "loop": False, "loop_count": None, "desc": "", "url": DEFAULT_URL, "ros2": None, "dry_run": False,
            "cancel": None, "cancel_overpending": None, "status": None, "status_mode": False,
            "stations_mode": False, "robots_mode": False}
    positional = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _VALUE_OPTS:
            if i + 1 >= len(argv):
                return None, f"[ERROR] {tok} 後面需要一個值。\n{USAGE}"
            opts[_VALUE_OPTS[tok]] = argv[i + 1]
            i += 2
        elif tok in _FLAG_OPTS:
            opts[_FLAG_OPTS[tok]] = True
            i += 1
        elif tok == "--status":
            opts["status_mode"] = True
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts["status"] = argv[i + 1]
                i += 2
            else:
                i += 1
        elif tok == "--loop":
            opts["loop"] = True
            if i + 1 < len(argv) and argv[i + 1].isdigit():
                opts["loop_count"] = int(argv[i + 1])
                i += 2
            else:
                i += 1
        elif tok.startswith("--"):
            return None, f"[ERROR] 不認識的選項 {tok}。\n{USAGE}"
        else:
            positional.append(tok)
            i += 1

    modes = [m for m, on in (("--cancel", opts["cancel"]), ("--cancel-overpending", opts["cancel_overpending"]),
                             ("--status", opts["status_mode"]), ("--stations", opts["stations_mode"]),
                             ("--robots", opts["robots_mode"])) if on]
    if len(modes) > 1:
        return None, f"[ERROR] 一次只能做一件事，收到 {', '.join(modes)}。\n{USAGE}"
    if modes:
        if positional:
            return None, f"[ERROR] {modes[0]} 模式不接受位置參數（收到 {positional}）。\n{USAGE}"
        return opts, None
    if len(positional) != 2:
        hint = ""
        if len(positional) == 5:
            hint = "（看起來是舊版 workitem_est 的 5 個位置參數；新版只有 task_id 與站點兩個位置參數，其餘改用選項）"
        return None, f"[ERROR] 發送需要 2 個位置參數 <task_id> <站點清單>，收到 {len(positional)} 個{hint}。\n{USAGE}"
    opts["task_id"], opts["stations"] = positional
    return opts, None


def build_payload(opts):
    """依 opts 組成一筆 work package dict（orchestrtor 讀取的鍵）。回傳 (payload, error)。"""
    task_id = (opts["task_id"] or "").strip()
    if task_id.lower() == "auto":
        task_id = "WP-" + time.strftime("%Y%m%d-%H%M%S")
    if not task_id:
        return None, f"[ERROR] task_id 不可為空（可填 auto 自動產生）。\n{USAGE}"
    stations = [s.strip() for s in (opts["stations"] or "").split(",") if s.strip()]
    if not stations:
        return None, f"[ERROR] 至少需要一個站點（逗號分隔，例如 home,a3,a7；站點名稱可用 --stations 查）。\n{USAGE}"
    descs = [d.strip() for d in (opts["desc"] or "").split(",")] if opts["desc"] else []
    if len(descs) > len(stations):
        return None, f"[ERROR] --desc 有 {len(descs)} 筆描述，但只有 {len(stations)} 站；描述數不可多於站點數。"
    descs += [""] * (len(stations) - len(descs))
    task_type = (opts["type"] or "").strip()
    if task_type not in TASK_TYPES:
        return None, f"[ERROR] --type 必須是 {'/'.join(TASK_TYPES)} 之一，收到: {task_type!r}。"
    level = (opts["level"] or "").strip()
    if level not in LEVELS:
        return None, f"[ERROR] --level 必須是 {'/'.join(LEVELS)} 之一，收到: {level!r}。"
    try:
        weight = int(str(opts["weight"]).strip())
    except ValueError:
        return None, f"[ERROR] --weight 必須是整數（0＝依類型與等級自動計算），收到: {opts['weight']!r}。"
    if weight < 0:
        return None, "[ERROR] --weight 不可為負數。"
    if opts["loop_count"] is not None and opts["loop_count"] <= 0:
        return None, "[ERROR] --loop 後面的輪數必須是正整數（不接數字＝循環到手動刪除）。"
    amr = (opts["amr"] or "").strip()
    for name, value in (("task_id", task_id), ("--amr", amr), *[("站點", s) for s in stations]):
        if "," in value:
            return None, f"[ERROR] {name} 不可含逗號（orchestrtor 派工給 distribute 時以逗號分隔欄位）: {value!r}"
    return {
        "task_id": task_id,
        "task_type": task_type,
        "level": level,
        "weight": weight,
        "assign_amr": amr,
        "loop": bool(opts["loop"]),
        "loop_count": opts["loop_count"] if opts["loop"] else None,
        "stops": [{"station": s, "description": d} for s, d in zip(stations, descs)],
    }, None


def describe(pkg):
    stations = [s["station"] for s in pkg["stops"]]
    loop = ("是（%s）" % (f"{pkg['loop_count']} 輪" if pkg["loop_count"] else "直到手動刪除")) if pkg["loop"] else "否"
    return (
        f"task_id: {pkg['task_id']}\n"
        f"stops: {' → '.join(stations)}（{len(stations)} 站）\n"
        f"assign_amr: {pkg['assign_amr'] or '（空，交給 distribute 挑機器人）'}｜task_type: {pkg['task_type']}｜"
        f"level: {pkg['level']}｜weight: {pkg['weight'] if pkg['weight'] else '0（依類型與等級自動計算）'}｜loop: {loop}"
    )


FOLLOW_UP = (
    "後續：orchestrtor 會依序把每一站派給 distribute（incoming_tasks），完成判斷靠機器人回報 /{rid}/task_exec_fin；"
    "沒有機器人可承接時工作包會停在當前站等待。用 --status <task_id> 追蹤進度，--cancel <task_id> 撤銷。"
)


# ---------------------------------------------------------------- HTTP 共用
def _http(method, base_url, path, body=None, timeout=HTTP_TIMEOUT_SECONDS):
    """回傳 (status, text, error)。error 為 [ERROR] 字串（連線／逾時／非 2xx），成功時為 None。"""
    url = base_url.rstrip("/") + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), None
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        if e.code == 503:
            return e.code, detail, f"[ERROR] web_console 回應 HTTP 503：ROS 2 橋接節點尚未就緒（web_console 剛啟動或 rclpy 未初始化），稍候幾秒再試一次。{detail}"
        if e.code == 422:
            return e.code, detail, f"[ERROR] web_console 回應 HTTP 422（欄位驗證失敗），請依 detail 修正參數，勿原樣重試: {detail}"
        if e.code == 404:
            return e.code, detail, f"[ERROR] web_console 回應 HTTP 404：{url} 不存在（web_console 版本不含這個端點？）: {detail}"
        return e.code, detail, f"[ERROR] web_console 回應 HTTP {e.code}: {detail}"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
            return None, "", f"[ERROR] 連線／等待 web_console 回應逾時（{timeout} 秒）：{url}。服務可能卡住，請確認 web_console 狀態後再試。"
        return None, "", (f"[ERROR] 無法連線到 web_console {url}（{reason}）。web_console 由 rmf_launch.py 帶起、port 8020、"
                          f"容器為 network_mode: host；請確認它有在跑，或改用 --ros2 <容器名稱> 直接發布 topic。")
    except (socket.timeout, TimeoutError):
        return None, "", f"[ERROR] 連線／等待 web_console 回應逾時（{timeout} 秒）：{url}。"


# ---------------------------------------------------------------- 迷你 WebSocket 客戶端（只讀一則文字訊息）
def ws_read_json(base_url, path="/api/ws", timeout=WS_TIMEOUT_SECONDS):
    """連上 web_console 的 WebSocket，收第一則完整的文字訊息後解析 JSON 回傳 (data, error)。
    伺服器每 0.5 秒推一幀快照，所以拿到第一幀就是「目前狀態」；讀完直接關 socket。"""
    u = urlparse(base_url)
    host, port = u.hostname or "localhost", u.port or (443 if u.scheme == "https" else 80)
    if u.scheme == "https":
        return None, "[ERROR] --status 目前只支援 http:// 的 web_console。"
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(handshake)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    return None, "[ERROR] web_console 在 WebSocket 握手時關閉了連線。"
                buf += chunk
            head, data = buf.split(b"\r\n\r\n", 1)
            status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
            if " 101 " not in status_line:
                return None, f"[ERROR] web_console 拒絕 WebSocket 升級（{status_line}）；{path} 可能不存在或不是 web_console。"

            def need(n):
                nonlocal data
                while len(data) < n:
                    chunk = s.recv(65536)
                    if not chunk:
                        raise ConnectionError("連線在訊息結束前被關閉")
                    data += chunk

            message = b""
            while True:
                need(2)
                b0, b1 = data[0], data[1]
                fin, opcode, masked, length = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F
                offset = 2
                if length == 126:
                    need(4)
                    length = struct.unpack("!H", data[2:4])[0]
                    offset = 4
                elif length == 127:
                    need(10)
                    length = struct.unpack("!Q", data[2:10])[0]
                    offset = 10
                mask = b""
                if masked:
                    need(offset + 4)
                    mask = data[offset:offset + 4]
                    offset += 4
                need(offset + length)
                payload = data[offset:offset + length]
                data = data[offset + length:]
                if masked:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 8:
                    return None, "[ERROR] web_console 送出 WebSocket close，沒有收到任何狀態快照。"
                if opcode in (9, 10):   # ping / pong：略過（我們馬上就要關連線）
                    continue
                if opcode in (1, 0):    # text（或後續分片）
                    message += payload
                    if fin:
                        break
                    continue
                # binary 或其他：忽略
                if fin:
                    message = b""
            try:
                s.sendall(b"\x88\x80" + os.urandom(4))  # 禮貌地送 close（masked、空 payload）
            except OSError:
                pass
    except socket.timeout:
        return None, f"[ERROR] WebSocket {timeout} 秒內沒收到快照：web_console 有在跑但沒有推送（/api/ws 異常）。"
    except (ConnectionRefusedError, OSError) as e:
        return None, f"[ERROR] 無法連線到 web_console {base_url}（{e}）。請確認它有在跑（rmf_launch.py 帶起、port 8020）。"
    try:
        return json.loads(message.decode("utf-8")), None
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"[ERROR] 快照不是合法 JSON：{e}"


# ---------------------------------------------------------------- 各模式
def send_http(base_url, pkg):
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
    return (
        f"[PASS] work package 已送出（HTTP {status}，web_console 已轉發到 {TOPIC}）\n"
        f"{describe(pkg)}\n"
        f"payload: {json.dumps(pkg, ensure_ascii=False)}\n"
        f"web_console 回應: {text[:600]}{note}\n"
        f"{FOLLOW_UP}"
    )


def ros2_pub_command(pkg):
    """在容器內直接發布到 /incoming_work_packages 的 ros2 指令（YAML 單引號字串內的 ' 要寫成 ''）。"""
    yaml_arg = "data: '" + json.dumps(pkg, ensure_ascii=False).replace("'", "''") + "'"
    return (f"ros2 topic pub --once -w 1 --max-wait-time-secs {ROS2_WAIT_SUBSCRIBER_SECONDS} "
            f"{TOPIC} std_msgs/msg/String {shlex.quote(yaml_arg)}")


def send_ros2(container, pkg):
    from _docker_common import ros2_exec  # 與其他容器技能共用逾時／錯誤翻譯
    container = (container or "").strip()
    if not container:
        return "[ERROR] --ros2 後面需要容器名稱（可用 docker_containers 查完整名稱）。"
    ok, out, err = ros2_exec(
        container, ros2_pub_command(pkg), ROS2_TIMEOUT_SECONDS,
        timeout_hint=(f"最可能是 {TOPIC} 沒有訂閱者（orchestrtor 沒在跑），ros2 topic pub 等不到訂閱者；"
                      "請先確認 orchestrtor 節點已啟動（ROS2_node_list）。"),
    )
    if not ok:
        return err
    return (
        f"[PASS] work package 已直接發布到 {TOPIC}（容器 {container}，未經 web_console）\n"
        f"{describe(pkg)}\n"
        f"payload: {json.dumps(pkg, ensure_ascii=False)}\n"
        f"ros2 輸出: {(out or '').strip()[:300]}\n"
        f"{FOLLOW_UP}"
    )


def _package_line(p):
    cur = p.get("current_item") or {}
    idx, total = p.get("station_index", 0), p.get("station_count") or len(p.get("stations") or [])
    stations = p.get("stations") or []
    station = cur.get("station") or (stations[idx] if idx < len(stations) else "?")
    if p.get("status") == "COMPLETED":
        progress = f"已完成（共 {p.get('iteration', 0)} 輪）"
    else:
        progress = f"第 {idx + 1}/{total} 站 {station}"
        if cur:
            robot = f"機器人 {cur.get('robot_id')}" if cur.get("robot_id") else ("⏳ 等待機器人" if cur.get("waiting_for_robot") else "機器人未知")
            progress += f"，工作項 {cur.get('status')}，{robot}"
    loop = ""
    if p.get("loop"):
        loop = f"｜循環 {p.get('loop_count') or '∞'} 輪，目前第 {p.get('iteration', 0) + 1} 輪"
    return (f"- {p.get('package_id')} {p.get('status')}：{progress}{loop}｜完成 {p.get('items_done', 0)}/{p.get('items_total', 0)} 項"
            f"｜路線 {' → '.join(stations)}")


def render_status(data, task_id=None):
    orch = data.get("orchestrtor") or {}
    packages = orch.get("packages") or []
    lines = [f"[PASS] 目前狀態（web_console /api/ws 快照 distribute #{data.get('step_counter', '?')} / orchestrtor #{orch.get('step_counter', '?')}）"]
    if task_id:
        hits = [p for p in packages if p.get("package_id") == task_id]
        if not hits:
            lines.append(f"orchestrtor 清單中沒有 work package {task_id}：可能未送達、已被刪除，或 orchestrtor 沒在跑"
                         f"（目前清單 {len(packages)} 個{'：' + '、'.join(p.get('package_id', '?') for p in packages[:10]) if packages else ''}）。")
        else:
            lines += [_package_line(p) for p in hits]
        related = [t for key in ("processing_tasks", "raw_tasks", "overpending_tasks", "buffer_tasks")
                   for t in (data.get(key) or []) if str(t.get("id", "")).startswith(f"{task_id}::")]
        if related:
            lines.append("distribute 佇列中相關的工作項：" + "；".join(
                f"{t.get('id')}" + (f" → {t.get('robot')}" if t.get("robot") else "") + (f"（等待 {t.get('wait')}s）" if "wait" in t else "")
                for t in related))
    else:
        if not orch:
            lines.append("orchestrtor 快照尚未收到（orchestrtor 可能沒在跑，或 web_console 剛啟動）。")
        else:
            running = sum(1 for p in packages if p.get("status") == "RUNNING")
            lines.append(f"orchestrtor work packages：{len(packages)} 個（RUNNING {running}，COMPLETED {len(packages) - running}）")
            lines += [_package_line(p) for p in packages[:15]]
        ovp = data.get("overpending_tasks") or []
        proc = data.get("processing_tasks") or []
        lines.append(f"distribute：狀態 {data.get('state', '?')}｜buffer {len(data.get('buffer_tasks') or [])}｜raw {len(data.get('raw_tasks') or [])}"
                     f"｜processing {len(proc)}" + (f"（{'、'.join(f'{t.get('id')}→{t.get('robot') or '-'}' for t in proc[:5])}）" if proc else "")
                     + f"｜overpending {len(ovp)}" + (f"（{'、'.join(f'{t.get('id')} 等待 {t.get('wait')}s' for t in ovp[:5])}，可用 --cancel-overpending 刪除）" if ovp else ""))
        robots = data.get("robots") or {}
        if robots:
            lines.append("機器人：" + "；".join(
                f"{rid} {info.get('status', '?')} {info.get('state', '?')}" + (f"（{info.get('last_task_id')}）" if info.get('last_task_id') not in (None, 'None') else "")
                for rid, info in list(robots.items())[:8]))
    for label, events in (("orchestrtor", orch.get("events") or []), ("distribute", data.get("events") or [])):
        if events:
            lines.append(f"最近事件（{label}）：" + " ｜ ".join(events[-3:]))
    return "\n".join(lines)


def status_mode(base_url, task_id=None):
    data, err = ws_read_json(base_url)
    if err:
        return err
    return render_status(data, task_id)


def cancel_mode(base_url, task_id):
    task_id = (task_id or "").strip()
    if not task_id:
        return f"[ERROR] --cancel 後面需要 work package 的 task_id。\n{USAGE}"
    status, text, err = _http("DELETE", base_url, f"/api/work_packages/{quote(task_id, safe='')}")
    if err:
        return err
    time.sleep(0.6)  # orchestrtor 每 0.5 秒重算一次快照，等一拍再確認
    data, ws_err = ws_read_json(base_url)
    if ws_err:
        confirm = f"（無法讀取快照確認：{ws_err[8:]}）"
    else:
        ids = [p.get("package_id") for p in (data.get("orchestrtor") or {}).get("packages") or []]
        confirm = "快照確認：orchestrtor 清單中已不存在 " + task_id if task_id not in ids else \
                  f"快照中 {task_id} 仍在清單（請求剛送出，稍後用 --status {task_id} 再確認；若一直存在表示 orchestrtor 沒收到）"
    return (
        f"[PASS] 已送出取消 work package {task_id} 的請求（HTTP {status}，web_console 已轉發 cancel_work_package）\n"
        f"web_console 回應: {text[:300]}\n{confirm}\n"
        "注意：orchestrtor 只會停止派下一站並移除清單；已送到 distribute 的當前站可能仍會被機器人執行完。"
    )


def cancel_overpending_mode(base_url, task_id):
    task_id = (task_id or "").strip()
    if not task_id:
        return f"[ERROR] --cancel-overpending 後面需要 distribute 佇列中的任務 id。\n{USAGE}"
    status, text, err = _http("DELETE", base_url, f"/api/overpending_tasks/{quote(task_id, safe='')}")
    if err:
        return err
    time.sleep(0.6)
    data, ws_err = ws_read_json(base_url)
    if ws_err:
        confirm = f"（無法讀取快照確認：{ws_err[8:]}）"
    else:
        ids = [t.get("id") for t in data.get("overpending_tasks") or []]
        confirm = f"快照確認：OverPending 佇列中已不存在 {task_id}" if task_id not in ids else \
                  f"快照中 {task_id} 仍在 OverPending 佇列（稍後用 --status 再確認）"
    return (
        f"[PASS] 已送出刪除 OverPending 任務 {task_id} 的請求（HTTP {status}，web_console 已轉發 cancel_overpending_task）\n"
        f"web_console 回應: {text[:300]}\n{confirm}\n"
        "注意：只影響已進入 OverPending 佇列的任務；還在 buffer/raw/processing 階段的不受影響。"
    )


def list_mode(base_url, what):
    path, key, label = ("/api/stations", "stations", "站點") if what == "stations" else ("/api/robots", "robots", "機器人")
    status, text, err = _http("GET", base_url, path)
    if err:
        return err
    try:
        items = json.loads(text).get(key) or []
    except (ValueError, AttributeError):
        return f"[ERROR] {path} 回應不是預期的 JSON: {text[:200]}"
    names = [it.get("id") if isinstance(it, dict) else str(it) for it in items]
    if not names:
        return f"[PASS] 目前場域沒有任何{label}（{path} 回傳空清單；機隊名冊 robots.yaml 或拓譜圖可能未載入）。"
    return f"[PASS] 目前場域的{label}（{len(names)}）：{', '.join(names)}"


def main(argv):
    opts, err = parse_args(argv)
    if err:
        return err
    base_url = opts["url"]
    if opts["status_mode"]:
        return status_mode(base_url, opts["status"])
    if opts["stations_mode"]:
        return list_mode(base_url, "stations")
    if opts["robots_mode"]:
        return list_mode(base_url, "robots")
    if opts["cancel"] is not None:
        if opts["dry_run"]:
            return f"[PASS] dry-run：會 DELETE {base_url}/api/work_packages/{quote(opts['cancel'], safe='')}，尚未送出。"
        return cancel_mode(base_url, opts["cancel"])
    if opts["cancel_overpending"] is not None:
        if opts["dry_run"]:
            return f"[PASS] dry-run：會 DELETE {base_url}/api/overpending_tasks/{quote(opts['cancel_overpending'], safe='')}，尚未送出。"
        return cancel_overpending_mode(base_url, opts["cancel_overpending"])
    pkg, err = build_payload(opts)
    if err:
        return err
    if opts["dry_run"]:
        target = f"ros2（容器 {opts['ros2']}）：{ros2_pub_command(pkg)}" if opts["ros2"] else f"POST {base_url}{SEND_PATH}"
        return (f"[PASS] dry-run：以下內容尚未送出\n{describe(pkg)}\npayload: {json.dumps(pkg, ensure_ascii=False)}\n"
                f"目標: {target}\n拿掉 --dry-run 即可真正送出。")
    if opts["ros2"]:
        return send_ros2(opts["ros2"], pkg)
    return send_http(base_url, pkg)


if __name__ == "__main__":
    try:
        print(main(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] workpackage_est 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
