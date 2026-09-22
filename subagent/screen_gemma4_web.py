"""
Gemma4 Vision Web Sniper

screen_gemma4.py 的網頁版：一樣是「WSL 呼叫 PowerShell 截全螢幕 → 框選裁切
→ 丟給 Gemma4 vision 模型推論」的流程。screen_gemma4.py 原本是用 Tkinter
開一個無邊框、置頂、鋪滿整個螢幕的 Toplevel 視窗顯示截圖，製造出「直接在
當下螢幕上拖曳選取」的錯覺；這個網頁版改用瀏覽器的 Fullscreen API +
<canvas> 重現同樣的效果，不需要 WSL 的 X11/Wayland 環境（WSLg 或額外裝
X server）就能用。

跟 screen_gemma4.py 的差異：
- 截圖的核心做法仍然是同一套 WSL → PowerShell（capture_via_wsl_hybrid），
  只是多接受一個 bounds 參數指定只截哪一塊區域，一樣只能在 WSL 底下執行。
- 按下「擷取畫面」時，如果偵測到多台螢幕（例如接了外接螢幕），會先跳出
  一個小選單讓你挑要擷取哪一台（list_screens，靠 PowerShell 的
  Screen.AllScreens 列出來）；只有單一螢幕時直接跳過選單、照舊一鍵擷取，
  筆電單獨帶出門、外接雙螢幕兩種情境都不需要另外設定。
- 選好螢幕（或只有一台不用選）後立刻進入全螢幕框選模式（盡量用瀏覽器的
  Fullscreen API 進去，若瀏覽器不支援則退回鋪滿視窗的固定覆蓋層），畫面
  上顯示的就是剛才擷取到的那台螢幕內容，感覺就像直接在自己當下的螢幕上
  拖曳選取，而不是在頁面裡一個縮小的預覽圖上操作。
- 可以連續拖曳框選多次，每次放開滑鼠就自動裁切、加入清單，不需要每次
  都重新點擊「擷取畫面」；按 Esc 或畫面上的「結束擷取」離開框選模式。
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

# 縮圖上限（裁切仍以原始解析度的截圖為準，這裡只是右側清單的顯示縮小）
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

def list_screens():
    """透過 PowerShell 的 Screen.AllScreens 列出目前所有螢幕，讓使用者
    擷取畫面前可以挑要哪一台（筆電單獨帶出門是單螢幕、接了外接螢幕變成
    雙螢幕，兩種情境都要能用）。回傳 (screens, error_message)，screens
    是 list[dict]：index/name/x/y/width/height/primary；失敗時
    screens 為 None。"""
    ps_command = (
        "[Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
        "$i = 0; "
        "foreach ($s in [System.Windows.Forms.Screen]::AllScreens) { "
        "Write-Output \"$i|$($s.DeviceName)|$($s.Bounds.X)|$($s.Bounds.Y)|$($s.Bounds.Width)|$($s.Bounds.Height)|$($s.Primary)\"; "
        "$i++; "
        "}"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-Command", ps_command],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception as e:
        return None, str(e)

    screens = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 7:
            continue
        idx, name, x, y, w, h, primary = parts
        try:
            screens.append({
                "index": int(idx),
                "name": name,
                "x": int(x),
                "y": int(y),
                "width": int(w),
                "height": int(h),
                "primary": primary.strip().lower() == "true",
            })
        except ValueError:
            continue

    if not screens:
        return None, "找不到任何螢幕（PowerShell 輸出無法解析）"
    return screens, None


def capture_via_wsl_hybrid(bounds=None):
    """bounds 給定時是 (x, y, width, height)，只擷取該範圍（單一螢幕，
    座標來自 list_screens()，可能是負數——次要螢幕擺在主螢幕左邊/上面時
    很常見）；不給時退回舊行為，擷取整個虛擬桌面（所有螢幕拼在一起）。"""
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

    # 用四個獨立變數（賦值語法）而不是建構 Rectangle 物件並依位置傳入
    # 座標，是為了避免 PowerShell 把負數的 X/Y（次要螢幕在主螢幕左邊或
    # 上面時很常見）誤判成參數旗標。
    if bounds:
        x, y, w, h = bounds
        screen_vars = f"$captureX = {x}; $captureY = {y}; $captureW = {w}; $captureH = {h}; "
    else:
        screen_vars = (
            "$__vs = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$captureX = $__vs.Left; $captureY = $__vs.Top; "
            "$captureW = $__vs.Width; $captureH = $__vs.Height; "
        )

    ps_command = (
        "[Reflection.Assembly]::LoadWithPartialName('System.Drawing') | Out-Null; "
        "[Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
        "$type = Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();' -Name 'User32' -Namespace 'Win32' -PassThru; "
        "[Win32.User32]::SetProcessDPIAware() | Out-Null; "
        f"{screen_vars}"
        "$bmp = New-Object System.Drawing.Bitmap $captureW, $captureH; "
        "$graphics = [System.Drawing.Graphics]::FromImage($bmp); "
        "$graphics.CopyFromScreen($captureX, $captureY, 0, 0, $bmp.Size); "
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
    max_dim 給定時會等比例縮小，回傳 (實際輸出寬, 高, data_url)。不給
    max_dim 時原始解析度直接輸出——全螢幕框選畫面需要盡量清晰，且僅在
    本機（127.0.0.1）傳輸，不必為了省頻寬犧牲清晰度。"""
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
  #thumbs img {
    width: 96px; height: 72px; object-fit: cover; border-radius: 4px; border: 1px solid #3a3c40;
  }
  textarea#prompt {
    width: 100%; height: 90px; resize: vertical; background: #1e1f22; color: #e3e3e3;
    border: 1px solid #3a3c40; border-radius: 6px; padding: 8px; font-size: 13px; font-family: inherit;
  }
  #result {
    flex: 1; background: #26282c; border: 1px solid #3a3c40; border-radius: 6px;
    padding: 10px; font-size: 13px; white-space: pre-wrap; overflow-y: auto; min-height: 160px;
  }

  /* ===== 選擇螢幕的小視窗：偵測到多台螢幕時，擷取前先讓使用者挑 ===== */
  #screen-picker-backdrop {
    display: none;
    position: fixed; inset: 0; z-index: 8000;
    background: rgba(0,0,0,0.6);
    align-items: center; justify-content: center;
  }
  #screen-picker-backdrop.active { display: flex; }
  #screen-picker {
    background: #26282c; border: 1px solid #3a3c40; border-radius: 10px;
    padding: 20px; min-width: 280px; display: flex; flex-direction: column; gap: 10px;
  }
  #screen-picker h2 { margin: 0 0 4px 0; font-size: 14px; }
  #screen-picker-list { display: flex; flex-direction: column; gap: 8px; }
  #screen-picker-list button { text-align: left; background: #34363b; }
  #screen-picker-list button:hover { background: #3a6df0; }

  /* ===== 全螢幕框選覆蓋層：製造「直接在當下螢幕上拖曳」的錯覺 ===== */
  #snip-overlay {
    display: none;
    position: fixed; inset: 0; z-index: 9999;
    background: #000; user-select: none;
  }
  #snip-overlay.active { display: block; }
  #snip-canvas { display: block; width: 100vw; height: 100vh; cursor: crosshair; }
  #snip-loading {
    position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
    color: #9aa0a6; font-size: 16px; pointer-events: none;
  }
  #snip-bar {
    position: fixed; top: 16px; left: 50%; transform: translateX(-50%);
    background: rgba(30,31,34,0.92); color: #e3e3e3; padding: 8px 16px;
    border-radius: 8px; font-size: 13px; display: flex; gap: 12px; align-items: center;
    box-shadow: 0 2px 10px rgba(0,0,0,0.4);
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
      <button onclick="capture()">📸 擷取畫面</button>
      <button class="danger" onclick="clearImages()">🗑 清空已加入的圖片</button>
    </div>
    <div id="hint">按下「📸 擷取畫面」時，如果偵測到多台螢幕會先讓你選要擷取哪一台（單螢幕時直接略過這步）；選好後立刻進入全螢幕框選模式：直接在畫面上拖曳滑鼠選取要加入的區域，放開滑鼠就會自動裁切並加入下面的清單，可以連續框選多次。選完後按 Esc 或畫面上方的「結束擷取」離開。</div>

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

