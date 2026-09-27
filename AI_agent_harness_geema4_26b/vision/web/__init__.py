"""前端共用資源（框選 overlay + 螢幕選單）的靜態檔案服務。

web_console.py 與 subagent/screen_gemma4_web.py 都用 GET /static/vision/<name>
把這裡的檔案送給瀏覽器，前端的框選邏輯只維護一份。"""
import os

_DIR = os.path.dirname(os.path.abspath(__file__))
_FILES = {
    "snip.js": "application/javascript; charset=utf-8",
    "snip.css": "text/css; charset=utf-8",
}
STATIC_PREFIX = "/static/vision/"


def static_file(path):
    """path 為完整 URL path（如 /static/vision/snip.js）。回傳 (bytes, content_type) 或 None。"""
    if not path.startswith(STATIC_PREFIX):
        return None
    name = path[len(STATIC_PREFIX):]
    if name not in _FILES:
        return None
    with open(os.path.join(_DIR, name), "rb") as f:
        return f.read(), _FILES[name]
