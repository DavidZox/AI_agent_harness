"""fih_rmf_system web_console 的離線替身（純標準庫，不需 FastAPI／uvicorn／ROS2）。

模擬 workpackage_est 會用到的全部端點，讓技能可以在沒有機器人系統的機器上測試：
  POST   /api/send_tasks              body 為 TaskRequest 陣列 → {"status","count","details":[每筆的 JSON 字串]}
                                      （details 比照 FastAPIBridgeNode.publish_as_string 發布到 incoming_work_packages 的 JSON）
  DELETE /api/work_packages/{id}      → {"status": "success", "package_id": id}（之後快照中該 package 消失）
  DELETE /api/overpending_tasks/{id}  → {"status": "success", "task_id": id}（之後快照中該任務消失）
  GET    /api/stations  /api/robots   場域的站點與機隊名冊
  WS     /api/ws                      推一幀 distribute 快照（含 "orchestrtor" 鍵），格式同 allocator_routes.websocket_endpoint
  GET    /api/received                這次啟動以來收到的所有 work package（除錯用，真的 web_console 沒有）
  加 ?fail=503 或 ?fail=422 到 POST /api/send_tasks 可模擬「ROS 2 未就緒」與欄位驗證失敗。

預設 port 8020 與真的 web_console 相同——真系統（容器 network_mode: host）在跑時請換 --port，並以
RMF_WEB_CONSOLE_URL 或技能的 --url 指到這裡。
    python3 scripts/mock_server.py [--port 8021]
"""
import base64
import hashlib
import json
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

LOCK = threading.Lock()
RECEIVED = []            # 收到的 work package（dict），依序
CANCELLED = set()        # 被 DELETE 的 package_id
OVERPENDING = [{"id": "OVP-DEMO::0::0::1", "wait": 12.3}]   # 預放一筆可刪的 OverPending 任務
ROBOTS = ["tb1", "tb2", "tb3", "tb4", "tb5"]
STATIONS = ["home", "a0", "a3", "a4", "a6", "a7", "a8"]
STEP = [0]
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def bridge_json(task):
    """比照 FastAPIBridgeNode.publish_as_string 的欄位與順序。"""
    return json.dumps({
        "task_id": task.get("task_id"),
        "task_type": task.get("task_type", "regular"),
        "level": task.get("level", "normal"),
        "weight": task.get("weight", 0),
        "assign_amr": task.get("assign_amr", "tb1"),
        "loop": task.get("loop", False),
        "loop_count": task.get("loop_count"),
        "stops": [{"station": s.get("station", "home"), "description": s.get("description") or ""} for s in task.get("stops", [])],
    }, ensure_ascii=False)


