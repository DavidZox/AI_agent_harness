"""
Gemma4 Vision Web Sniper（薄殼版）

「截全螢幕 → 瀏覽器全螢幕框選裁切 → 丟給多模態模型推論」的獨立單頁工具。
截圖後端自動偵測：WSL 用 PowerShell、Linux X11 桌面用 Pillow ImageGrab，都沒有時
由瀏覽器的 getDisplayMedia 擷取（需以 localhost/https 開啟）。所有邏輯都來自頂層的 `vision/` library：

- 螢幕擷取／框選／影像清單：vision.VisionSession（跟 web_console 的 📷 附圖共用）
- 推論：vision.analyze（逾時與錯誤分類只維護一份）
- 前端框選覆蓋層與螢幕選單：vision/web/snip.js、snip.css（以 /static/vision/... 載入）

這個檔案只剩 HTTP 路由與這個頁面自己的版面（縮圖清單、Prompt、結果框）。
多螢幕選單、連續框選、Esc 離開等行為與先前版本相同。

執行方式：
    python3 subagent/screen_gemma4_web.py
    然後瀏覽器打開 http://127.0.0.1:8766
環境變數：SCREEN_GEMMA_WEB_MODEL（預設同 vision 的 VISION_MODEL / gemma4:e4b）、
        SCREEN_GEMMA_WEB_HOST、SCREEN_GEMMA_WEB_PORT
"""

import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 本檔以 `python3 subagent/screen_gemma4_web.py` 執行時 sys.path[0] 是 subagent/，
# 要把專案根目錄加進來才 import 得到 vision
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from vision import DEFAULT_MODEL, VisionError, VisionSession, analyze  # noqa: E402
from vision.web import static_file  # noqa: E402

MODEL_NAME = os.environ.get("SCREEN_GEMMA_WEB_MODEL", DEFAULT_MODEL)

session = VisionSession()


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>Gemma4 Vision Web Sniper</title>
<link rel="stylesheet" href="/static/vision/snip.css">
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
  main { flex: 1; display: flex; min-height: 0; overflow: auto; justify-content: center; }
  #panel { flex: 1; max-width: 720px; padding: 16px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .toolbar { display: flex; gap: 8px; flex-wrap: wrap; }
  button {
    background: #3a6df0; color: white; border: none; border-radius: 6px; padding: 8px 14px;
    font-size: 13px; cursor: pointer;
  }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.secondary { background: #4a4c50; }
  button.danger { background: #b0473f; }
  button.success { background: #28a745; }
  #hint { font-size: 12px; color: #9aa0a6; }
  h2 { font-size: 13px; margin: 0 0 8px 0; color: #c7c9cc; }
  #thumbs { display: flex; flex-wrap: wrap; gap: 8px; }
  .thumb { position: relative; }
  .thumb img { width: 96px; height: 72px; object-fit: cover; border-radius: 4px; border: 1px solid #3a3c40; display: block; }
  .thumb .rm {
    position: absolute; top: -6px; right: -6px; width: 18px; height: 18px; border-radius: 50%;
    background: #b0473f; color: white; font-size: 11px; line-height: 18px; text-align: center; cursor: pointer;
  }
  textarea#prompt {
    width: 100%; height: 90px; resize: vertical; background: #1e1f22; color: #e3e3e3;
    border: 1px solid #3a3c40; border-radius: 6px; padding: 8px; font-size: 13px; font-family: inherit;
  }
  #result {
    flex: 1; background: #26282c; border: 1px solid #3a3c40; border-radius: 6px;
    padding: 10px; font-size: 13px; white-space: pre-wrap; overflow-y: auto; min-height: 160px;
  }
</style>
</head>
<body>

<header>
  <h1>🤖 Gemma4 Vision Web Sniper</h1>
  <div id="status">
    <span id="stat-shot">畫面：未擷取</span>
    <span id="stat-count">已加入圖片：0</span>
  </div>
</header>

<main>
  <section id="panel">
    <div class="toolbar">
      <button onclick="VisionSnip.capture()">📸 擷取畫面</button>
      <button class="secondary" onclick="fileInput.click()">📁 選擇檔案</button>
      <button class="danger" onclick="clearImages()">🗑 清空已加入的圖片</button>
      <input type="file" id="file-input" accept="image/*" multiple style="display:none">
    </div>
    <div id="hint">按下「📸 擷取畫面」時，若伺服器端能截圖（WSL 或 Linux X11 桌面）且偵測到多台螢幕會先讓你選要擷取哪一台（單螢幕時直接略過這步）；伺服器端不能截圖時會改由瀏覽器的「分享畫面」讓你挑螢幕。之後進入全螢幕框選模式：直接在畫面上拖曳滑鼠選取要加入的區域，放開滑鼠就會自動裁切並加入下面的清單，可以連續框選多次。選完後按 Esc 或畫面上方的「結束擷取」離開。也可以用「📁 選擇檔案」直接加入本機的圖片。</div>

    <div>
      <h2>已加入的圖片</h2>
      <div id="thumbs"></div>
    </div>
    <div>
      <h2>Prompt</h2>
      <textarea id="prompt">解釋一下這張圖</textarea>
    </div>
    <button class="success" id="run-btn" onclick="runInference()">🤖 開始推論</button>
    <div style="display:flex; flex-direction:column; min-height:0;">
      <h2>Gemma Result</h2>
      <div id="result">[ 尚無結果 ]</div>
    </div>
  </section>
</main>

<script src="/static/vision/snip.js"></script>
<script>
const thumbs = document.getElementById('thumbs');
const resultBox = document.getElementById('result');
const runBtn = document.getElementById('run-btn');
const statShot = document.getElementById('stat-shot');
const statCount = document.getElementById('stat-count');
const fileInput = document.getElementById('file-input');
const postJSON = VisionSnip.postJSON;

function addThumb(item) {
  const wrap = document.createElement('div');
  wrap.className = 'thumb';
  const img = document.createElement('img');
  img.src = item.thumbnail; img.title = `${item.width}x${item.height}`;
  const rm = document.createElement('div');
  rm.className = 'rm'; rm.textContent = '✕'; rm.title = '移除';
  rm.onclick = async () => {
    const data = await postJSON('/api/remove', { id: item.id });
    wrap.remove();
    statCount.textContent = `已加入圖片：${data.count}`;
  };
  wrap.appendChild(img); wrap.appendChild(rm);
  thumbs.appendChild(wrap);
  statCount.textContent = `已加入圖片：${item.count}`;
}

fileInput.addEventListener('change', async () => {
  for (const file of Array.from(fileInput.files || [])) {
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const r = new FileReader(); r.onload = () => resolve(r.result); r.onerror = () => reject(r.error); r.readAsDataURL(file);
      });
      const data = await postJSON('/api/upload', { name: file.name, data: dataUrl });
      if (data.error) { alert(`加入 ${file.name} 失敗：${data.error}`); continue; }
      addThumb(data);
    } catch (e) { alert(`讀取 ${file.name} 失敗：${e}`); }
  }
  fileInput.value = '';
});

