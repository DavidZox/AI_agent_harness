"""workpackage_est：對任務協調器（orchestrtor）發送、取消、查詢 work package，並讀語義地圖對應目前站點狀態。

對齊 fih_rmf_system（amr_base/src/fih_rmf_system）web_console 對外的能力，全部走 http://localhost:8020
（容器 network_mode: host，宿主機直連）；技能不讀任何地圖檔，站點／語意／機器人位置都是問 web_console：
  發送 work package      POST   /api/send_tasks              → topic incoming_work_packages（orchestrtor 逐站派給 distribute）
  取消 work package      DELETE /api/work_packages/{id}      → topic cancel_work_package
  刪除 OverPending 任務  DELETE /api/overpending_tasks/{id}  → topic cancel_overpending_task
  狀態快照               WebSocket /api/ws（每 0.5 秒一幀：distribute 佇列／機器人；"orchestrtor" 鍵是 work package 狀態）
  語義地圖               GET /api/topology（拓譜圖節點與邊 + semantics.yaml 的語意名稱／說明）、/api/stations、/api/robots
備用入口 --ros2 <容器>：不經 web_console，在 ROS2 容器內 `ros2 topic pub --once` 直接發布 work package JSON。

時序上的兩個重點（實作依據 distribute/ResourceAllocator_node_HTML_demo.py）：
  * OverPending 不是終點：raw 佇列等超過 threshold_ovp_sec（預設 10 秒）進 OverPending，待滿 threshold_recovery_sec
    （預設 10 秒）又回流 raw，直到有機器人領走。DELETE /api/overpending_tasks 只對「此刻在 OverPending」的任務有效，
    所以 --cancel-overpending 會先看快照，不在逾時區時可用 --wait 秒 等它進來再刪。
  * 一幀快照只是瞬間，--status --watch 秒 會持續收幀並整理這段時間的變化（站點推進、OverPending 進出、機器人狀態、事件）。

回傳以 [PASS]/[ERROR] 開頭（專案慣例）。純標準庫（urllib + 迷你 WebSocket 客戶端），不依賴 requests／websockets。
"""
import base64
import json
import math
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
HTTP_TIMEOUT_SECONDS = 15
WS_TIMEOUT_SECONDS = 6
ROS2_WAIT_SUBSCRIBER_SECONDS = 10
ROS2_TIMEOUT_SECONDS = 30
MAX_WATCH_SECONDS = 300          # 低於 harness 的 600 秒總逾時
NEAR_STATION_DISTANCE = 1.0      # 機器人與站點座標距離小於此值視為「在該站」（與拓譜圖同單位，isaacsim 場景為公尺）

TASK_TYPES = ("regular", "charge", "park")
LEVELS = ("normal", "middle", "emergency")

USAGE = (
    "用法:\n"
    "  發送: scripts/workpackage_est_cmd.py <task_id|auto> <站點1,站點2,...> [--amr 機器人] [--type regular|charge|park]\n"
    "        [--level normal|middle|emergency] [--weight 整數] [--loop [輪數]] [--desc 描述1,描述2,...] [--ros2 容器名稱] [--dry-run]\n"
    "        站點可寫代號（a3）或語意名稱（加工線通道-6），會自動對應成代號\n"
    "  取消: --cancel <task_id>；--cancel-overpending <任務id> [--wait 秒]（不在逾時區時等它進來再刪）\n"
    "  查詢: --status [task_id] [--watch 秒]（持續觀察並整理變化）｜--map [關鍵字或站點代號]（語義地圖＋目前佈局）｜--stations｜--robots\n"
    "  共用: [--url http://localhost:8020]\n"
    "  範例: scripts/workpackage_est_cmd.py WP001 home,a3,a7 --amr tb1\n"
    "        scripts/workpackage_est_cmd.py --map 加工線通道　　scripts/workpackage_est_cmd.py --status --watch 20"
)

_VALUE_OPTS = {"--amr": "amr", "--type": "type", "--level": "level", "--weight": "weight",
               "--desc": "desc", "--url": "url", "--ros2": "ros2",
               "--cancel": "cancel", "--cancel-overpending": "cancel_overpending",
               "--wait": "wait", "--watch": "watch"}
_FLAG_OPTS = {"--dry-run": "dry_run", "--stations": "stations_mode", "--robots": "robots_mode"}
_OPTIONAL_VALUE_OPTS = {"--status": ("status_mode", "status"), "--map": ("map_mode", "map_query")}