<div id="screen-picker-backdrop">
  <div id="screen-picker">
    <h2>選擇要擷取的螢幕</h2>
    <div id="screen-picker-list"></div>
    <button class="secondary" onclick="closeScreenPicker()">取消</button>
  </div>
</div>

<div id="snip-overlay">
  <div id="snip-loading">擷取中...</div>
  <canvas id="snip-canvas"></canvas>
  <div id="snip-bar">
    <span id="snip-count-label">拖曳滑鼠選取要加入的區域</span>
    <button class="secondary" onclick="exitSnipMode()">✕ 結束擷取（Esc）</button>
  </div>
</div>

<script>
const thumbs = document.getElementById('thumbs');
const resultBox = document.getElementById('result');
const runBtn = document.getElementById('run-btn');
const statShot = document.getElementById('stat-shot');
const statCount = document.getElementById('stat-count');

const snipOverlay = document.getElementById('snip-overlay');
const snipCanvas = document.getElementById('snip-canvas');
const snipCtx = snipCanvas.getContext('2d');
const snipLoading = document.getElementById('snip-loading');
const snipCountLabel = document.getElementById('snip-count-label');

const screenPickerBackdrop = document.getElementById('screen-picker-backdrop');
const screenPickerList = document.getElementById('screen-picker-list');

