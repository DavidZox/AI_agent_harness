"""
Gemma4 Vision Web Sniper

screen_gemma4.py 的網頁版：一樣是「WSL 呼叫 PowerShell 截全螢幕 → 框選裁切
→ 丟給 Gemma4 vision 模型推論」的流程，但把原本 Tkinter 的裁切視窗換成
瀏覽器裡的 <canvas> 拖曳框選，不需要 WSL 的 X11/Wayland 環境（WSLg 或額外
裝 X server）就能用。

跟 screen_gemma4.py 的差異：
- 截圖仍然是同一套 WSL → PowerShell 的做法（capture_via_wsl_hybrid），
  這段完全沒有改動，一樣只能在 WSL 底下執行。
- 裁切互動從 Tkinter canvas 換成瀏覽器 <canvas> + 滑鼠拖曳，可以連續
  框選多次，每次放開滑鼠就自動裁切、加入右側清單，不需要每次都按
  「Add Crop」重新進入裁切模式。
- 純標準庫 http.server，沿用 web_console.py 同一套模式，不需要額外安裝
  Flask / FastAPI。

執行方式：
    python3 subagent/screen_gemma4_web.py
    然後瀏覽器打開 http://127.0.0.1:8766
"""

import base64
import io
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ollama
from PIL import Image

MODEL_NAME = os.environ.get("SCREEN_GEMMA_WEB_MODEL", "gemma4:e4b")
TEMP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fullscreen_capture.png")

# 傳給瀏覽器顯示用的縮圖上限（裁切仍然基於原始解析度的截圖，只有顯示縮小）
PREVIEW_MAX_DIM = 1600
THUMBNAIL_MAX_DIM = 240

# =========================================================
# 共用狀態（單一使用者、單一截圖工作階段）
# =========================================================

state_lock = threading.Lock()
state = {
    "full_screen_img": None,  # PIL.Image，原始解析度，裁切時都以此為準
    "snipped_images": [],     # list[PIL.Image]，已框選加入的裁切圖片
}


# =========================================================
# 螢幕擷取（跟 screen_gemma4.py 的 capture_via_wsl_hybrid 相同邏輯，
# 只差在回傳 (img, error_message) 讓 HTTP handler 能回報給瀏覽器，
# 而不是印在伺服器的終端機上看不到）
# =========================================================

