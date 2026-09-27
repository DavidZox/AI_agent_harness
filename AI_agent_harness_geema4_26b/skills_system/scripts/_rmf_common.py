"""fih_rmf_system web_console 共用層：HTTP、迷你 WebSocket 客戶端、語義地圖（/api/topology）與快照工具。

工作包相關技能（workpackage_send／workpackage_status／workpackage_cancel／overpending_cancel／semantic_map）都透過這裡
跟 web_console（http://localhost:8020，容器 network_mode: host，宿主機直連）溝通；技能不讀任何地圖檔，站點／語意／
機器人位置都是問 web_console（對齊 amr_base/src/fih_rmf_system 的 distribute_task.FastAPIBridgeNode）：
  發送 work package      POST   /api/send_tasks              → topic incoming_work_packages（orchestrtor 逐站派給 distribute）
  取消 work package      DELETE /api/work_packages/{id}      → topic cancel_work_package
  刪除 OverPending 任務  DELETE /api/overpending_tasks/{id}  → topic cancel_overpending_task
  狀態快照               WebSocket /api/ws（每 0.5 秒一幀：distribute 佇列／機器人；"orchestrtor" 鍵是 work package 狀態）
  語義地圖               GET /api/topology（拓譜圖節點與邊 + semantics.yaml 的語意名稱／說明）、/api/stations、/api/robots

時序上的兩個重點（依據 distribute/ResourceAllocator_node_HTML_demo.py）：
  * OverPending 不是終點：raw 佇列等超過 threshold_ovp_sec（預設 10 秒）進 OverPending，待滿 threshold_recovery_sec
    （預設 10 秒）又回流 raw。DELETE /api/overpending_tasks 只對「此刻在 OverPending」的任務有效（overpending_cancel 會先看快照）。
  * 一幀快照只是瞬間，要看一段時間的變化用 workpackage_status --watch 秒。

共用選項 --url 位址（預設環境變數 RMF_WEB_CONSOLE_URL 或 http://localhost:8020），各腳本以 split_url() 取出。
回傳慣例：(status, text, error) 或 (data, error)，error 以 [ERROR] 開頭；純標準庫（urllib + 迷你 WebSocket），不依賴 requests／websockets。
"""
import base64
import json
import math
import os
import socket
import struct
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

DEFAULT_URL = os.environ.get("RMF_WEB_CONSOLE_URL", "http://localhost:8020").rstrip("/")
HTTP_TIMEOUT_SECONDS = 15
WS_TIMEOUT_SECONDS = 6
MAX_WATCH_SECONDS = 300          # 低於 harness 的 600 秒總逾時
NEAR_STATION_DISTANCE = 1.0      # 機器人與站點座標距離小於此值視為「在該站」（與拓譜圖同單位，isaacsim 場景為公尺）


# ---------------------------------------------------------------- 共用選項
def split_url(argv):
    """取出共用選項 --url，回傳 (其餘參數, base_url, error)。"""
    args, url, i = [], DEFAULT_URL, 0
    while i < len(argv):
        if argv[i] == "--url":
            if i + 1 >= len(argv):
                return None, None, "[ERROR] --url 後面需要 web_console 位址（例如 http://localhost:8020）。"
            url = argv[i + 1].rstrip("/")
            i += 2
        else:
            args.append(argv[i])
            i += 1
    return args, url, None


def parse_seconds(value, name, max_seconds=MAX_WATCH_SECONDS):
    """秒數選項：0～max_seconds 的整數（低於 harness 的 600 秒總逾時）。回傳 (秒, error)。"""
    try:
        val = int(str(value).strip())
    except (TypeError, ValueError):
        return None, f"[ERROR] {name} 必須是秒數（整數），收到: {value!r}。"
    if not 0 <= val <= max_seconds:
        return None, f"[ERROR] {name} 需介於 0～{max_seconds} 秒（低於系統總逾時 600 秒）。"
    return val, None

# ---------------------------------------------------------------- HTTP
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

# ---------------------------------------------------------------- 語義地圖（/api/topology）與站點對應
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
        return None, None, (f"[ERROR] 目前場域沒有站點「{tok}」（代號或語意名稱都對不上），可用 semantic_map 查；"
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

# ---------------------------------------------------------------- 快照裡找任務
def _ids(data, key):
    return [t.get("id") for t in (data.get(key) or []) if t.get("id")]

def _locate_task(data, task_id):
    for key, label in (("overpending_tasks", "OverPending"), ("raw_tasks", "raw"), ("processing_tasks", "processing"), ("buffer_tasks", "buffer")):
        for t in data.get(key) or []:
            if t.get("id") == task_id:
                extra = f"，已在逾時區 {t.get('wait')}s" if key == "overpending_tasks" else (f"，已等 {t.get('age')}s" if key == "raw_tasks" else (f"，機器人 {t.get('robot') or '-'}" if key == "processing_tasks" else ""))
                return label, extra
    return None, ""