async function clearImages() {
  const data = await postJSON('/api/clear', {});
  thumbs.innerHTML = '';
  statCount.textContent = `已加入圖片：${data.count}`;
  resultBox.textContent = '[ 尚無結果 ]';
}

async function runInference() {
  const prompt = document.getElementById('prompt').value.trim();
  runBtn.disabled = true;
  resultBox.textContent = 'Gemma 4 思考中...';
  try {
    const data = await postJSON('/api/run', { prompt });
    resultBox.textContent = data.error ? ('錯誤：' + data.error) : data.result;
  } catch (e) {
    resultBox.textContent = '錯誤：' + e;
  } finally {
    runBtn.disabled = false;
  }
}

document.body.insertAdjacentHTML('beforeend', VisionSnip.markup());
VisionSnip.init({
  apiBase: '/api',
  onAdded: addThumb,
  onStatus: (t) => { statShot.textContent = '畫面：' + t; },
});

fetch('/api/status').then(r => r.json()).then(data => {
  statCount.textContent = `已加入圖片：${data.count}`;
});
</script>

</body>
</html>
"""


class SniperHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 安靜一點，避免洗版終端機

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, body, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self):
        if self.path == "/":
            self._send_bytes(HTML_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        static = static_file(self.path)
        if static:
            self._send_bytes(*static)
            return
        if self.path == "/api/status":
            self._send_json(session.status())
            return
        self.send_error(404)

    def do_POST(self):
        data = self._read_json()
        try:
            if self.path == "/api/screens":
                payload = session.screens()
            elif self.path == "/api/capture":
                payload = session.capture(data.get("screen_index"))
            elif self.path == "/api/crop":
                payload = session.crop(
                    data.get("x1"), data.get("y1"), data.get("x2"), data.get("y2"),
                    data.get("preview_width"), data.get("preview_height"),
                )
            elif self.path == "/api/upload":
                payload = session.add_data_url(data.get("data"), data.get("name") or "上傳的影像")
            elif self.path == "/api/remove":
                payload = session.remove(data.get("id"))
            elif self.path == "/api/clear":
                payload = session.clear()
            elif self.path == "/api/run":
                payload = self._run(data)
            else:
                self.send_error(404)
                return
        except VisionError as e:
            self._send_json({"error": str(e)}, status=400)
            return
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": f"伺服器處理失敗：{e}"}, status=500)
            return
        self._send_json(payload)

    def _run(self, data):
        prompt = (data.get("prompt") or "").strip()
        images = session.snapshot()
        if not images:
            raise VisionError("請先框選或加入至少一張圖片")
        if not prompt:
            raise VisionError("請輸入 Prompt")
        return {"result": analyze(images, prompt, model=MODEL_NAME)}


def main():
    host = os.environ.get("SCREEN_GEMMA_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("SCREEN_GEMMA_WEB_PORT", "8766"))
    server = ThreadingHTTPServer((host, port), SniperHandler)

    print("\n" + "=" * 50)
    print(f"🌐 Gemma4 Vision Web Sniper 已啟動: http://{host}:{port}（模型 {MODEL_NAME}）")
    print("按 Ctrl+C 停止伺服器。")
    print("=" * 50)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Bye")
        server.shutdown()


if __name__ == "__main__":
    main()