def capture_via_wsl_hybrid():
    linux_file_path = TEMP_FILE

    if os.path.exists(linux_file_path):
        try:
            os.remove(linux_file_path)
        except Exception:
            pass

    try:
        result = subprocess.run(
            ['wslpath', '-w', linux_file_path],
            capture_output=True,
            text=True,
            check=True,
        )
        win_temp_path = result.stdout.strip()
    except Exception:
        wsl_distro = os.environ.get('WSL_DISTRO_NAME', 'Ubuntu')
        win_temp_path = (
            f"\\\\wsl.localhost\\{wsl_distro}{linux_file_path}"
        ).replace('/', '\\')

    ps_command = (
        "[Reflection.Assembly]::LoadWithPartialName('System.Drawing') | Out-Null; "
        "[Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
        "$type = Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();' -Name 'User32' -Namespace 'Win32' -PassThru; "
        "[Win32.User32]::SetProcessDPIAware() | Out-Null; "
        "$screen = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
        "$bmp = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height; "
        "$graphics = [System.Drawing.Graphics]::FromImage($bmp); "
        "$graphics.CopyFromScreen($screen.Left, $screen.Top, 0, 0, $bmp.Size); "
        f"$bmp.Save('{win_temp_path}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$graphics.Dispose(); $bmp.Dispose();"
    )

    try:
        subprocess.run(
            ["powershell.exe", "-Command", ps_command],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if os.path.exists(linux_file_path):
            img = Image.open(linux_file_path)
            return img.convert("RGB"), None
        return None, "PowerShell 執行完成，但找不到輸出檔案"
    except Exception as e:
        return None, str(e)


def _img_to_data_url(img, max_dim=None):
    """把 PIL.Image 編碼成瀏覽器可直接顯示的 base64 PNG data URL。
    max_dim 給定時會等比例縮小（裁切仍以傳入的原圖尺寸為準，這裡只是
    降低傳輸量與瀏覽器渲染負擔），回傳 (實際輸出寬, 高, data_url)。"""
    out = img
    if max_dim is not None and max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        out = img.resize(
            (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    buf = io.BytesIO()
    out.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return out.width, out.height, f"data:image/png;base64,{encoded}"


# =========================================================
# HTTP Server（純標準庫，跟 web_console.py 同一套模式）
# =========================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>Gemma4 Vision Web Sniper</title>
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
  main { flex: 1; display: flex; min-height: 0; overflow: hidden; }
  .panel { display: flex; flex-direction: column; min-width: 0; }
  #left-panel { flex: 2; border-right: 1px solid #3a3c40; padding: 12px; overflow: auto; }
  #right-panel { flex: 1; padding: 12px; overflow: auto; display: flex; flex-direction: column; gap: 10px; min-width: 280px; }
  .toolbar { display: flex; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
  button {
    background: #3a6df0; color: white; border: none; border-radius: 6px; padding: 8px 14px;
    font-size: 13px; cursor: pointer;
  }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.secondary { background: #4a4c50; }
  button.danger { background: #b0473f; }
  button.success { background: #28a745; }
  #canvas-wrap { display: inline-block; }
  #shot-canvas { border: 1px solid #3a3c40; cursor: crosshair; max-width: 100%; background: #101113; }
  #hint { font-size: 12px; color: #9aa0a6; margin-bottom: 8px; }
  h2 { font-size: 13px; margin: 0 0 8px 0; color: #c7c9cc; }
  #thumbs { display: flex; flex-wrap: wrap; gap: 8px; }
  #thumbs img {
    width: 72px; height: 54px; object-fit: cover; border-radius: 4px; border: 1px solid #3a3c40;
  }
  textarea#prompt {
    width: 100%; height: 90px; resize: vertical; background: #1e1f22; color: #e3e3e3;
    border: 1px solid #3a3c40; border-radius: 6px; padding: 8px; font-size: 13px; font-family: inherit;
  }
  #result {
    flex: 1; background: #26282c; border: 1px solid #3a3c40; border-radius: 6px;
    padding: 10px; font-size: 13px; white-space: pre-wrap; overflow-y: auto; min-height: 120px;
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
  <section id="left-panel" class="panel">
    <div class="toolbar">
      <button onclick="capture()">📸 擷取畫面</button>
      <button class="danger" onclick="clearImages()">🗑 清空已加入的圖片</button>
    </div>
    <div id="hint">擷取畫面後，直接在下方圖片上拖曳滑鼠選取要加入的區域，放開滑鼠就會自動裁切並加入右側清單，可以重複框選多次。</div>
    <div id="canvas-wrap">
      <canvas id="shot-canvas"></canvas>
    </div>
  </section>

  <section id="right-panel" class="panel">
    <div>
      <h2>已加入的裁切圖片</h2>
      <div id="thumbs"></div>
    </div>
    <div>
      <h2>Prompt</h2>
      <textarea id="prompt">解釋一下這張圖</textarea>
    </div>
    <button class="success" id="run-btn" onclick="runInference()">🤖 開始推論</button>
    <div style="flex:1; display:flex; flex-direction:column; min-height:0;">
      <h2>Gemma Result</h2>
      <div id="result">[ 尚無結果 ]</div>
    </div>
  </section>
</main>

<script>
const canvas = document.getElementById('shot-canvas');
const ctx = canvas.getContext('2d');
const thumbs = document.getElementById('thumbs');
const resultBox = document.getElementById('result');
const runBtn = document.getElementById('run-btn');
const statShot = document.getElementById('stat-shot');
const statCount = document.getElementById('stat-count');

let baseImage = null;   // 目前顯示在 canvas 上的 Image 物件（畫面截圖縮圖）
let dragging = false;
let startX = 0, startY = 0;

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  return res.json();
}

// canvas 的內部像素尺寸（canvas.width/height）跟它在畫面上實際顯示的
// CSS 尺寸可能不同（例如螢幕解析度太高、被 max-width:100% 縮小），
// 這裡把滑鼠的 clientX/Y 換算成 canvas 內部像素座標，裁切座標才會準確。
function toCanvasCoords(e) {
  const rect = canvas.getBoundingClientRect();
  const scaleX = canvas.width / rect.width;
  const scaleY = canvas.height / rect.height;
  return {
    x: (e.clientX - rect.left) * scaleX,
    y: (e.clientY - rect.top) * scaleY,
  };
}

async function capture() {
  statShot.textContent = '畫面：擷取中...';
  try {
    const data = await postJSON('/api/capture', {});
    if (data.error) {
      alert(data.error);
      statShot.textContent = '畫面：未擷取';
      return;
    }
    const img = new Image();
    img.onload = () => {
      canvas.width = data.preview_width;
      canvas.height = data.preview_height;
      baseImage = img;
      redraw();
      statShot.textContent = `畫面：已擷取（原始 ${data.orig_width}x${data.orig_height}）`;
    };
    img.src = data.image;
  } catch (e) {
    alert('擷取失敗：' + e);
    statShot.textContent = '畫面：未擷取';
  }
}

function redraw() {
  if (baseImage) {
    ctx.drawImage(baseImage, 0, 0, canvas.width, canvas.height);
  }
}

canvas.addEventListener('mousedown', (e) => {
  if (!baseImage) return;
  const p = toCanvasCoords(e);
  startX = p.x;
  startY = p.y;
  dragging = true;
});

canvas.addEventListener('mousemove', (e) => {
  if (!dragging) return;
  const p = toCanvasCoords(e);
  redraw();
  ctx.strokeStyle = '#ff4d4f';
  ctx.lineWidth = 2;
  ctx.strokeRect(startX, startY, p.x - startX, p.y - startY);
});

window.addEventListener('mouseup', async (e) => {
  if (!dragging) return;
  dragging = false;
  const p = toCanvasCoords(e);
  redraw();

  if (Math.abs(p.x - startX) < 5 || Math.abs(p.y - startY) < 5) return;

  try {
    const data = await postJSON('/api/crop', {
      x1: startX, y1: startY, x2: p.x, y2: p.y,
      preview_width: canvas.width, preview_height: canvas.height,
    });
    if (data.error) {
      alert(data.error);
      return;
    }
    addThumb(data.thumbnail);
    statCount.textContent = `已加入圖片：${data.count}`;
  } catch (err) {
    alert('裁切失敗：' + err);
  }
});

function addThumb(src) {
  const img = document.createElement('img');
  img.src = src;
  thumbs.appendChild(img);
}

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
    if (data.error) {
      resultBox.textContent = '錯誤：' + data.error;
      return;
    }
    resultBox.textContent = data.result;
  } catch (e) {
    resultBox.textContent = '錯誤：' + e;
  } finally {
    runBtn.disabled = false;
  }
}

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
            with state_lock:
                count = len(state["snipped_images"])
                has_shot = state["full_screen_img"] is not None
            self._send_json({"count": count, "has_screenshot": has_shot})
            return
        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/capture":
            self._handle_capture()
        elif self.path == "/api/crop":
            self._handle_crop()
        elif self.path == "/api/clear":
            self._handle_clear()
        elif self.path == "/api/run":
            self._handle_run()
        else:
            self.send_error(404)

    def _handle_capture(self):
        img, error = capture_via_wsl_hybrid()
        if img is None:
            self._send_json({"error": f"無法擷取畫面：{error}"}, status=500)
            return

        with state_lock:
            state["full_screen_img"] = img

        pw, ph, data_url = _img_to_data_url(img, max_dim=PREVIEW_MAX_DIM)
        self._send_json({
            "image": data_url,
            "preview_width": pw,
            "preview_height": ph,
            "orig_width": img.width,
            "orig_height": img.height,
        })

    def _handle_crop(self):
        data = self._read_json()

        with state_lock:
            img = state["full_screen_img"]

        if img is None:
            self._send_json({"error": "尚未擷取畫面，請先按「📸 擷取畫面」。"}, status=409)
            return

        try:
            x1, y1 = float(data["x1"]), float(data["y1"])
            x2, y2 = float(data["x2"]), float(data["y2"])
            preview_w, preview_h = float(data["preview_width"]), float(data["preview_height"])
        except (KeyError, TypeError, ValueError):
            self._send_json({"error": "裁切參數不完整"}, status=400)
            return

        if preview_w <= 0 or preview_h <= 0:
            self._send_json({"error": "裁切參數不合法"}, status=400)
            return

        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        if x2 - x1 < 5 or y2 - y1 < 5:
            self._send_json({"error": "選取範圍太小"}, status=400)
            return

        # 瀏覽器顯示的是縮圖（preview_width/height），裁切要換算回原始解析度
        scale_x = img.width / preview_w
        scale_y = img.height / preview_h
        box = (
            max(0, int(x1 * scale_x)),
            max(0, int(y1 * scale_y)),
            min(img.width, int(x2 * scale_x)),
            min(img.height, int(y2 * scale_y)),
        )
        cropped = img.crop(box)

        with state_lock:
            state["snipped_images"].append(cropped)
            count = len(state["snipped_images"])

        _, _, thumb_url = _img_to_data_url(cropped, max_dim=THUMBNAIL_MAX_DIM)
        self._send_json({"thumbnail": thumb_url, "count": count})

    def _handle_clear(self):
        with state_lock:
            state["snipped_images"].clear()
        self._send_json({"count": 0})

    def _handle_run(self):
        data = self._read_json()
        prompt = (data.get("prompt") or "").strip()

        with state_lock:
            images = list(state["snipped_images"])

        if not images:
            self._send_json({"error": "請先框選至少一張裁切圖片"}, status=400)
            return
        if not prompt:
            self._send_json({"error": "請輸入 Prompt"}, status=400)
            return

        images_bytes = []
        for img in images:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            images_bytes.append(buf.getvalue())

        try:
            response = ollama.chat(
                model=MODEL_NAME,
                messages=[{
                    'role': 'user',
                    'content': prompt,
                    'images': images_bytes,
                }],
                options={'temperature': 0.2, 'num_ctx': 12288},
                think=False,
            )
            result = response['message']['content']
        except Exception as e:
            self._send_json({"error": f"推論失敗：{e}"}, status=500)
            return

        self._send_json({"result": result})


def main():
    host = os.environ.get("SCREEN_GEMMA_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("SCREEN_GEMMA_WEB_PORT", "8766"))
    server = ThreadingHTTPServer((host, port), SniperHandler)

    print("\n" + "=" * 50)
    print(f"🌐 Gemma4 Vision Web Sniper 已啟動: http://{host}:{port}")
    print("按 Ctrl+C 停止伺服器。")
    print("=" * 50)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Bye")
        server.shutdown()


if __name__ == "__main__":
    main()
