/* vision/web/snip.js：螢幕擷取 + 全螢幕框選覆蓋層（web_console 與 sniper 共用）
 *
 * 用法：
 *   document.body.insertAdjacentHTML('beforeend', VisionSnip.markup());
 *   VisionSnip.init({
 *     apiBase: '/api/vision',            // 後端路由前綴：<apiBase>/screens、/capture、/upload
 *     onAdded: (item) => {...},          // 每框選一張：{id, thumbnail, count, width, height}
 *     onStatus: (text) => {...},         // 狀態文字
 *     onError: (text) => {...},          // 預設 alert
 *   });
 *   VisionSnip.capture();                // 由頁面上的按鈕（使用者手勢）呼叫
 *
 * 擷取方式自動選擇：
 *   1. 先問伺服器 /screens 有沒有截圖後端（WSL 的 PowerShell、或 Linux X11 桌面）。
 *      有 → 伺服器端截圖（多螢幕先跳選單），整張原始解析度的截圖送回瀏覽器當底圖。
 *   2. 沒有 → 用瀏覽器的 getDisplayMedia（分享畫面）讓使用者自己選螢幕，抓一幀當底圖。
 *      任何作業系統都可用，但頁面必須以 http://localhost 或 https 開啟（安全來源限制）。
 * 兩種方式之後的流程相同：底圖等比例（letterbox）鋪在覆蓋層上拖曳框選，裁切在瀏覽器端
 * 以原始解析度完成，PNG 直接 POST 到 /upload 加入清單，可連續框選，Esc 或「結束擷取」離開。
 */