let snipBaseImage = null;   // 全螢幕覆蓋層上顯示的截圖
let snipDragging = false;
let snipStartX = 0, snipStartY = 0;
let addedThisSession = 0;

async function postJSON(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  return res.json();
}

async function capture() {
  statShot.textContent = '偵測螢幕中...';
  let screens = [];
  try {
    const data = await postJSON('/api/screens', {});
    screens = data.screens || [];
  } catch (e) {
    screens = [];
  }

  if (screens.length > 1) {
    // 有多台螢幕（例如接了外接螢幕）才需要問，單螢幕直接略過這步
    openScreenPicker(screens);
    return;
  }

  startCapture(screens.length === 1 ? screens[0].index : null);
}

function openScreenPicker(screens) {
  screenPickerList.innerHTML = '';
  screens.forEach(s => {
    const btn = document.createElement('button');
    const label = (s.primary ? '⭐ 主要螢幕' : '🖥️ 螢幕 ' + (s.index + 1)) + ` ${s.width}x${s.height}`;
    btn.textContent = label;
    btn.onclick = () => {
      closeScreenPicker();
      startCapture(s.index);
    };
    screenPickerList.appendChild(btn);
  });
  const allBtn = document.createElement('button');
  allBtn.textContent = '🖼️ 全部螢幕（整個虛擬桌面拼在一起）';
  allBtn.onclick = () => {
    closeScreenPicker();
    startCapture(null);
  };
  screenPickerList.appendChild(allBtn);
  statShot.textContent = '請選擇要擷取的螢幕';
  screenPickerBackdrop.classList.add('active');
}

function closeScreenPicker() {
  screenPickerBackdrop.classList.remove('active');
}

async function startCapture(screenIndex) {
  statShot.textContent = '擷取中...';
  // 先進全螢幕覆蓋層，再等 API 回應：Fullscreen API 只有在使用者操作的
  // 呼叫堆疊內才保證可用，等 fetch 回應太久可能會被瀏覽器判定不是使用者
  // 手勢觸發而擋掉 requestFullscreen()，所以要在 await 之前先呼叫。
  enterSnipOverlay();
  try {
    const data = await postJSON('/api/capture', { screen_index: screenIndex });
    if (data.error) {
      alert(data.error);
      exitSnipMode();
      statShot.textContent = '擷取失敗';
      return;
    }
    const img = new Image();
    img.onload = () => {
      snipBaseImage = img;
      snipLoading.style.display = 'none';
      resizeSnipCanvas();
    };
    img.src = data.image;
    statShot.textContent = `畫面：已擷取（原始 ${data.orig_width}x${data.orig_height}）`;
  } catch (e) {
    alert('擷取失敗：' + e);
    exitSnipMode();
    statShot.textContent = '擷取失敗';
  }
}

function enterSnipOverlay() {
  addedThisSession = 0;
  snipCountLabel.textContent = '拖曳滑鼠選取要加入的區域';
  snipLoading.style.display = 'flex';
  snipOverlay.classList.add('active');
  if (snipOverlay.requestFullscreen) {
    snipOverlay.requestFullscreen().catch(() => {});
  }
  resizeSnipCanvas();
}

function exitSnipMode() {
  snipOverlay.classList.remove('active');
  snipBaseImage = null;
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  }
}

function resizeSnipCanvas() {
  snipCanvas.width = window.innerWidth;
  snipCanvas.height = window.innerHeight;
  redrawSnip();
}

function redrawSnip() {
  if (snipBaseImage) {
    snipCtx.drawImage(snipBaseImage, 0, 0, snipCanvas.width, snipCanvas.height);
  }
}

// canvas 的內部像素尺寸（canvas.width/height）跟它在畫面上實際顯示的
// CSS 尺寸可能不同，這裡把滑鼠的 clientX/Y 換算成 canvas 內部像素座標，
// 裁切座標才會準確。
function toSnipCoords(e) {
  const rect = snipCanvas.getBoundingClientRect();
  const scaleX = snipCanvas.width / rect.width;
  const scaleY = snipCanvas.height / rect.height;
  return {
    x: (e.clientX - rect.left) * scaleX,
    y: (e.clientY - rect.top) * scaleY,
  };
}

