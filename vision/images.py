"""影像來源統一成 PIL.Image，以及編碼／裁切等純影像處理。

所有來源（螢幕擷取、瀏覽器上傳的檔案、本機路徑、未來的相機快照或 ROS image
topic）都先轉成 RGB 的 PIL.Image，之後的裁切、縮圖、推論就只認這一種型別。
"""
import base64
import io
import os

from PIL import Image, UnidentifiedImageError

from .errors import VisionError

MAX_IMAGE_BYTES = 20 * 1024 * 1024   # 單張影像上限，避免一張巨圖把記憶體或模型撐爆
SUPPORTED_HINT = "PNG / JPEG / WebP / BMP / GIF"


def _open(data_or_path, what):
    try:
        img = Image.open(data_or_path)
        img.load()
    except UnidentifiedImageError:
        raise VisionError(f"{what} 不是可辨識的影像格式（支援 {SUPPORTED_HINT}）")
    except OSError as e:
        raise VisionError(f"讀取{what}失敗: {e}")
    return img.convert("RGB")


def from_file(path):
    """讀取本機影像檔。路徑相對於目前工作目錄；~ 會展開。"""
    if not path or not str(path).strip():
        raise VisionError("請提供影像檔路徑")
    abs_path = os.path.abspath(os.path.expanduser(str(path).strip()))
    if not os.path.exists(abs_path):
        raise VisionError(f"影像檔不存在: {path}（解析為 {abs_path}）")
    if os.path.isdir(abs_path):
        raise VisionError(f"'{path}' 是目錄，不是影像檔")
    if not os.path.isfile(abs_path):
        raise VisionError(f"'{path}' 不是一般檔案")
    size = os.path.getsize(abs_path)
    if size > MAX_IMAGE_BYTES:
        raise VisionError(f"影像檔 {size} bytes 超過上限 {MAX_IMAGE_BYTES} bytes")
    return _open(abs_path, f"影像檔 '{path}'")


def from_bytes(data, name="影像"):
    """從原始 bytes（例如瀏覽器上傳的檔案內容）建立影像。"""
    if not data:
        raise VisionError(f"{name} 內容為空")
    if len(data) > MAX_IMAGE_BYTES:
        raise VisionError(f"{name} {len(data)} bytes 超過上限 {MAX_IMAGE_BYTES} bytes")
    return _open(io.BytesIO(data), name)


def from_data_url(url, name="影像"):
    """從 data:image/...;base64,.... 建立影像（瀏覧器 FileReader.readAsDataURL 的格式）。"""
    if not url or "," not in url or not url.startswith("data:"):
        raise VisionError(f"{name} 不是合法的 data URL")
    try:
        raw = base64.b64decode(url.split(",", 1)[1], validate=False)
    except Exception as e:
        raise VisionError(f"{name} 的 base64 內容無法解碼: {e}")
    return from_bytes(raw, name)


def to_png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def to_data_url(img, max_dim=None):
    """編碼成瀏覽器可直接顯示的 base64 PNG data URL；max_dim 給定時等比縮小。
    回傳 (實際輸出寬, 高, data_url)。"""
    out = img
    if max_dim is not None and max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        out = img.resize(
            (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
            Image.Resampling.LANCZOS,
        )
    encoded = base64.b64encode(to_png_bytes(out)).decode("ascii")
    return out.width, out.height, f"data:image/png;base64,{encoded}"


def crop_by_preview(img, x1, y1, x2, y2, preview_w, preview_h, min_size=5):
    """依「預覽畫面上的座標」裁切原始解析度影像。

    瀏覽器全螢幕覆蓋層的 canvas 尺寸（preview_w/h）不一定等於原始截圖解析度
    （瀏覽器縮放、多螢幕虛擬桌面），所以先換算回原始座標再裁切。"""
    try:
        x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
        preview_w, preview_h = float(preview_w), float(preview_h)
    except (TypeError, ValueError):
        raise VisionError("裁切參數不完整或不是數字")
    if preview_w <= 0 or preview_h <= 0:
        raise VisionError("裁切參數不合法（預覽尺寸必須大於 0）")
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    if x2 - x1 < min_size or y2 - y1 < min_size:
        raise VisionError("選取範圍太小")
    scale_x = img.width / preview_w
    scale_y = img.height / preview_h
    box = (
        max(0, int(x1 * scale_x)),
        max(0, int(y1 * scale_y)),
        min(img.width, int(x2 * scale_x)),
        min(img.height, int(y2 * scale_y)),
    )
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        raise VisionError("選取範圍換算後為空")
    return img.crop(box)