# ---------------------------------------------------------------- 參數
def parse_args(argv):
    opts = {"task_id": None, "stations": None, "amr": "", "type": "regular", "level": "normal", "weight": "0",
            "loop": False, "loop_count": None, "desc": "", "url": DEFAULT_URL, "ros2": None, "dry_run": False,
            "cancel": None, "cancel_overpending": None, "wait": None, "watch": None,
            "status_mode": False, "status": None, "map_mode": False, "map_query": None,
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
        elif tok in _OPTIONAL_VALUE_OPTS:
            flag, value_key = _OPTIONAL_VALUE_OPTS[tok]
            opts[flag] = True
            if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
                opts[value_key] = argv[i + 1]
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

    for key, name in (("wait", "--wait"), ("watch", "--watch")):
        if opts[key] is not None:
            try:
                val = int(opts[key])
            except ValueError:
                return None, f"[ERROR] {name} 必須是秒數（整數）。"
            if not 0 <= val <= MAX_WATCH_SECONDS:
                return None, f"[ERROR] {name} 需介於 0～{MAX_WATCH_SECONDS} 秒（低於系統總逾時 600 秒）。"
            opts[key] = val
    modes = [m for m, on in (("--cancel", opts["cancel"] is not None), ("--cancel-overpending", opts["cancel_overpending"] is not None),
                             ("--status", opts["status_mode"]), ("--map", opts["map_mode"]),
                             ("--stations", opts["stations_mode"]), ("--robots", opts["robots_mode"])) if on]
    if len(modes) > 1:
        return None, f"[ERROR] 一次只能做一件事，收到 {', '.join(modes)}。\n{USAGE}"
    if opts["watch"] is not None and "--status" not in modes:
        return None, "[ERROR] --watch 只能搭配 --status。"
    if opts["wait"] is not None and "--cancel-overpending" not in modes:
        return None, "[ERROR] --wait 只能搭配 --cancel-overpending。"
    if modes:
        if positional:
            return None, f"[ERROR] {modes[0]} 模式不接受位置參數（收到 {positional}）。\n{USAGE}"
        return opts, None
    if len(positional) != 2:
        hint = "（看起來是舊版 workitem_est 的 5 個位置參數；新版只有 task_id 與站點兩個位置參數，其餘改用選項）" if len(positional) == 5 else ""
        return None, f"[ERROR] 發送需要 2 個位置參數 <task_id> <站點清單>，收到 {len(positional)} 個{hint}。\n{USAGE}"
    opts["task_id"], opts["stations"] = positional
    return opts, None


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
            return e.code, detail, f"[ERROR] web_console 回應 HTTP 404：{url} 不存在（web_console 版本不含這個端點，或拓譜圖檔案不存在）: {detail}"
        return e.code, detail, f"[ERROR] web_console 回應 HTTP {e.code}: {detail}"
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, socket.timeout) or "timed out" in str(reason).lower():
            return None, "", f"[ERROR] 連線／等待 web_console 回應逾時（{timeout} 秒）：{url}。服務可能卡住，請確認 web_console 狀態後再試。"
        return None, "", (f"[ERROR] 無法連線到 web_console {url}（{reason}）。web_console 由 rmf_launch.py 帶起、port 8020、"
                          f"容器為 network_mode: host；請確認它有在跑，或（僅發送）改用 --ros2 <容器名稱> 直接發布 topic。")
    except (socket.timeout, TimeoutError):
        return None, "", f"[ERROR] 連線／等待 web_console 回應逾時（{timeout} 秒）：{url}。"


# ---------------------------------------------------------------- 迷你 WebSocket 客戶端
class _WsError(Exception):
    pass


def _ws_connect(base_url, path="/api/ws", timeout=WS_TIMEOUT_SECONDS):
    u = urlparse(base_url)
    if u.scheme == "https":
        raise _WsError("[ERROR] 狀態快照目前只支援 http:// 的 web_console。")
    host, port = u.hostname or "localhost", u.port or 80
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
    except (ConnectionRefusedError, OSError) as e:
        raise _WsError(f"[ERROR] 無法連線到 web_console {base_url}（{e}）。請確認它有在跑（rmf_launch.py 帶起、port 8020）。")
    s.settimeout(timeout)
    s.sendall(handshake)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            s.close()
            raise _WsError("[ERROR] web_console 在 WebSocket 握手時關閉了連線。")
        buf += chunk
    head, rest = buf.split(b"\r\n\r\n", 1)
    status_line = head.split(b"\r\n", 1)[0].decode(errors="replace")
    if " 101 " not in status_line:
        s.close()
        raise _WsError(f"[ERROR] web_console 拒絕 WebSocket 升級（{status_line}）；{path} 可能不存在或不是 web_console。")
    return s, rest


def _ws_messages(s, initial):
    """從已握手的 socket 逐則產生文字訊息（bytes）；close 幀時結束。"""
    data = initial
    message = b""

    def need(n):
        nonlocal data
        while len(data) < n:
            chunk = s.recv(65536)
            if not chunk:
                raise _WsError("連線在訊息結束前被關閉")
            data += chunk

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
            return
        if opcode in (9, 10):
            continue
        if opcode in (1, 0):
            message += payload
            if fin:
                yield message
                message = b""


def ws_frames(base_url, duration=None):
    """產生 (相對秒數, 快照 dict)。duration=None 只取第一幀；否則持續到 duration 秒。
    失敗時第一次就 raise _WsError；中途連線中斷則結束產生。"""
    s, rest = _ws_connect(base_url)
    start = time.monotonic()
    got_any = False
    try:
        for raw in _ws_messages(s, rest):
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            got_any = True
            yield time.monotonic() - start, data
            if duration is None or time.monotonic() - start >= duration:
                break
    except socket.timeout:
        if not got_any:
            raise _WsError(f"[ERROR] WebSocket {WS_TIMEOUT_SECONDS} 秒內沒收到快照：web_console 有在跑但沒有推送（/api/ws 異常）。")
    except _WsError:
        if not got_any:
            raise
    finally:
        try:
            s.sendall(b"\x88\x80" + os.urandom(4))
        except OSError:
            pass
        s.close()


