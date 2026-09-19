"""
Web Console for SkillAgent

一個純標準庫（不需要額外安裝 flask / fastapi）的網頁版操作介面，
取代在終端機裡跑 Agent_Runner.py 的方式。

設計原則：
- 完全不修改 Agent_Runner.py，只 import 其中的 SkillAgent 類別與
  _append_discarded_tool_result 這個共用函式，重用既有邏輯。
- 畫面拆成兩塊：
    左邊「使用者 ↔ Agent 對話」：使用者輸入與 AI 的文字回應。
    右邊「系統 / 工具回傳」：EXECUTE 指令觸發的規格書載入或腳本執行結果。
- CLI 版本原本用 /auto on、/compress 這類指令切換模式；這裡沿用同樣的
  指令字串，並新增 /menu 可以查詢目前支援哪些指令。

執行方式：
    python3 web_console.py
    然後瀏覽器打開 http://127.0.0.1:8765
"""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# TOKEN_THRESHOLD（整體上下文自動壓縮門檻）、TOOL_RESULT_TOKEN_THRESHOLD
# （單一工具回傳精簡門檻）與 _content_for_context 都是核心邏輯，定義在
# Agent_Runner.py 裡，CLI（main()）與這裡共用同一份，避免兩邊各自維護一份
# 而逐漸產生行為落差。
from Agent_Runner import (
    SkillAgent,
    _append_discarded_tool_result,
    _content_for_context,
    TOKEN_THRESHOLD,
    TOOL_RESULT_TOKEN_THRESHOLD,
)

# =========================================================
# Agent 狀態（單一使用者、單一 Agent 實例）
# =========================================================

agent = SkillAgent(model=os.environ.get("WEB_CONSOLE_MODEL", "gemma4:e4b"), max_history=30)
agent.reset_conversation()

state = {"auto_mode": False, "hybrid_mode": False, "tool_summary_mode": False}
pending = {"result": None, "mode": None, "tokens": None}  # 等待使用者決策的工具結果（hybrid / manual 模式用）
lock = threading.Lock()

MAX_AUTO_ITERATIONS = 25  # 安全防護：避免 auto 模式下模型無限迴圈卡住伺服器

MENU_TEXT = """可用指令：
/menu                  顯示本說明
/clear                  清空對話記憶，重新開始
/compress               手動壓縮並歸檔目前的歷史對話
/auto on / /auto off    切換 Auto Continue 模式（工具結果自動帶入下一輪，不需確認）
/hybrid on / /hybrid off 切換 Hybrid 模式（每次工具結果都詢問是否加入上下文）
/summarize on / /summarize off 切換工具回傳摘要模式（見下方說明，預設關閉）
/objective set <內容>   設定 Sticky Objective（最高優先任務，會持續提醒 AI）
/objective show         查看目前的 Objective
/objective clear        清除 Objective

不切換 auto／hybrid 時，預設為「手動模式」：每次工具執行完都會等待你確認
是否要把結果加入上下文，畫面下方會出現決策按鈕。

單一工具回傳若超過 {threshold} tokens，不論目前是什麼模式，預設 AI 只會收到
精簡的成功／失敗摘要（避免大量原始輸出干擾推理）。開啟 /summarize on 後，
超過門檻的結果會改由一個獨立、乾淨的 session 做語意摘要（不會混進主對話
的上下文），主 session 拿到的會是摘要後的重點而不只是成功/失敗；若摘要
session 失敗會自動退回原本的成功/失敗摘要，不影響主流程。完整原始內容
永遠都會顯示在「系統 / 工具回傳」面板並標記 ⚠️ 待確認，需自行點
「✅ 我已確認」。""".format(
    threshold=TOOL_RESULT_TOKEN_THRESHOLD
)


# =========================================================
# 核心流程：重用 Agent_Runner.SkillAgent 的方法，改寫成
# 「每次呼叫处理一小段、把過程記錄成 events 回傳給前端」的形式
# =========================================================

def current_mode_label():
    if state["auto_mode"]:
        return "auto"
    if state["hybrid_mode"]:
        return "hybrid"
    return "manual"


def build_stats():
    return {
        "mode": current_mode_label(),
        "tool_summary_mode": state["tool_summary_mode"],
        "current_cwd": agent.current_cwd,
        "container_cwd": agent.container_cwd,
        "objective": agent.sticky_objective or None,
        "total_user_tokens": agent.total_user_tokens,
        "total_ai_tokens": agent.total_ai_tokens,
        "total_tool_tokens": agent.total_tool_tokens,
    }