window.VisionSnip = (() => {
  let cfg = { apiBase: '/api/vision', onAdded: () => {}, onStatus: () => {}, onError: (t) => alert(t) };
  let overlay, canvas, ctx, loading, countLabel, pickerBackdrop, pickerList;
  let base = null;   // 原始解析度的底圖（HTMLCanvasElement）
  let fit = null;    // 底圖在覆蓋層 canvas 上的顯示區域（letterbox）
  let dragging = false, startX = 0, startY = 0, addedThisSession = 0;

  // ---------- 幾何（純函式，另有 node 測試） ----------
  // 把 iw x ih 的底圖等比例置中塞進 cw x ch 的畫布
  function fitRect(iw, ih, cw, ch) {
    const s = Math.min(cw / iw, ch / ih);
    const w = iw * s, h = ih * s;
    return { s, w, h, x: (cw - w) / 2, y: (ch - h) / 2 };
  }
  // 畫布座標的選取矩形 → 底圖像素座標（整數、裁到底圖範圍內）
  function selectionToImageRect(x1, y1, x2, y2, f, iw, ih) {
    const ix = (v) => (v - f.x) / f.s, iy = (v) => (v - f.y) / f.s;
    const clampX = (v) => Math.max(0, Math.min(iw, v)), clampY = (v) => Math.max(0, Math.min(ih, v));
    const sx = clampX(Math.floor(Math.min(ix(x1), ix(x2)))), ex = clampX(Math.ceil(Math.max(ix(x1), ix(x2))));
    const sy = clampY(Math.floor(Math.min(iy(y1), iy(y2)))), ey = clampY(Math.ceil(Math.max(iy(y1), iy(y2))));
    return { sx, sy, sw: ex - sx, sh: ey - sy };
  }

  function canUseBrowserCapture() {
    return !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);
  }

  function markup() {
    return `
<div id="screen-picker-backdrop">
  <div id="screen-picker">
    <h2>選擇要擷取的螢幕</h2>
    <div id="screen-picker-list"></div>
    <button class="secondary" onclick="VisionSnip.closeScreenPicker()">取消</button>
  </div>
</div>
<div id="snip-overlay">
  <div id="snip-loading">擷取中...</div>
  <canvas id="snip-canvas"></canvas>
  <div id="snip-bar">
    <span id="snip-count-label">拖曳滑鼠選取要加入的區域</span>
    <button onclick="VisionSnip.exitSnipMode()">✕ 結束擷取（Esc）</button>
  </div>
</div>`;
  }

  async function postJSON(url, body) {
    const res = await fetch(url, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {})
    });
    return res.json();
  }

  function init(options) {
    cfg = Object.assign(cfg, options || {});
    overlay = document.getElementById('snip-overlay');
    canvas = document.getElementById('snip-canvas');
    ctx = canvas.getContext('2d');
    loading = document.getElementById('snip-loading');
    countLabel = document.getElementById('snip-count-label');
    pickerBackdrop = document.getElementById('screen-picker-backdrop');
    pickerList = document.getElementById('screen-picker-list');

    canvas.addEventListener('mousedown', (e) => {
      if (!base) return;
      const p = toCoords(e); startX = p.x; startY = p.y; dragging = true;
    });
    canvas.addEventListener('mousemove', (e) => {
      if (!dragging) return;
      const p = toCoords(e);
      redraw();
      ctx.strokeStyle = '#ff4d4f'; ctx.lineWidth = 2;
      ctx.strokeRect(startX, startY, p.x - startX, p.y - startY);
    });
    window.addEventListener('mouseup', onMouseUp);
    window.addEventListener('resize', () => { if (overlay.classList.contains('active')) resizeCanvas(); });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && overlay.classList.contains('active')) exitSnipMode();
    });
    // 使用者用瀏覽器原生方式（F11）離開全螢幕時，同步關閉覆蓋層
    document.addEventListener('fullscreenchange', () => {
      if (!document.fullscreenElement && overlay.classList.contains('active')) exitSnipMode();
    });
  }

  // ---------- 入口：決定擷取方式 ----------
  async function capture() {
    cfg.onStatus('偵測擷取方式...');
    let info = { screens: [], backend: null };
    try { info = await postJSON(cfg.apiBase + '/screens', {}); } catch (e) { /* 伺服器不可用，交給瀏覽器 */ }
    if (!info.backend) {
      if (canUseBrowserCapture()) return captureViaBrowser();
      cfg.onError('無法擷取畫面：伺服器端沒有截圖後端（需要 WSL 的 PowerShell 或 Linux X11 桌面），'
        + '瀏覽器端也不支援 getDisplayMedia（請以 http://localhost 或 https 開啟頁面）。請改用「選擇檔案」。');
      cfg.onStatus('無法擷取');
      return;
    }
    const screens = info.screens || [];
    if (screens.length > 1) { openScreenPicker(screens); return; }   // 多螢幕才需要問
    startCapture(screens.length === 1 ? screens[0].index : null);
  }

  // ---------- 瀏覽器端擷取（getDisplayMedia） ----------
  async function captureViaBrowser() {
    cfg.onStatus('請在瀏覽器跳出的視窗中選擇要擷取的螢幕或視窗...');
    let stream;
    try {
      // getDisplayMedia 必須在使用者手勢的有效期內呼叫；上面只有一次本機 fetch，仍在期限內
      stream = await navigator.mediaDevices.getDisplayMedia({
        video: { displaySurface: 'monitor' }, audio: false,
        selfBrowserSurface: 'exclude', surfaceSwitching: 'exclude', systemAudio: 'exclude',
      });
    } catch (e) {
      if (e && (e.name === 'NotAllowedError' || e.name === 'AbortError')) { cfg.onStatus('已取消擷取'); return; }
      cfg.onError('瀏覽器擷取失敗：' + ((e && e.message) || e)); cfg.onStatus('擷取失敗'); return;
    }
    try {
      const frame = await grabFrame(stream);
      showBase(frame, `已擷取（瀏覽器，${frame.width}x${frame.height}）`, true);
    } catch (e) {
      cfg.onError('讀取擷取畫面失敗：' + ((e && e.message) || e)); cfg.onStatus('擷取失敗');
    } finally {
      stream.getTracks().forEach(t => t.stop());   // 只要一幀，立刻停止分享
    }
  }

  // 從 MediaStream 抓一幀到 canvas（原始解析度）
  function grabFrame(stream) {
    return new Promise((resolve, reject) => {
      const video = document.createElement('video');
      video.muted = true; video.playsInline = true; video.srcObject = stream;
      video.style.cssText = 'position:fixed;left:-9999px;top:0;width:1px;height:1px;opacity:0;';
      document.body.appendChild(video);
      let settled = false;
      const finish = (fn, v) => { if (settled) return; settled = true; clearTimeout(timer); video.remove(); fn(v); };
      const timer = setTimeout(() => finish(reject, new Error('等待擷取影像逾時（8 秒）')), 8000);
      video.onloadeddata = () => {
        // 第一幀有時是黑的，多等一點再抓
        setTimeout(() => {
          try {
            const c = document.createElement('canvas');
            c.width = video.videoWidth; c.height = video.videoHeight;
            if (!c.width || !c.height) throw new Error('影像尺寸為 0');
            c.getContext('2d').drawImage(video, 0, 0);
            finish(resolve, c);
          } catch (e) { finish(reject, e); }
        }, 200);
      };
      video.onerror = () => finish(reject, new Error('video 無法播放擷取串流'));
      video.play().catch((e) => finish(reject, e));
    });
  }

  // ---------- 伺服器端擷取（PowerShell / X11） ----------
  function openScreenPicker(screens) {
    pickerList.innerHTML = '';
    screens.forEach(s => {
      const btn = document.createElement('button');
      btn.textContent = (s.primary ? '⭐ 主要螢幕' : '🖥️ 螢幕 ' + (s.index + 1)) + ` ${s.name} ${s.width}x${s.height}`;
      btn.onclick = () => { closeScreenPicker(); startCapture(s.index); };
      pickerList.appendChild(btn);
    });
    const allBtn = document.createElement('button');
    allBtn.textContent = '🖼️ 全部螢幕（整個虛擬桌面拼在一起）';
    allBtn.onclick = () => { closeScreenPicker(); startCapture(null); };
    pickerList.appendChild(allBtn);
    cfg.onStatus('請選擇要擷取的螢幕');
    pickerBackdrop.classList.add('active');
  }

  function closeScreenPicker() { pickerBackdrop.classList.remove('active'); }

  async function startCapture(screenIndex) {
    cfg.onStatus('擷取中...');
    enterOverlay(true);   // 先進全螢幕再等 API：requestFullscreen 要在使用者手勢的有效期內
    try {
      const data = await postJSON(cfg.apiBase + '/capture', { screen_index: screenIndex });
      if (data.error) { cfg.onError(data.error); exitSnipMode(); cfg.onStatus('擷取失敗'); return; }
      const img = new Image();
      img.onload = () => {
        const c = document.createElement('canvas');
        c.width = img.naturalWidth; c.height = img.naturalHeight;
        c.getContext('2d').drawImage(img, 0, 0);
        showBase(c, `已擷取（原始 ${data.orig_width}x${data.orig_height}）`, false);
      };
      img.onerror = () => { cfg.onError('無法載入擷取的畫面'); exitSnipMode(); cfg.onStatus('擷取失敗'); };
      img.src = data.image;
    } catch (e) {
      cfg.onError('擷取失敗：' + e); exitSnipMode(); cfg.onStatus('擷取失敗');
    }
  }

  // ---------- 覆蓋層 ----------
  function enterOverlay(requestFull) {
    addedThisSession = 0;
    countLabel.textContent = '拖曳滑鼠選取要加入的區域';
    loading.style.display = 'flex';
    overlay.classList.add('active');
    // 全螢幕只是加分：被瀏覽器拒絕（例如使用者手勢已過期）就維持鋪滿視窗的固定覆蓋層
    if (requestFull && overlay.requestFullscreen) overlay.requestFullscreen().catch(() => {});
    resizeCanvas();
  }

  function showBase(frameCanvas, statusText, tryFullscreen) {
    base = frameCanvas;
    if (!overlay.classList.contains('active')) enterOverlay(tryFullscreen);
    loading.style.display = 'none';
    resizeCanvas();
    cfg.onStatus(statusText);
  }

  function exitSnipMode() {
    overlay.classList.remove('active');
    base = null; fit = null;
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  }

  function resizeCanvas() { canvas.width = window.innerWidth; canvas.height = window.innerHeight; redraw(); }

  function redraw() {
    ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (!base) return;
    fit = fitRect(base.width, base.height, canvas.width, canvas.height);
    ctx.drawImage(base, fit.x, fit.y, fit.w, fit.h);
  }

  // canvas 內部像素尺寸與 CSS 顯示尺寸可能不同，把 clientX/Y 換算成內部像素座標
  function toCoords(e) {
    const rect = canvas.getBoundingClientRect();
    return { x: (e.clientX - rect.left) * (canvas.width / rect.width), y: (e.clientY - rect.top) * (canvas.height / rect.height) };
  }

  async function onMouseUp(e) {
    if (!dragging) return;
    dragging = false;
    if (!overlay.classList.contains('active') || !base || !fit) return;
    const p = toCoords(e);
    redraw();
    const r = selectionToImageRect(startX, startY, p.x, p.y, fit, base.width, base.height);
    if (r.sw < 5 || r.sh < 5) return;
    // 裁切在瀏覽器端以原始解析度完成，直接上傳 PNG
    const out = document.createElement('canvas');
    out.width = r.sw; out.height = r.sh;
    out.getContext('2d').drawImage(base, r.sx, r.sy, r.sw, r.sh, 0, 0, r.sw, r.sh);
    try {
      const data = await postJSON(cfg.apiBase + '/upload', { name: 'screen_crop.png', data: out.toDataURL('image/png') });
      if (data.error) { cfg.onError(data.error); return; }
      addedThisSession++;
      countLabel.textContent = `已加入 ${addedThisSession} 張，可繼續框選`;
      cfg.onAdded(data);
    } catch (err) { cfg.onError('加入裁切影像失敗：' + err); }
  }

  return { markup, init, capture, closeScreenPicker, exitSnipMode, postJSON, canUseBrowserCapture,
           _geom: { fitRect, selectionToImageRect } };
})();