def ws_read_json(base_url):
    """讀一幀快照，回傳 (data, error)。"""
    try:
        for _, data in ws_frames(base_url):
            return data, None
        return None, "[ERROR] web_console 送出 WebSocket close，沒有收到任何狀態快照。"
    except _WsError as e:
        return None, str(e)


def ws_collect(base_url, duration):
    """收 duration 秒的快照，回傳 (frames[(t, data)], error)。"""
    try:
        return list(ws_frames(base_url, duration)), None
    except _WsError as e:
        return [], str(e)


# ---------------------------------------------------------------- 語義地圖（/api/topology）
def fetch_topology(base_url):
    """回傳 (topo, error)。topo = {"nodes", "edges", "by_name", "by_id", "labels"}；labels = 語意名稱 -> [站點代號]。"""
    status, text, err = _http("GET", base_url, "/api/topology")
    if err:
        return None, err
    try:
        data = json.loads(text)
    except ValueError:
        return None, f"[ERROR] /api/topology 回應不是 JSON: {text[:200]}"
    nodes = [n for n in (data.get("nodes") or []) if n.get("name")]
    edges = data.get("edges") or []
    labels = {}
    for n in nodes:
        label = (n.get("label") or "").strip()
        if label and label != n["name"]:
            labels.setdefault(label, []).append(n["name"])
    return {"nodes": nodes, "edges": edges,
            "by_name": {n["name"]: n for n in nodes},
            "by_id": {str(n.get("id")): n for n in nodes},
            "labels": labels}, None


def node_by_ref(ref, topo):
    """站點參照可能是代號（a10）或節點 id（"10"）：RobotState 的 target_id／current_edge 用的是 id。"""
    if not topo or ref is None:
        return None
    ref = str(ref).strip()
    return topo["by_name"].get(ref) or topo["by_id"].get(ref)


def station_text(ref, topo):
    """站點參照附語意名稱：a0（加工線通道-6）；id 會換成代號。"""
    if ref is None or ref == "":
        return "?"
    node = node_by_ref(ref, topo)
    if not node:
        return str(ref)
    label = node.get("label") or ""
    return f"{node['name']}（{label}）" if label and label != node["name"] else str(node["name"])


def edge_text(edge_ref, topo):
    """current_edge 形如 "17->10" 或 "a3->a7"：兩端換成站點文字。"""
    if not edge_ref or "->" not in str(edge_ref):
        return str(edge_ref or "")
    a, b = str(edge_ref).split("->", 1)
    return f"{station_text(a, topo)}→{station_text(b, topo) if b else '?'}"


def resolve_stations(tokens, topo):
    """站點輸入可為代號或語意名稱；回傳 (代號清單, 對應說明清單, error)。topo 為 None 時原樣放行。"""
    if not topo:
        return list(tokens), [], None
    resolved, notes = [], []
    for tok in tokens:
        if tok in topo["by_name"]:
            resolved.append(tok)
            continue
        exact = topo["labels"].get(tok) or [n for lbl, ns in topo["labels"].items() for n in ns if lbl.lower() == tok.lower()]
        if len(exact) == 1:
            resolved.append(exact[0])
            notes.append(f"「{tok}」→ {exact[0]}")
            continue
        if len(exact) > 1:
            return None, None, f"[ERROR] 語意名稱「{tok}」對應到多個站點（{', '.join(exact)}），請改用站點代號。"
        partial = [n for lbl, ns in topo["labels"].items() if tok.lower() in lbl.lower() for n in ns]
        if len(partial) == 1:
            resolved.append(partial[0])
            notes.append(f"「{tok}」→ {partial[0]}（{topo['by_name'][partial[0]].get('label')}）")
            continue
        if len(partial) > 1:
            return None, None, (f"[ERROR] 「{tok}」符合多個站點的語意名稱：" +
                                "；".join(f"{n}（{topo['by_name'][n].get('label')}）" for n in partial[:8]) + "，請指定其中一個。")
        return None, None, (f"[ERROR] 目前場域沒有站點「{tok}」（代號或語意名稱都對不上），可用 --stations 或 --map 查；"
                            f"現有代號：{', '.join(list(topo['by_name'])[:12])}{'…' if len(topo['by_name']) > 12 else ''}")
    return resolved, notes, None


def _edge_endpoints(edge, topo):
    a = topo["by_id"].get(str(edge.get("start_id")), {}).get("name") or str(edge.get("start_id"))
    b = topo["by_id"].get(str(edge.get("end_id")), {}).get("name") or str(edge.get("end_id"))
    return a, b