def snapshot():
    """比照 allocator_routes.websocket_endpoint 推送的 payload：distribute 快照 + "orchestrtor" 鍵。"""
    STEP[0] += 1
    with LOCK:
        packages = []
        for t in RECEIVED:
            if t["task_id"] in CANCELLED:
                continue
            stations = [s.get("station") for s in t.get("stops", [])]
            packages.append({
                "package_id": t["task_id"], "status": "RUNNING", "loop": t.get("loop", False),
                "loop_count": t.get("loop_count"), "iteration": 0, "station_index": 0,
                "station_count": len(stations), "stations": stations,
                "current_item": {"item_id": f"{t['task_id']}::0::0::1", "station": stations[0] if stations else None,
                                 "status": "DISPATCHED", "robot_id": t.get("assign_amr") or None,
                                 "waiting_for_robot": not t.get("assign_amr")},
                "items_done": 0, "items_total": 1, "updated_at": time.time(),
            })
        ovp = list(OVERPENDING)
    processing = [{"id": p["current_item"]["item_id"], "type": "regular", "robot": p["current_item"]["robot_id"],
                   "station": p["current_item"]["station"]} for p in packages if p["current_item"]["robot_id"]]
    return {
        "step_counter": STEP[0], "state": "IDLE",
        "events": [f"[{time.strftime('%H:%M:%S')}] mock distribute 運作中"],
        "buffer_tasks": [], "raw_tasks": [], "overpending_tasks": ovp, "processing_tasks": processing,
        "robots": {rid: {"state": "duty" if any(p["robot"] == rid for p in processing) else "standby", "real_state": "IDLE",
                         "last_task_id": next((p["id"] for p in processing if p["robot"] == rid), "None"),
                         "status": "ONLINE" if rid in ("tb1", "tb2") else "OFFLINE", "delay": 0.3} for rid in ROBOTS},
        "params": {},
        "orchestrtor": {"step_counter": STEP[0], "events": [f"[{time.strftime('%H:%M:%S')}] mock orchestrtor 運作中"] +
                        [f"📦 收到 work package {p['package_id']}" for p in packages[-3:]], "packages": packages},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _websocket(self):
        """最小 WebSocket 伺服端：握手 → 推一幀快照 → close。"""
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        self.wfile.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        payload = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
        n = len(payload)
        header = b"\x81" + (bytes([n]) if n < 126 else (b"\x7e" + struct.pack("!H", n) if n < 65536 else b"\x7f" + struct.pack("!Q", n)))
        self.wfile.write(header + payload)
        self.wfile.flush()
        time.sleep(0.2)
        try:
            self.wfile.write(b"\x88\x00")
        except OSError:
            pass
        self.close_connection = True

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/ws" and "websocket" in (self.headers.get("Upgrade") or "").lower():
            return self._websocket()
        if path == "/api/robots":
            return self._json({"robots": ROBOTS})
        if path == "/api/stations":
            return self._json({"stations": [{"id": s, "label": s} for s in STATIONS]})
        if path == "/api/received":
            with LOCK:
                return self._json({"received": RECEIVED, "cancelled": sorted(CANCELLED), "overpending": OVERPENDING})
        self._json({"detail": "Not Found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/work_packages/"):
            pid = unquote(path[len("/api/work_packages/"):])
            with LOCK:
                CANCELLED.add(pid)
            print(f"🗑️ 要求刪除 work package: {pid}", flush=True)
            return self._json({"status": "success", "package_id": pid})
        if path.startswith("/api/overpending_tasks/"):
            tid = unquote(path[len("/api/overpending_tasks/"):])
            with LOCK:
                OVERPENDING[:] = [t for t in OVERPENDING if t["id"] != tid]
            print(f"🗑️ 要求刪除 OverPending 任務: {tid}", flush=True)
            return self._json({"status": "success", "task_id": tid})
        self._json({"detail": "Not Found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/send_tasks":
            return self._json({"detail": "Not Found"}, 404)
        fail = parse_qs(parsed.query).get("fail", [None])[0]
        if fail == "503":
            return self._json({"detail": "ROS 2 未就緒"}, 503)
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            tasks = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            return self._json({"detail": [{"msg": "body 不是合法 JSON"}]}, 422)
        if fail == "422" or not isinstance(tasks, list) or not all(isinstance(t, dict) and t.get("task_id") for t in tasks):
            return self._json({"detail": [{"loc": ["body"], "msg": "expected a list of TaskRequest with task_id"}]}, 422)
        details = []
        with LOCK:
            for t in tasks:
                if t["task_id"] not in {r["task_id"] for r in RECEIVED}:   # orchestrtor 會忽略重複 id
                    RECEIVED.append(t)
                details.append(bridge_json(t))
                stops = " → ".join(s.get("station", "?") for s in t.get("stops", []))
                print(f"📦 收到 work package {t.get('task_id')}（{len(t.get('stops', []))} 站：{stops}，loop={t.get('loop')}）", flush=True)
        self._json({"status": "success", "count": len(tasks), "details": details})


def main():
    port = 8020
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"🧪 mock web_console 啟動於 http://0.0.0.0:{port}（POST /api/send_tasks、DELETE /api/work_packages/<id>、WS /api/ws；Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
