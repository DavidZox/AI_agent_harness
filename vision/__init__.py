"""vision：影像來源 → PIL.Image → 視覺模型推論 的共用 library。

模組分工：
- capture.py    螢幕擷取：自動偵測後端（WSL → PowerShell、Linux X11 → ImageGrab），都沒有時前端改用瀏覽器 getDisplayMedia
- images.py     任何來源統一成 PIL.Image：from_file / from_bytes / from_data_url、裁切、縮圖、編碼
- inference.py  analyze(images, prompt) → 文字；逾時與錯誤分類只維護這一份
- extraction.py extract(images, user_text) → 給決策 AI 用的結構化提取（文字 → 畫面 → 個體 → 背景、線索、重點）；
                Web Console 附圖用它，模型呼叫仍走 analyze()
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
from .inference import DEFAULT_MODEL, DEFAULT_TIMEOUT, analyze
from .extraction import EXTRACTION_SYSTEM_PROMPT, describe_sources, extract, extraction_schema, render_extraction
from .capture import backend as capture_backend, capture_screen, list_screens
from .session import VisionSession

__all__ = [
    "VisionError", "MAX_IMAGE_BYTES", "crop_by_preview", "from_bytes", "from_data_url", "from_file",
    "to_data_url", "to_png_bytes", "DEFAULT_MODEL", "DEFAULT_TIMEOUT", "analyze",
    "EXTRACTION_SYSTEM_PROMPT", "describe_sources", "extract", "extraction_schema", "render_extraction",
    "capture_backend", "capture_screen", "list_screens", "VisionSession",
]