def occupancy(snapshot, topo):
    """把即時快照對到站點：回傳 (station -> [說明], robot_lines)。"""
    per_station = {}
    robot_lines = []
    nodes = topo["nodes"]
    for rid, info in (snapshot.get("robots") or {}).items():
        parts = []
        x, y = info.get("x"), info.get("y")
        nearest = None
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and nodes:
            nearest = min(nodes, key=lambda n: math.hypot((n.get("x") or 0) - x, (n.get("y") or 0) - y))
            dist = math.hypot((nearest.get("x") or 0) - x, (nearest.get("y") or 0) - y)
            parts.append(f"座標 ({x:.2f}, {y:.2f}) 最近站 {nearest['name']}（距 {dist:.2f}）")
            if dist <= NEAR_STATION_DISTANCE:
                per_station.setdefault(nearest["name"], []).append(f"{rid} 在此（距 {dist:.2f}）")
        if info.get("current_edge"):
            parts.append(f"路段 {edge_text(info['current_edge'], topo)}")
        if info.get("target_id"):
            parts.append(f"目標 {station_text(info['target_id'], topo)}")
            target = node_by_ref(info["target_id"], topo)
            if target:
                per_station.setdefault(target["name"], []).append(f"{rid} 前往中")
        robot_lines.append(f"{rid} {info.get('status', '?')} {info.get('state', '?')}：" + ("｜".join(parts) if parts else "無座標／路段資訊"))
    for p in (snapshot.get("orchestrtor") or {}).get("packages") or []:
        if p.get("status") != "RUNNING":
            continue
        cur = p.get("current_item") or {}
        st = cur.get("station")
        if st:
            who = f"機器人 {cur['robot_id']}" if cur.get("robot_id") else "等待機器人"
            per_station.setdefault(st, []).append(f"WP {p.get('package_id')} 第 {p.get('station_index', 0) + 1}/{p.get('station_count')} 站派工中（{who}）")
    return per_station, robot_lines


def _clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n] + "…"


def map_mode(base_url, query=None):
    topo, err = fetch_topology(base_url)
    if err:
        return err
    snapshot, ws_err = ws_read_json(base_url)
    per_station, robot_lines = occupancy(snapshot, topo) if snapshot else ({}, [])
    live_note = "" if snapshot else f"（即時快照讀取失敗：{ws_err[8:] if ws_err else '?'}，以下沒有機器人／工作包位置）"
    nodes, edges = topo["nodes"], topo["edges"]
    sem_nodes = sum(1 for n in nodes if n.get("has_semantic"))
    sem_edges = sum(1 for e in edges if e.get("has_semantic"))
    head = f"[PASS][digest] 語義地圖：{len(nodes)} 站、{len(edges)} 邊，有語意名稱／說明的站 {sem_nodes}、邊 {sem_edges}（web_console /api/topology + 即時快照）{live_note}"

    if not query:
        # 精簡版：站點=語意名稱 一行、有東西的站點另列，避免 30 幾站逐行超過工具回傳門檻
        pairs = [f"{n['name']}={n.get('label')}" if n.get("label") and n.get("label") != n["name"] else n["name"] for n in nodes]
        lines = [head, "站點（代號=語意名稱）：" + "、".join(pairs)]
        busy = [f"{station_text(name, topo)}：{'；'.join(items)}" for name, items in per_station.items()]
        lines.append("目前有機器人或工作包的站點：" + ("；".join(busy) if busy else "沒有"))
        if robot_lines:
            lines.append("機器人：" + "；".join(robot_lines))
        lines.append("用 --map <關鍵字或站點代號> 看該站／路段的完整說明（VLM 判讀紀錄等）。")
        return "\n".join(lines)

    q = query.strip().lower()
    hit_nodes = [n for n in nodes if q == str(n["name"]).lower() or q in str(n.get("label") or "").lower() or q in str(n.get("description") or "").lower()]
    hit_edges = [e for e in edges if q in str(e.get("label") or "").lower() or q in str(e.get("description") or "").lower()
                 or q in {x.lower() for x in _edge_endpoints(e, topo)}]
    if not hit_nodes and not hit_edges:
        return f"{head}\n找不到與「{query}」相關的站點或路段（比對代號、語意名稱、說明）。現有站點：{', '.join(n['name'] for n in nodes[:20])}{'…' if len(nodes) > 20 else ''}"
    lines = [head, f"與「{query}」相關：{len(hit_nodes)} 站、{len(hit_edges)} 路段"]
    for n in hit_nodes[:6]:
        lines.append(f"■ {n['name']}｜語意名稱：{n.get('label') or '—'}｜id {n.get('id')}｜座標 ({n.get('x')}, {n.get('y')})")
        lines.append(f"  說明：{_clip(n.get('description'), 500) or '（尚未設定）'}")
        lines.append(f"  目前：{'；'.join(per_station.get(n['name'], [])) or '沒有機器人在此或前往、沒有工作包派工中'}")
        linked = [e for e in edges if str(e.get("start_id")) == str(n.get("id")) or str(e.get("end_id")) == str(n.get("id"))]
        if linked:
            lines.append("  連接路段：" + "；".join(f"#{e.get('id')} {'→'.join(_edge_endpoints(e, topo))}" + (f"「{e.get('label')}」" if e.get("label") else "") for e in linked[:8]))
    for e in [e for e in hit_edges if e not in []][:6]:
        a, b = _edge_endpoints(e, topo)
        lines.append(f"■ 路段 #{e.get('id')} {a}→{b}｜語意名稱：{e.get('label') or '—'}")
        lines.append(f"  說明：{_clip(e.get('description'), 400) or '（尚未設定）'}")
    if robot_lines:
        lines.append("機器人：" + "；".join(robot_lines))
    return "\n".join(lines)


