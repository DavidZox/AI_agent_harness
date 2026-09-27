"""單一使用者的影像工作階段：目前的全螢幕截圖 + 已加入的影像清單（thread-safe）。

Web Console 與獨立 sniper 的 HTTP handler 都只是把 JSON 轉成這裡的方法呼叫，
所以「螢幕選單 → 擷取 → 框選裁切 → 加入清單 → 移除／清空 → 取出推論」這整套
邏輯只維護一份。影像來源不限於螢幕：add_image() 接受任何 PIL.Image。
"""
import itertools
import threading

from . import capture as _capture
from .errors import VisionError
from .images import crop_by_preview, from_data_url, to_data_url

THUMBNAIL_MAX_DIM = 240


class VisionSession:
    def __init__(self, capture_fn=None, list_screens_fn=None, backend_fn=None):
        # 可注入，方便在沒有桌面環境的機器上測試，或未來換成別的擷取來源
        self._capture = capture_fn or _capture.capture_screen
        self._list_screens = list_screens_fn or _capture.list_screens
        self._backend = backend_fn or _capture.backend
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.full_screen_img = None
        self.items = []   # list[dict]: {"id": int, "img": PIL.Image, "source": str}

    # ---- 查詢 ----
    def count(self):
        with self._lock:
            return len(self.items)

    def status(self):
        with self._lock:
            return {"count": len(self.items), "has_screenshot": self.full_screen_img is not None}

    def screens(self):
        """回傳 {"screens": [...], "backend": "powershell"|"x11"|None[, "error": ...]}。
        backend 為 None 代表伺服器端無法擷取，前端會改用瀏覽器的 getDisplayMedia；
        有後端但列螢幕失敗不視為錯誤：回空清單，前端會退回擷取整個虛擬桌面。"""
        b = self._backend()
        if b is None:
            return {"screens": [], "backend": None, "error": _capture.NO_BACKEND_MESSAGE}
        screens, error = self._list_screens()
        if screens is None:
            return {"screens": [], "backend": b, "error": error}
        return {"screens": screens, "backend": b}

    # ---- 螢幕擷取／框選 ----
    def capture(self, screen_index=None):
        """擷取畫面並記住原始解析度影像，回傳給前端當全螢幕框選底圖。
        重新查一次 list_screens 取座標，不信任前端傳回的過期座標。"""
        bounds = None
        if screen_index is not None:
            screens, _ = self._list_screens()
            match = next((s for s in (screens or []) if s["index"] == screen_index), None)
            if match:
                bounds = (match["x"], match["y"], match["width"], match["height"])
        img, error = self._capture(bounds=bounds)
        if img is None:
            raise VisionError(f"無法擷取畫面：{error}")
        with self._lock:
            self.full_screen_img = img
        _, _, url = to_data_url(img)
        return {"image": url, "orig_width": img.width, "orig_height": img.height}

    def crop(self, x1, y1, x2, y2, preview_width, preview_height):
        with self._lock:
            base = self.full_screen_img
        if base is None:
            raise VisionError("尚未擷取畫面，請先擷取")
        cropped = crop_by_preview(base, x1, y1, x2, y2, preview_width, preview_height)
        return self.add_image(cropped, source="screen")

    # ---- 任何來源 ----
    def add_image(self, img, source="image"):
        with self._lock:
            item = {"id": next(self._ids), "img": img, "source": source}
            self.items.append(item)
            count = len(self.items)
        _, _, thumb = to_data_url(img, max_dim=THUMBNAIL_MAX_DIM)
        return {"id": item["id"], "thumbnail": thumb, "count": count, "width": img.width, "height": img.height}

    def add_data_url(self, data_url, name="上傳的影像"):
        return self.add_image(from_data_url(data_url, name), source=f"file:{name}")

    def remove(self, item_id):
        with self._lock:
            before = len(self.items)
            self.items = [it for it in self.items if it["id"] != item_id]
            return {"count": len(self.items), "removed": len(self.items) != before}

    def clear(self):
        with self._lock:
            self.items.clear()
        return {"count": 0}

    def snapshot(self):
        """複製目前的影像清單（不清空）。"""
        with self._lock:
            return [it["img"] for it in self.items]

    def take_all(self):
        """取出全部影像並清空清單（Web Console 送出訊息時一次消費）。"""
        with self._lock:
            imgs = [it["img"] for it in self.items]
            self.items = []
            return imgs