# summarize_tool_result() 產生的內容固定以這個標籤開頭，用來判斷
# _content_for_context() 這次回傳的是不是「AI 摘要」版本。
_SUMMARY_TAG = "[tool result - AI 摘要]"


def _emit_summary_event(content, events):
    """若這次餵給 AI 的內容是獨立摘要 session 產生的，額外推一個事件到
    前端的「系統 / 工具回傳」面板，讓使用者也能看到摘要結果，
    而不是只能從主對話推測 AI 收到了什麼。"""
    if content.startswith(_SUMMARY_TAG):
        events.append({"channel": "summary", "text": content})


def run_turn(events):
    """反覆執行 ask_ai -> run_tool，直到這一回合自然結束（沒有工具需要執行），
    或是需要使用者對工具結果做決策為止（hybrid / manual 模式）。

    回傳 True 代表目前正在等待使用者決策（awaiting_decision）。
    """
    for _ in range(MAX_AUTO_ITERATIONS):
        ai_msg = agent.ask_ai()
        ai_tokens = agent.count_tokens(ai_msg)
        agent.total_ai_tokens += ai_tokens
        agent.messages.append({'role': 'assistant', 'content': ai_msg})
        events.append({"channel": "chat", "role": "assistant", "text": ai_msg, "tokens": ai_tokens})

        result = agent.run_tool(ai_msg)
        tool_tokens = 0
        if result:
            tool_tokens = agent.count_tokens(result)
            agent.total_tool_tokens += tool_tokens
            oversized = tool_tokens > TOOL_RESULT_TOKEN_THRESHOLD
            events.append({
                "channel": "tool",
                "text": result,
                "tokens": tool_tokens,
                "oversized": oversized,
            })
            if oversized:
                events.append({
                    "channel": "system",
                    "text": (
                        f"⚠️ 此工具回傳約 {tool_tokens} tokens，超過門檻 "
                        f"{TOOL_RESULT_TOKEN_THRESHOLD}，已標記待人工確認；"
                        f"AI 只會收到精簡的成功／失敗摘要。"
                    ),
                })
        else:
            events.append({"channel": "system", "text": "✅ 無工具需要執行"})

        full_context = agent.get_system_prompt() + "\n" + "".join(
            f"{m['role']}: {m['content']}\n" for m in agent.messages
        )
        if agent.count_tokens(full_context) > TOKEN_THRESHOLD:
            agent.compress_context_to_file(num_to_keep=2)
            events.append({"channel": "system", "text": "📦 上下文超過門檻，已自動壓縮並歸檔。"})
            continue

        if not result:
            return False

        if state["auto_mode"]:
            content = _content_for_context(
                result, tool_tokens, agent=agent, use_summary=state["tool_summary_mode"]
            )
            _emit_summary_event(content, events)
            agent.messages.append({'role': 'user', 'content': f"[tool result]\n{content}"})
            events.append({"channel": "system", "text": "♻️ Auto Continue 中..."})
            continue

        # hybrid / manual 都需要暫停，等待使用者對這次工具結果做決策
        pending["result"] = result
        pending["mode"] = "hybrid" if state["hybrid_mode"] else "manual"
        pending["tokens"] = tool_tokens
        return True

    events.append({"channel": "system", "text": "⚠️ 已達安全上限（連續執行過多輪工具），本回合自動中止。"})
    return False


def apply_decision(action, events):
    """套用使用者對待處理工具結果的決策，回傳是否要繼續本回合的迴圈。"""
    result = pending["result"]
    mode = pending["mode"]
    tool_tokens = pending["tokens"] or 0
    pending["result"] = None
    pending["mode"] = None
    pending["tokens"] = None

    # 只有真的要把結果加入上下文時才計算 content——若是摘要模式，這會觸發
    # 一次獨立的 ollama 呼叫，使用者選擇「捨棄」時就不需要浪費這次呼叫。
    if mode == "hybrid":
        if action == "y":
            content = _content_for_context(
                result, tool_tokens, agent=agent, use_summary=state["tool_summary_mode"]
            )
            _emit_summary_event(content, events)
            agent.messages.append({'role': 'user', 'content': f"[tool result]\n{content}"})
        else:
            _append_discarded_tool_result(agent)
        return True  # hybrid 不論加入或捨棄，都會讓 AI 接續推論

    # manual 模式
    if action == "y":
        content = _content_for_context(
            result, tool_tokens, agent=agent, use_summary=state["tool_summary_mode"]
        )
        _emit_summary_event(content, events)
        agent.messages.append({'role': 'user', 'content': f"【系統執行結果】:\n{content}"})
        return True
    if action == "stop":
        return False
    _append_discarded_tool_result(agent)
    return False