# ---------------------------------------------------------------- 發送
def build_payload(opts, topo=None):
    task_id = (opts["task_id"] or "").strip()
    if task_id.lower() == "auto":
        task_id = "WP-" + time.strftime("%Y%m%d-%H%M%S")
    if not task_id:
        return None, None, f"[ERROR] task_id 不可為空（可填 auto 自動產生）。\n{USAGE}"
    tokens = [s.strip() for s in (opts["stations"] or "").split(",") if s.strip()]
    if not tokens:
        return None, None, f"[ERROR] 至少需要一個站點（逗號分隔，例如 home,a3,a7；站點可用 --stations 查）。\n{USAGE}"
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
    if opts["loop_count"] is not None and opts["loop_count"] <= 0:
        return None, None, "[ERROR] --loop 後面的輪數必須是正整數（不接數字＝循環到手動刪除）。"
    amr = (opts["amr"] or "").strip()
    for name, value in (("task_id", task_id), ("--amr", amr), *[("站點", s) for s in stations]):
        if "," in value:
            return None, None, f"[ERROR] {name} 不可含逗號（orchestrtor 派工給 distribute 時以逗號分隔欄位）: {value!r}"
    return {
        "task_id": task_id, "task_type": task_type, "level": level, "weight": weight, "assign_amr": amr,
        "loop": bool(opts["loop"]), "loop_count": opts["loop_count"] if opts["loop"] else None,
        "stops": [{"station": s, "description": d} for s, d in zip(stations, descs)],
    }, notes, None


def describe(pkg, topo=None):
    stations = [station_text(s["station"], topo) for s in pkg["stops"]]
    loop = ("是（%s）" % (f"{pkg['loop_count']} 輪" if pkg["loop_count"] else "直到手動刪除")) if pkg["loop"] else "否"
    return (f"task_id: {pkg['task_id']}\nstops: {' → '.join(stations)}（{len(stations)} 站）\n"
            f"assign_amr: {pkg['assign_amr'] or '（空，交給 distribute 挑機器人）'}｜task_type: {pkg['task_type']}｜"
            f"level: {pkg['level']}｜weight: {pkg['weight'] if pkg['weight'] else '0（依類型與等級自動計算）'}｜loop: {loop}")


FOLLOW_UP = ("後續：orchestrtor 會依序把每一站派給 distribute，完成判斷靠機器人回報 /{rid}/task_exec_fin；沒有機器人可承接時停在當前站等待。"
             "用 --status <task_id> 看進度、--status --watch 秒 觀察一段時間、--cancel <task_id> 撤銷。")


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
    return (f"[PASS] work package 已送出（HTTP {status}，web_console 已轉發到 {TOPIC}）\n{mapped}{describe(pkg, topo)}\n"
            f"payload: {json.dumps(pkg, ensure_ascii=False)}\nweb_console 回應: {text[:600]}{note}\n{FOLLOW_UP}")


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
    return (f"[PASS] work package 已直接發布到 {TOPIC}（容器 {container}，未經 web_console）\n{mapped}{describe(pkg)}\n"
            f"payload: {json.dumps(pkg, ensure_ascii=False)}\nros2 輸出: {(out or '').strip()[:300]}\n{FOLLOW_UP}")


# ---------------------------------------------------------------- 狀態
def _package_line(p, topo=None):
    cur = p.get("current_item") or {}
    idx, total = p.get("station_index", 0), p.get("station_count") or len(p.get("stations") or [])
    stations = p.get("stations") or []
    station = cur.get("station") or (stations[idx] if idx < len(stations) else "?")
    if p.get("status") == "COMPLETED":
        progress = f"已完成（共 {p.get('iteration', 0)} 輪）"
    else:
        progress = f"第 {idx + 1}/{total} 站 {station_text(station, topo)}"
        if cur:
            robot = f"機器人 {cur.get('robot_id')}" if cur.get("robot_id") else ("⏳ 等待機器人" if cur.get("waiting_for_robot") else "機器人未知")
            progress += f"，工作項 {cur.get('status')}，{robot}"
    loop = f"｜循環 {p.get('loop_count') or '∞'} 輪，目前第 {p.get('iteration', 0) + 1} 輪" if p.get("loop") else ""
    return (f"- {p.get('package_id')} {p.get('status')}：{progress}{loop}｜完成 {p.get('items_done', 0)}/{p.get('items_total', 0)} 項"
            f"｜路線 {' → '.join(station_text(s, topo) for s in stations)}")


