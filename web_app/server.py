"""HTTP 伺服器：路由（/、/api/*、/static/vision/*）、NDJSON 串流（EventStream）與主程式 main()。"""
import json
import os
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from vision import VisionError
from vision.web import static_file as vision_static_file
from agent_core.protocol import attach_skill_docs
from agent_core.turn import after_turn_compression
from .commands import handle_slash_command, SLASH_COMMANDS
from .flows import (
    _run_vision_subsession,
    apply_decision,
    build_stats,
    clear_plan_for_new_task,
    DEFAULT_VISION_PROMPT,
    handle_plan_response,
    handle_skill_draft_response,
    run_turn,
    start_plan_flow,
)
from .state import (
    agent,
    DEFAULT_SKILL_PROMPT,
    load_pending_skill,
    lock,
    pending,
    pending_skill_names,
    plan_pending,
    remove_pending_skill,
    state,
    take_all_pending_skills,
    vision_session,
)


# =========================================================
# 前端頁面（純 HTML / CSS / JS，無外部依賴、可離線使用）
# =========================================================

# 網頁本身（HTML／CSS／JS 都在一個檔案，無外部依賴、可離線使用）放在 static/index.html，啟動時讀一次
HTML_PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html"),
                 encoding="utf-8").read()


# =========================================================
# HTTP Server（純標準庫，無外部依賴）
# =========================================================

class EventStream:
    """取代原本累積用的 events list：介面同樣只有 append()，但每 append
    一筆就立刻以 NDJSON（一行一個 JSON）寫回 HTTP 回應並 flush，讓瀏覽器
    在每次推論／工具執行完成的當下就看到結果，而不是等整回合結束。

    每筆事件後面會順帶推一筆最新 stats，讓 header 的 token 計數也即時更新。

    若使用者中途關掉分頁導致寫入失敗，不往外拋例外，只標記 client_gone
    並靜默略過之後的寫入——讓 run_turn 等流程照原本的方式跑完，Agent 的
    狀態（messages、pending、token 計數）才不會停在半途，跟改成串流前
    「請求一送出，後端一定跑完整回合」的行為一致。
    """

    def __init__(self, handler):
        self._handler = handler
        self.client_gone = False

    def _write(self, payload):
        if self.client_gone:
            return
        try:
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            self._handler.wfile.write(line.encode("utf-8"))
            self._handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.client_gone = True

    def append(self, event):
        self._write({"type": "event", "event": event})
        self._write({"type": "stats", "stats": build_stats()})

    def error(self, text):
        self._write({"type": "error", "error": text})

    def done(self, **fields):
        self._write({"type": "done", "stats": build_stats(), **fields})


class ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 安靜一點，避免洗版終端機

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _begin_stream(self):
        """送出串流回應的 header 並回傳 EventStream。不帶 Content-Length，
        以 HTTP/1.0 的「連線關閉」作為回應結尾，瀏覽器的 fetch 會邊收邊給。"""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        return EventStream(self)

    def _finish(self, stream, awaiting_decision):
        """推出最後一行 done：帶前端收尾要用的旗標與最新 stats。"""
        stream.done(
            awaiting_decision=awaiting_decision,
            awaiting_plan=plan_pending["active"],
            awaiting_skill_draft=agent.pending_skill_draft is not None,
            pending_mode=pending["mode"] if awaiting_decision else None,
        )

    def _finish_turn(self, stream, awaiting_decision):
        """回合真正結束（不在等決策、也沒有計畫待核准）時的收尾：先做軟水位檢查再送 done。
        AI 的最終回覆事件早已串流到前端，這裡的壓縮不影響使用者看到答案的時間；
        /parallel_cal on 時壓縮在背景執行緒進行，done 會立刻送出。"""
        if not awaiting_decision and not plan_pending["active"]:
            after_turn_compression(
                agent, state["parallel_cal"],
                lambda text: stream.append({"channel": "system", "text": text}),
            )
        self._finish(stream, awaiting_decision)

    def do_GET(self):
        if self.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            self._send_json(build_stats())
            return
        if self.path == "/api/commands":
            # 「/」選單的內容：功能開關／指令 + SKILLS.md 技能清單（含分類、是否有經驗記憶）
            self._send_json({"commands": SLASH_COMMANDS, "skills": agent.list_skills()})
            return
        if self.path.startswith("/api/results/"):
            # 📄 工具結果存檔原文：/api/results/<編號|latest|index|檔名>（純文字；讓使用者點卡片上的編號就能看整份原文）
            from urllib.parse import unquote
            path = agent.result_file_path(unquote(self.path[len("/api/results/"):]))
            if not path:
                self.send_error(404, "no such tool result")
                return
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        static = vision_static_file(self.path)
        if static:
            body, content_type = static
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/api/vision/"):
            self._handle_vision(self.path[len("/api/vision/"):])
            return
        if self.path.startswith("/api/skill/"):
            self._handle_skill(self.path[len("/api/skill/"):])
            return
        if self.path not in ("/api/send", "/api/decision"):
            self.send_error(404)
            return
        data = self._read_json()
        with lock:
            stream = self._begin_stream()
            try:
                if self.path == "/api/send":
                    self._handle_send(data, stream)
                else:
                    self._handle_decision(data, stream)
            except Exception as e:
                # 串流 header 已送出，無法再改 HTTP 狀態碼，改以 error 訊息
                # 告知前端；同時把 traceback 印在伺服器端方便除錯。
                traceback.print_exc()
                stream.error(f"伺服器處理時發生錯誤：{e}")
                self._finish(stream, bool(pending["mode"]))

    def _handle_skill(self, action):
        """📘 手動載入技能規格的 JSON 端點（不串流、不進 agent 的 lock，狀態在 pending_skills 自己的 lock）。"""
        data = self._read_json()
        try:
            if action == "load":
                self._send_json(load_pending_skill(data.get("name")))
            elif action == "remove":
                self._send_json({"pending": remove_pending_skill(data.get("name"))})
            elif action == "clear":
                take_all_pending_skills()
                self._send_json({"pending": []})
            elif action == "status":
                self._send_json({"pending": pending_skill_names()})
            else:
                self.send_error(404)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": f"伺服器處理技能規格時發生錯誤：{e}"}, status=500)

    def _handle_vision(self, action):
        """📷 影像附件的一般 JSON 端點（不串流）。附件狀態在 vision_session 自己的 lock 裡，
        不進 agent 的 lock：擷取畫面（PowerShell）可能要幾秒，不該卡住其他請求。"""
        data = self._read_json()
        try:
            if action == "screens":
                payload = vision_session.screens()
            elif action == "capture":
                payload = vision_session.capture(data.get("screen_index"))
            elif action == "crop":
                payload = vision_session.crop(
                    data.get("x1"), data.get("y1"), data.get("x2"), data.get("y2"),
                    data.get("preview_width"), data.get("preview_height"),
                )
            elif action == "upload":
                payload = vision_session.add_data_url(data.get("data"), data.get("name") or "上傳的影像")
            elif action == "remove":
                payload = vision_session.remove(data.get("id"))
            elif action == "clear":
                payload = vision_session.clear()
            elif action == "status":
                payload = vision_session.status()
            else:
                self.send_error(404)
                return
        except VisionError as e:
            self._send_json({"error": str(e)}, status=400)
            return
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": f"伺服器處理影像時發生錯誤：{e}"}, status=500)
            return
        self._send_json(payload)

    def _handle_send(self, data, stream):
        message = (data.get("message") or "").strip()

        if pending["mode"]:
            stream.error("尚有待決策的工具結果，請先回應決策再繼續。")
            self._finish(stream, True)
            return

        # 有計畫待核准時，輸入框的內容一律視為對計畫的回應（y／n／修改意見），
        # 不當作新指令或 slash command 處理——跟 CLI 版 _run_plan_flow 的
        # input() 迴圈行為一致。
        if plan_pending["active"]:
            outcome = handle_plan_response(message, stream)
            awaiting_decision = False
            if outcome == "approved":
                awaiting_decision = run_turn(stream)
            if outcome == "revised":
                self._finish(stream, False)  # 仍在規劃中，不算回合結束
            else:
                self._finish_turn(stream, awaiting_decision)  # 核准後跑完、或取消，都是回合結束
            return

        # 有技能草稿待決定時，輸入框的內容一律視為對草稿的回應（y／t／n／修改意見），
        # 與計畫核准同一種處理方式；這裡不呼叫模型的主對話，也不消耗附件與待送技能規格。
        if agent.pending_skill_draft:
            handle_skill_draft_response(message, stream)
            self._finish(stream, False)
            return

        if not message:
            if vision_session.count() == 0 and not pending_skill_names():
                self._finish(stream, False)
                return
            # 只附圖／只載入技能而不打字：用預設提示詞
            message = DEFAULT_VISION_PROMPT if vision_session.count() else DEFAULT_SKILL_PROMPT

        if handle_slash_command(message, stream):
            self._finish(stream, False)  # slash 指令不消耗附件
            return

        # 📷📘 只有「新任務」才消費附件與手動載入的技能規格（slash 指令與計畫核准／修改意見不會）
        attachments = vision_session.take_all()
        skill_items = take_all_pending_skills()

        user_tokens = agent.count_tokens(message)
        agent.total_user_tokens += user_tokens
        stream.append({
            "channel": "chat", "role": "user", "text": message,
            "tokens": user_tokens, "attachments": len(attachments),
            "skills": [name for name, _ in skill_items],
        })

        # 新任務開始：上一個已核准的計畫到此結束（見 clear_plan_for_new_task）
        clear_plan_for_new_task(stream)

        # 記下使用者這句話（任務線：最近 3 句），跟 CLI 版（agent_core.cli.main）一致；
        # 獨立 session 摘要或 recall 時拿它當「使用者的目標」的一部分（見 SkillAgent._build_task_anchor_text）
        agent.set_current_task(message)

        # 有附加影像：先跑獨立視覺 sub-session，把分析結果以文字併入這次的使用者訊息，
        # 之後不論是 plan 模式還是直接執行，主 Agent 拿到的都是「原文 + 影像分析」的純文字
        content = _run_vision_subsession(message, attachments, stream) if attachments else message

        # 📘 手動載入的技能規格附在這則訊息後面（AI 可直接依規格裡的腳本路徑執行，省掉一輪「先載規格」）
        if skill_items:
            blocks = [block for _, block in skill_items]
            skill_tokens = sum(agent.count_tokens(b) for b in blocks)
            agent.total_tool_tokens += skill_tokens
            content = attach_skill_docs(content, blocks)
            stream.append({"channel": "system", "text": (
                f"📘 隨訊息載入技能規格：{', '.join(name for name, _ in skill_items)}（≈{skill_tokens} tokens）"
            )})

        if state["plan_mode"]:
            start_plan_flow(content, stream)
            self._finish(stream, False)
            return

        agent.messages.append({'role': 'user', 'content': content})
        self._finish_turn(stream, run_turn(stream))

    def _handle_decision(self, data, stream):
        action = (data.get("action") or "").strip().lower()

        if not pending["mode"]:
            stream.error("目前沒有待決策的工具結果。")
            self._finish(stream, False)
            return

        should_continue = apply_decision(action, stream)
        awaiting = run_turn(stream) if should_continue else False
        self._finish_turn(stream, awaiting)


def main():
    host = os.environ.get("WEB_CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_CONSOLE_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), ConsoleHandler)

    print("\n" + "=" * 50)
    print(f"🌐 SkillAgent Web Console 已啟動: http://{host}:{port}")
    print("按 Ctrl+C 停止伺服器。")
    print("=" * 50)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Bye")
        server.shutdown()
