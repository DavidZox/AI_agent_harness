"""vision：影像來源 → PIL.Image → 視覺模型推論 的共用 library。

模組分工：
- capture.py    螢幕擷取：自動偵測後端（WSL → PowerShell、Linux X11 → ImageGrab），都沒有時前端改用瀏覽器 getDisplayMedia
- images.py     任何來源統一成 PIL.Image：from_file / from_bytes / from_data_url、裁切、縮圖、編碼
- inference.py  analyze(images, prompt) → 文字；逾時與錯誤分類只維護這一份
- session.py    VisionSession：Web Console／sniper 共用的「擷取→框選→清單→取出」工作階段
- web/          前端共用資源（snip.js / snip.css）與靜態檔案服務

使用者：web_console.py（📷 附圖）、subagent/screen_gemma4_web.py（獨立 sniper）、
skills_system/scripts/image_inspect_cmd.py（Agent 對檔案路徑做圖像推論）。
新增影像來源時只要能產出 PIL.Image 或檔案路徑，呼叫 analyze() 即可。
"""
from .errors import VisionError
from .images import (
    MAX_IMAGE_BYTES, crop_by_preview, from_bytes, from_data_url, from_file, to_data_url, to_png_bytes,
)
from .inference import DEFAULT_MODEL, DEFAULT_TIMEOUT, SUBSESSION_SYSTEM_PROMPT, analyze
from .capture import backend as capture_backend, capture_screen, list_screens
from .session import VisionSession

__all__ = [
    "VisionError", "MAX_IMAGE_BYTES", "crop_by_preview", "from_bytes", "from_data_url", "from_file",
    "to_data_url", "to_png_bytes", "DEFAULT_MODEL", "DEFAULT_TIMEOUT", "SUBSESSION_SYSTEM_PROMPT",
    "analyze", "capture_backend", "capture_screen", "list_screens", "VisionSession",
]