def render_status(data, task_id=None, topo=None, head=None):
    orch = data.get("orchestrtor") or {}
    packages = orch.get("packages") or []
    lines = [head or f"[PASS] 目前狀態（web_console /api/ws 快照 distribute #{data.get('step_counter', '?')} / orchestrtor #{orch.get('step_counter', '?')}）"]
    if task_id:
        hits = [p for p in packages if p.get("package_id") == task_id]
        if not hits:
            lines.append(f"orchestrtor 清單中沒有 work package {task_id}：可能未送達、已被刪除，或 orchestrtor 沒在跑"
                         f"（目前清單 {len(packages)} 個{'：' + '、'.join(str(p.get('package_id', '?')) for p in packages[:10]) if packages else ''}）。")
        else:
            lines += [_package_line(p, topo) for p in hits]
        related = [t for key in ("processing_tasks", "raw_tasks", "overpending_tasks", "buffer_tasks")
                   for t in (data.get(key) or []) if str(t.get("id", "")).startswith(f"{task_id}::")]
        if related:
            lines.append("distribute 佇列中相關的工作項：" + "；".join(
                f"{t.get('id')}" + (f" → {t.get('robot')}" if t.get("robot") else "") + (f"（OverPending 已等 {t.get('wait')}s）" if "wait" in t else "")
                for t in related))
    else:
        if not orch:
            lines.append("orchestrtor 快照尚未收到（orchestrtor 可能沒在跑，或 web_console 剛啟動）。")
        else:
            running = sum(1 for p in packages if p.get("status") == "RUNNING")
            lines.append(f"orchestrtor work packages：{len(packages)} 個（RUNNING {running}，COMPLETED {len(packages) - running}）")
            lines += [_package_line(p, topo) for p in packages[:15]]
        ovp, proc, raw = data.get("overpending_tasks") or [], data.get("processing_tasks") or [], data.get("raw_tasks") or []
        lines.append(f"distribute：狀態 {data.get('state', '?')}｜buffer {len(data.get('buffer_tasks') or [])}｜raw {len(raw)}"
                     + (f"（{'、'.join(f'{t.get('id')} 等 {t.get('age')}s' for t in raw[:5])}）" if raw else "")
                     + f"｜processing {len(proc)}" + (f"（{'、'.join(f'{t.get('id')}→{t.get('robot') or '-'}' for t in proc[:5])}）" if proc else "")
                     + f"｜overpending {len(ovp)}" + (f"（{'、'.join(f'{t.get('id')} 已等 {t.get('wait')}s' for t in ovp[:5])}；此刻在逾時區，可 --cancel-overpending 刪除，超過 recovery 秒數會回流 raw）" if ovp else ""))
        robots = data.get("robots") or {}
        if robots:
            lines.append("機器人：" + "；".join(
                f"{rid} {info.get('status', '?')} {info.get('state', '?')}" + (f"（{info.get('last_task_id')}）" if info.get('last_task_id') not in (None, 'None') else "")
                + (f" 目標 {station_text(info['target_id'], topo)}" if info.get("target_id") else "")
                for rid, info in list(robots.items())[:8]))
    for label, events in (("orchestrtor", orch.get("events") or []), ("distribute", data.get("events") or [])):
        if events:
            lines.append(f"最近事件（{label}）：" + " ｜ ".join(events[-3:]))
    return "\n".join(lines)


def _ids(data, key):
    return [t.get("id") for t in (data.get(key) or []) if t.get("id")]