def handle_slash_command(message, events):
    """處理 /指令。回傳 True 代表已被當作指令處理，不需再送去給 AI。"""
    text = message.strip()
    lower = text.lower()

    if lower == "/menu":
        events.append({"channel": "system", "text": MENU_TEXT})
        return True
    if lower == "/clear":
        agent.reset_conversation()
        events.append({"channel": "system", "text": "🧹 記憶已清空。"})
        return True
    if lower == "/compress":
        agent.compress_context_to_file(num_to_keep=2)
        events.append({"channel": "system", "text": "🗜️ 歷史已手動壓縮並歸檔。"})
        return True
    if lower == "/auto on":
        state["auto_mode"] = True
        events.append({"channel": "system", "text": "🤖 已開啟 Auto Continue 模式"})
        return True
    if lower == "/auto off":
        state["auto_mode"] = False
        events.append({"channel": "system", "text": "🛑 已關閉 Auto Continue 模式"})
        return True
    if lower == "/hybrid on":
        state["hybrid_mode"] = True
        events.append({"channel": "system", "text": "🧬 已開啟 Hybrid 模式"})
        return True
    if lower == "/hybrid off":
        state["hybrid_mode"] = False
        events.append({"channel": "system", "text": "🧬 已關閉 Hybrid 模式"})
        return True
    if lower == "/summarize on":
        state["tool_summary_mode"] = True
        events.append({"channel": "system", "text": "🧠 已開啟工具回傳摘要模式（超過門檻的結果會由獨立 session 摘要）"})
        return True
    if lower == "/summarize off":
        state["tool_summary_mode"] = False
        events.append({"channel": "system", "text": "🧠 已關閉工具回傳摘要模式（改回精簡成功/失敗判定）"})
        return True
    if lower.startswith("/objective set "):
        agent.sticky_objective = text[len("/objective set "):].strip()
        events.append({"channel": "system", "text": "🎯 已設定 Objective"})
        return True
    if lower == "/objective show":
        events.append({"channel": "system", "text": f"🎯 Current: {agent.sticky_objective or 'None'}"})
        return True
    if lower == "/objective clear":
        agent.sticky_objective = ""
        events.append({"channel": "system", "text": "🧹 已清除 Sticky Objective"})
        return True

    return False