snipCanvas.addEventListener('mousedown', (e) => {
  if (!snipBaseImage) return;
  const p = toSnipCoords(e);
  snipStartX = p.x;
  snipStartY = p.y;
  snipDragging = true;
});

snipCanvas.addEventListener('mousemove', (e) => {
  if (!snipDragging) return;
  const p = toSnipCoords(e);
  redrawSnip();
  snipCtx.strokeStyle = '#ff4d4f';
  snipCtx.lineWidth = 2;
  snipCtx.strokeRect(snipStartX, snipStartY, p.x - snipStartX, p.y - snipStartY);
});

window.addEventListener('mouseup', async (e) => {
  if (!snipDragging) return;
  snipDragging = false;
  if (!snipOverlay.classList.contains('active')) return;
  const p = toSnipCoords(e);
  redrawSnip();

  if (Math.abs(p.x - snipStartX) < 5 || Math.abs(p.y - snipStartY) < 5) return;

  try {
    const data = await postJSON('/api/crop', {
      x1: snipStartX, y1: snipStartY, x2: p.x, y2: p.y,
      preview_width: snipCanvas.width, preview_height: snipCanvas.height,
    });
    if (data.error) {
      alert(data.error);
      return;
    }
    addThumb(data.thumbnail);
    addedThisSession++;
    statCount.textContent = `已加入圖片：${data.count}`;
    snipCountLabel.textContent = `已加入 ${addedThisSession} 張，可繼續框選`;
  } catch (err) {
    alert('裁切失敗：' + err);
  }
});

window.addEventListener('resize', () => {
  if (!snipOverlay.classList.contains('active')) return;
  resizeSnipCanvas();
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && snipOverlay.classList.contains('active')) {
    exitSnipMode();
  }
});

// 使用者用瀏覽器原生方式（例如 F11）離開全螢幕時，也要同步關閉覆蓋層
document.addEventListener('fullscreenchange', () => {
  if (!document.fullscreenElement && snipOverlay.classList.contains('active')) {
    exitSnipMode();
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
        elif self.path == "/api/screens":
            self._handle_screens()
        elif self.path == "/api/crop":
            self._handle_crop()
        elif self.path == "/api/clear":
            self._handle_clear()
        elif self.path == "/api/run":
            self._handle_run()
        else:
            self.send_error(404)

    def _handle_screens(self):
        """列出目前所有螢幕，給前端在擷取前顯示選單用（見 list_screens）。
        偵測失敗時不當成 HTTP 錯誤擋住流程，回傳空清單就好，前端會自動
        退回「不選、直接擷取整個虛擬桌面」的行為。"""
        screens, error = list_screens()
        if screens is None:
            self._send_json({"screens": [], "error": error})
            return
        self._send_json({"screens": screens})

    def _handle_capture(self):
        """擷取畫面，回傳原始解析度的圖片給前端顯示成全螢幕框選畫面。
        不會自動加入清單——使用者在全螢幕畫面上拖曳框選出來的區域，才會
        透過 /api/crop 加入清單（見 _handle_crop）。

        body 可帶 screen_index（來自 /api/screens 回傳的 index）指定只
        擷取哪一台螢幕；不帶、或找不到對應螢幕時，退回擷取整個虛擬桌面
        （所有螢幕拼在一起，維持單螢幕環境下的舊行為）。這裡重新查一次
        list_screens() 取得座標，而不是直接信任前端傳回來的座標，避免
        選單開著時螢幕排列剛好被使用者調整過而拿到過期座標。"""
        data = self._read_json()
        screen_index = data.get("screen_index")

        bounds = None
        if screen_index is not None:
            screens, _ = list_screens()
            match = next((s for s in (screens or []) if s["index"] == screen_index), None)
            if match:
                bounds = (match["x"], match["y"], match["width"], match["height"])

        img, error = capture_via_wsl_hybrid(bounds=bounds)
        if img is None:
            self._send_json({"error": f"無法擷取畫面：{error}"}, status=500)
            return

        with state_lock:
            state["full_screen_img"] = img

        _, _, image_url = _img_to_data_url(img)
        self._send_json({
            "image": image_url,
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

        # 瀏覽器全螢幕覆蓋層的 canvas 尺寸（preview_width/height）不一定
        # 等於原始截圖解析度（例如瀏覽器縮放、多螢幕虛擬桌面），裁切前要
        # 換算回原始解析度的座標。
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