def watch_mode(base_url, seconds, task_id=None, topo=None):
    frames, err = ws_collect(base_url, seconds)
    if err:
        return err
    if not frames:
        return "[ERROR] 觀察期間沒有收到任何快照。"
    changes = []
    prev = None
    events_seen, event_lines = set(), []
    for t, data in frames:
        for src, evs in (("orchestrtor", (data.get("orchestrtor") or {}).get("events") or []), ("distribute", data.get("events") or [])):
            for e in evs:
                if e not in events_seen:
                    events_seen.add(e)
                    event_lines.append(f"{src}: {e}")
        if prev is None:
            prev = data
            continue
        pk_prev = {p.get("package_id"): p for p in (prev.get("orchestrtor") or {}).get("packages") or []}
        pk_now = {p.get("package_id"): p for p in (data.get("orchestrtor") or {}).get("packages") or []}
        for pid, p in pk_now.items():
            if task_id and pid != task_id:
                continue
            q = pk_prev.get(pid)
            if q is None:
                changes.append(f"+{t:.1f}s 新 work package {pid}")
                continue
            if (p.get("station_index"), p.get("iteration")) != (q.get("station_index"), q.get("iteration")):
                changes.append(f"+{t:.1f}s {pid} 第 {q.get('station_index', 0) + 1} 站 → 第 {p.get('station_index', 0) + 1} 站"
                               + (f"（第 {p.get('iteration', 0) + 1} 輪）" if p.get("iteration") != q.get("iteration") else "")
                               + f"，目前 {station_text((p.get('current_item') or {}).get('station'), topo)}")
            elif (p.get("current_item") or {}).get("robot_id") != (q.get("current_item") or {}).get("robot_id"):
                changes.append(f"+{t:.1f}s {pid} 工作項改由 {(p.get('current_item') or {}).get('robot_id') or '（等待機器人）'} 承接")
            if p.get("status") != q.get("status"):
                changes.append(f"+{t:.1f}s {pid} {q.get('status')} → {p.get('status')}")
        for pid in set(pk_prev) - set(pk_now):
            if not task_id or pid == task_id:
                changes.append(f"+{t:.1f}s work package {pid} 從清單消失（被刪除）")
        for key, label in (("overpending_tasks", "OverPending"), ("raw_tasks", "raw"), ("processing_tasks", "processing")):
            a, b = set(_ids(prev, key)), set(_ids(data, key))
            for tid in sorted(b - a):
                changes.append(f"+{t:.1f}s {tid} 進入 {label}")
            for tid in sorted(a - b):
                changes.append(f"+{t:.1f}s {tid} 離開 {label}")
        rp, rn = prev.get("robots") or {}, data.get("robots") or {}
        for rid, info in rn.items():
            before = rp.get(rid) or {}
            for f in ("status", "state"):
                if before.get(f) != info.get(f) and before:
                    changes.append(f"+{t:.1f}s {rid} {f} {before.get(f)} → {info.get(f)}")
        prev = data
    span = frames[-1][0]
    last = frames[-1][1]
    head = f"[PASS][digest] 觀察 {seconds} 秒（收到 {len(frames)} 幀，實際 {span:.1f} 秒）"
    lines = [head, "變化：" + ("" if changes else "沒有任何狀態變化（工作包站點、佇列成員、機器人狀態都相同）")]
    lines += changes[:25]
    if len(changes) > 25:
        lines.append(f"…另有 {len(changes) - 25} 筆變化省略")
    new_events = [e for e in event_lines if not any(e.split(": ", 1)[-1] in (evs or []) for evs in
                  ((frames[0][1].get("orchestrtor") or {}).get("events") or [], frames[0][1].get("events") or []))]
    if new_events:
        lines.append(f"期間新增事件（{len(new_events)} 則）：" + " ｜ ".join(new_events[-6:]))
    # 結尾一行摘要（完整內容用 --status 再看），避免超過工具回傳門檻被精簡掉
    pk = [p for p in (last.get("orchestrtor") or {}).get("packages") or [] if not task_id or p.get("package_id") == task_id]
    pk_text = "；".join(f"{p.get('package_id')} {p.get('status')} 第 {p.get('station_index', 0) + 1}/{p.get('station_count')} 站 {station_text((p.get('current_item') or {}).get('station'), topo)}"
                        + (f" 機器人 {(p.get('current_item') or {}).get('robot_id')}" if (p.get('current_item') or {}).get('robot_id') else " ⏳") for p in pk[:6]) or "沒有 work package"
    lines.append(f"結束時：{pk_text}｜distribute raw {len(last.get('raw_tasks') or [])}／processing {len(last.get('processing_tasks') or [])}／overpending {len(last.get('overpending_tasks') or [])}"
                 f"（{'、'.join(_ids(last, 'overpending_tasks')[:4])}）" if last.get("overpending_tasks") else
                 f"結束時：{pk_text}｜distribute raw {len(last.get('raw_tasks') or [])}／processing {len(last.get('processing_tasks') or [])}／overpending 0")
    return "\n".join(lines)


def status_mode(base_url, task_id=None, watch=None):
    topo, _ = fetch_topology(base_url)   # 拿不到語意就只顯示代號
    if watch:
        return watch_mode(base_url, watch, task_id, topo)
    data, err = ws_read_json(base_url)
    if err:
        return err
    return render_status(data, task_id, topo)


# ---------------------------------------------------------------- 取消
def cancel_mode(base_url, task_id):
    task_id = (task_id or "").strip()
    if not task_id:
        return f"[ERROR] --cancel 後面需要 work package 的 task_id。\n{USAGE}"
    status, text, err = _http("DELETE", base_url, f"/api/work_packages/{quote(task_id, safe='')}")
    if err:
        return err
    time.sleep(0.6)
    data, ws_err = ws_read_json(base_url)
    if ws_err:
        confirm = f"（無法讀取快照確認：{ws_err[8:]}）"
    else:
        ids = [p.get("package_id") for p in (data.get("orchestrtor") or {}).get("packages") or []]
        confirm = ("快照確認：orchestrtor 清單中已不存在 " + task_id) if task_id not in ids else \
                  f"快照中 {task_id} 仍在清單（請求剛送出，稍後用 --status {task_id} 再確認；若一直存在表示 orchestrtor 沒收到）"
    return (f"[PASS] 已送出取消 work package {task_id} 的請求（HTTP {status}，web_console 已轉發 cancel_work_package）\n"
            f"web_console 回應: {text[:300]}\n{confirm}\n注意：orchestrtor 只會停止派下一站並移除清單；已送到 distribute 的當前站可能仍會被機器人執行完。")


def _locate_task(data, task_id):
    for key, label in (("overpending_tasks", "OverPending"), ("raw_tasks", "raw"), ("processing_tasks", "processing"), ("buffer_tasks", "buffer")):
        for t in data.get(key) or []:
            if t.get("id") == task_id:
                extra = f"，已在逾時區 {t.get('wait')}s" if key == "overpending_tasks" else (f"，已等 {t.get('age')}s" if key == "raw_tasks" else (f"，機器人 {t.get('robot') or '-'}" if key == "processing_tasks" else ""))
                return label, extra
    return None, ""