# =========================================================
# 前端頁面（純 HTML / CSS / JS，無外部依賴、可離線使用）
# =========================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>SkillAgent Web Console</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, "Segoe UI", "PingFang TC", "Microsoft JhengHei", sans-serif;
    background: #1e1f22; color: #e3e3e3; height: 100vh; display: flex; flex-direction: column;
  }
  header {
    padding: 10px 16px; background: #2b2d31; border-bottom: 1px solid #3a3c40;
    display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;
  }
  header h1 { font-size: 16px; margin: 0; }
  #status { font-size: 12px; color: #9aa0a6; display: flex; gap: 14px; flex-wrap: wrap; }
  main { flex: 1; display: flex; min-height: 0; }
  .panel { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .panel + .panel { border-left: 1px solid #3a3c40; }
  .panel h2 {
    font-size: 13px; margin: 0; padding: 8px 12px; background: #26282c;
    border-bottom: 1px solid #3a3c40; color: #c7c9cc;
  }
  .log { flex: 1; overflow-y: auto; padding: 10px 12px; font-size: 13px; }
  .entry { margin-bottom: 10px; padding: 8px 10px; border-radius: 6px; white-space: pre-wrap; word-break: break-word; }
  .entry.user { background: #2c3e50; }
  .entry.assistant { background: #2b2d31; border: 1px solid #3a3c40; }
  .entry.tool { background: #1f2a24; border: 1px solid #2f4a3a; font-family: "Cascadia Code", Consolas, monospace; }
  .entry.system { background: #2a2620; border: 1px solid #4a4030; color: #d8c9a3; font-style: italic; }
  .entry.summary { background: #241f33; border: 1px solid #5a4a8f; color: #cfc3f0; }
  .entry.summary .tag { color: #b39ddb; opacity: 1; }
  .entry.tool.oversized { border: 1px solid #b0873f; box-shadow: 0 0 0 1px #b0873f inset; }
  .entry.tool.oversized .tag { color: #e6b95c; opacity: 1; }
  .entry.tool.oversized.reviewed { border-color: #2f4a3a; box-shadow: none; opacity: 0.75; }
  .entry .tag { font-size: 10px; text-transform: uppercase; opacity: 0.6; margin-bottom: 4px; }
  .entry .ack-btn {
    display: inline-block; margin-top: 6px; padding: 4px 10px; font-size: 11px;
    background: #4a4c50; border-radius: 4px; cursor: pointer;
  }
  footer { border-top: 1px solid #3a3c40; padding: 10px 12px; background: #2b2d31; }
  #decision-bar { display: none; margin-bottom: 8px; gap: 8px; align-items: center; font-size: 13px; }
  #decision-bar.show { display: flex; }
  #decision-bar button { cursor: pointer; }
  .input-row { display: flex; gap: 8px; }
  textarea#msg {
    flex: 1; resize: none; height: 54px; background: #1e1f22; color: #e3e3e3;
    border: 1px solid #3a3c40; border-radius: 6px; padding: 8px; font-size: 13px; font-family: inherit;
  }
  button { background: #3a6df0; color: white; border: none; border-radius: 6px; padding: 8px 16px; font-size: 13px; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.secondary { background: #4a4c50; }
  button.danger { background: #b0473f; }
</style>
</head>
<body>

<header>
  <h1>🤖 SkillAgent Web Console</h1>
  <div id="status">
    <span id="stat-mode">mode: manual</span>
    <span id="stat-cwd">cwd: -</span>
    <span id="stat-tokens">tokens: -</span>
  </div>
</header>

<main>
  <section class="panel">
    <h2>💬 使用者 ↔ Agent 對話</h2>
    <div class="log" id="chat-log"></div>
  </section>
  <section class="panel">
    <h2>🛠️ 系統 / 工具回傳</h2>
    <div class="log" id="tool-log"></div>
  </section>
</main>

<footer>
  <div id="decision-bar">
    <span>⏸️ 有工具結果待決策：</span>
    <button onclick="sendDecision('y')">✅ 加入上下文</button>
    <button class="secondary" id="decision-n">🚫 捨棄</button>
    <button class="danger" id="decision-stop" onclick="sendDecision('stop')">⏹️ 停止本回合</button>
  </div>
  <div class="input-row">
    <textarea id="msg" placeholder="輸入訊息，或用 /menu 查詢可用指令...（Enter 送出，Shift+Enter 換行）"></textarea>
    <button id="send-btn" onclick="sendMessage()">送出</button>
  </div>
</footer>

<script>
const chatLog = document.getElementById('chat-log');
const toolLog = document.getElementById('tool-log');
const msgBox = document.getElementById('msg');
const sendBtn = document.getElementById('send-btn');
const decisionBar = document.getElementById('decision-bar');

function renderEntry(container, cls, tag, text, oversized) {
  const div = document.createElement('div');
  div.className = 'entry ' + cls + (oversized ? ' oversized' : '');
  if (tag) {
    const tagEl = document.createElement('div');
    tagEl.className = 'tag';
    tagEl.textContent = oversized ? tag + ' ⚠️ 待確認' : tag;
    div.appendChild(tagEl);
  }
  const textEl = document.createElement('div');
  textEl.textContent = text;
  div.appendChild(textEl);
  if (oversized) {
    const ackBtn = document.createElement('div');
    ackBtn.className = 'ack-btn';
    ackBtn.textContent = '✅ 我已確認';
    ackBtn.onclick = () => {
      div.classList.add('reviewed');
      ackBtn.remove();
    };
    div.appendChild(ackBtn);
  }
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function renderEvents(events) {
  events.forEach(ev => {
    if (ev.channel === 'chat') {
      renderEntry(chatLog, ev.role, ev.role === 'user' ? '你' : 'AI', ev.text);
    } else if (ev.channel === 'tool') {
      renderEntry(toolLog, 'tool', '系統回傳', ev.text, ev.oversized);
    } else if (ev.channel === 'summary') {
      renderEntry(toolLog, 'summary', '🧠 AI 摘要（獨立 session）', ev.text);
    } else if (ev.channel === 'system') {
      renderEntry(toolLog, 'system', '系統', ev.text);
    }
  });
}

function updateStatus(stats) {
  document.getElementById('stat-mode').textContent = 'mode: ' + stats.mode;
  document.getElementById('stat-cwd').textContent = 'cwd: ' + stats.current_cwd;
  document.getElementById('stat-tokens').textContent =
    `tokens: user ${stats.total_user_tokens} / ai ${stats.total_ai_tokens} / tool ${stats.total_tool_tokens}`;
}

function setBusy(busy) {
  sendBtn.disabled = busy;
  msgBox.disabled = busy;
}

function showDecisionBar(pendingMode) {
  decisionBar.classList.add('show');
  const stopBtn = document.getElementById('decision-stop');
  const nBtn = document.getElementById('decision-n');
  nBtn.onclick = () => sendDecision('n');
  // manual 模式才有獨立的「停止」選項；hybrid 模式捨棄後仍會繼續推論
  stopBtn.style.display = (pendingMode === 'manual') ? 'inline-block' : 'none';
}

function hideDecisionBar() {
  decisionBar.classList.remove('show');
}

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  return res.json();
}

async function sendMessage() {
  const text = msgBox.value.trim();
  if (!text) return;
  setBusy(true);
  hideDecisionBar();
  msgBox.value = '';
  try {
    const data = await postJSON('/api/send', { message: text });
    if (data.error) {
      renderEntry(toolLog, 'system', '錯誤', data.error);
      return;
    }
    renderEvents(data.events);
    updateStatus(data.stats);
    if (data.awaiting_decision) showDecisionBar(data.pending_mode);
  } finally {
    setBusy(false);
    msgBox.focus();
  }
}

async function sendDecision(action) {
  setBusy(true);
  hideDecisionBar();
  try {
    const data = await postJSON('/api/decision', { action });
    if (data.error) {
      renderEntry(toolLog, 'system', '錯誤', data.error);
      return;
    }
    renderEvents(data.events);
    updateStatus(data.stats);
    if (data.awaiting_decision) showDecisionBar(data.pending_mode);
  } finally {
    setBusy(false);
    msgBox.focus();
  }
}

msgBox.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// 初始載入時抓一次目前狀態
fetch('/api/status').then(r => r.json()).then(updateStatus);
</script>

</body>
</html>
"""


# =========================================================
# HTTP Server（純標準庫，無外部依賴）
# =========================================================

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
        self.send_error(404)

    def do_POST(self):
        with lock:
            if self.path == "/api/send":
                self._handle_send()
            elif self.path == "/api/decision":
                self._handle_decision()
            else:
                self.send_error(404)

    def _handle_send(self):
        data = self._read_json()
        message = (data.get("message") or "").strip()
        events = []

        if pending["mode"]:
            self._send_json({"error": "尚有待決策的工具結果，請先回應決策再繼續。"}, status=409)
            return

        if not message:
            self._send_json({"events": [], "awaiting_decision": False, "stats": build_stats()})
            return

        if handle_slash_command(message, events):
            self._send_json({"events": events, "awaiting_decision": False, "stats": build_stats()})
            return

        user_tokens = agent.count_tokens(message)
        agent.total_user_tokens += user_tokens
        agent.messages.append({'role': 'user', 'content': message})
        events.append({"channel": "chat", "role": "user", "text": message, "tokens": user_tokens})

        awaiting = run_turn(events)
        resp = {"events": events, "awaiting_decision": awaiting, "stats": build_stats()}
        if awaiting:
            resp["pending_mode"] = pending["mode"]
        self._send_json(resp)

    def _handle_decision(self):
        data = self._read_json()
        action = (data.get("action") or "").strip().lower()
        events = []

        if not pending["mode"]:
            self._send_json({"error": "目前沒有待決策的工具結果。"}, status=409)
            return

        should_continue = apply_decision(action, events)
        awaiting = run_turn(events) if should_continue else False
        resp = {"events": events, "awaiting_decision": awaiting, "stats": build_stats()}
        if awaiting:
            resp["pending_mode"] = pending["mode"]
        self._send_json(resp)


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


if __name__ == "__main__":
    main()