def cancel_overpending_mode(base_url, task_id, wait):
    task_id = (task_id or "").strip()
    if not task_id:
        return f"[ERROR] --cancel-overpending 後面需要 distribute 佇列中的任務 id（--status 可查）。\n{USAGE}"
    wait = wait or 0
    where, extra, elapsed = None, "", 0.0
    try:
        for t, data in ws_frames(base_url, wait if wait > 0 else None):
            where, extra = _locate_task(data, task_id)
            elapsed = t
            if where == "OverPending":
                break
            if wait == 0:
                break
    except _WsError as e:
        return str(e)
    if where != "OverPending":
        if where is None:
            return (f"[PASS] 未刪除：{task_id} 目前不在 distribute 的任何佇列（觀察 {elapsed:.1f} 秒）。可能已被機器人領走並執行、"
                    f"id 打錯（工作項 id 形如 <package>::<輪>::<站>::<序>，用 --status 查），或 distribute 沒在跑。")
        return (f"[PASS] 未刪除：{task_id} 目前在 {where} 佇列{extra}，不在 OverPending 逾時區，此刻無法用 cancel_overpending 刪除"
                f"（觀察了 {elapsed:.1f} 秒）。任務在 raw 等超過 threshold_ovp_sec（預設 10 秒）才會進入 OverPending，"
                f"進入後 threshold_recovery_sec（預設 10 秒）內就要刪；請加 --wait 30 讓我等它進逾時區再刪，或改用 --cancel 取消整個 work package。")
    status, text, err = _http("DELETE", base_url, f"/api/overpending_tasks/{quote(task_id, safe='')}")
    if err:
        return err
    time.sleep(0.6)
    data, ws_err = ws_read_json(base_url)
    if ws_err:
        confirm = f"（無法讀取快照確認：{ws_err[8:]}）"
    else:
        ids = _ids(data, "overpending_tasks")
        confirm = f"快照確認：OverPending 佇列中已不存在 {task_id}" if task_id not in ids else f"快照中 {task_id} 仍在 OverPending 佇列（稍後用 --status 再確認）"
    waited = f"（等了 {elapsed:.1f} 秒它才進入逾時區）" if elapsed > 0.6 else ""
    return (f"[PASS] 已刪除 OverPending 任務 {task_id}{waited}（HTTP {status}，web_console 已轉發 cancel_overpending_task）\n"
            f"web_console 回應: {text[:300]}\n{confirm}\n注意：只影響已進入 OverPending 佇列的任務；它所屬的 work package 若還在 RUNNING，orchestrtor 不會自動補派，需要的話用 --cancel 取消整個 work package。")


# ---------------------------------------------------------------- 查詢清單
def list_mode(base_url, what):
    if what == "stations":
        topo, err = fetch_topology(base_url)
        if not err:
            names = [station_text(n["name"], topo) for n in topo["nodes"]]
            if not names:
                return "[PASS] 目前場域沒有任何站點（拓譜圖沒有具名節點）。"
            return f"[PASS] 目前場域的站點（{len(names)}，括號為語意名稱；--map 可看說明與目前佈局）：{', '.join(names)}"
        status, text, err2 = _http("GET", base_url, "/api/stations")
        if err2:
            return err
        items = (json.loads(text).get("stations") or []) if text else []
        names = [it.get("id") if isinstance(it, dict) else str(it) for it in items]
        return f"[PASS] 目前場域的站點（{len(names)}）：{', '.join(names)}" if names else "[PASS] 目前場域沒有任何站點。"
    status, text, err = _http("GET", base_url, "/api/robots")
    if err:
        return err
    try:
        names = [str(r) for r in (json.loads(text).get("robots") or [])]
    except (ValueError, AttributeError):
        return f"[ERROR] /api/robots 回應不是預期的 JSON: {text[:200]}"
    return f"[PASS] 目前場域的機器人（{len(names)}）：{', '.join(names)}" if names else "[PASS] 目前場域沒有任何機器人（robots.yaml 未載入或為空）。"


def main(argv):
    opts, err = parse_args(argv)
    if err:
        return err
    base_url = opts["url"]
    if opts["status_mode"]:
        return status_mode(base_url, opts["status"], opts["watch"])
    if opts["map_mode"]:
        return map_mode(base_url, opts["map_query"])
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
            return f"[PASS] dry-run：會先確認 {opts['cancel_overpending']} 在 OverPending 逾時區才 DELETE {base_url}/api/overpending_tasks/…，尚未送出。"
        return cancel_overpending_mode(base_url, opts["cancel_overpending"], opts["wait"])
    topo, _ = fetch_topology(base_url)   # 站點名稱對應與驗證；web_console 拿不到時放行（--ros2 路徑也一樣）
    pkg, notes, err = build_payload(opts, topo)
    if err:
        return err
    if opts["dry_run"]:
        target = f"ros2（容器 {opts['ros2']}）：{ros2_pub_command(pkg)}" if opts["ros2"] else f"POST {base_url}{SEND_PATH}"
        mapped = f"站點名稱對應：{'；'.join(notes)}\n" if notes else ""
        return f"[PASS] dry-run：以下內容尚未送出\n{mapped}{describe(pkg, topo)}\npayload: {json.dumps(pkg, ensure_ascii=False)}\n目標: {target}\n拿掉 --dry-run 即可真正送出。"
    if opts["ros2"]:
        return send_ros2(opts["ros2"], pkg, notes)
    return send_http(base_url, pkg, notes, topo)


if __name__ == "__main__":
    try:
        print(main(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] workpackage_est 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
