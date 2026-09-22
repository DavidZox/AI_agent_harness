"""視覺模型推論：把一或多張影像 + 提示詞交給 Ollama 的多模態模型，回傳文字。

這是整個 library 唯一會呼叫模型的地方。所有來源（Web Console 附圖、獨立 sniper、
image_inspect 技能、未來的相機／ROS 影像）都走 analyze()，逾時與錯誤分類只維護一份。
"""
import os

import ollama

from .errors import VisionError
from .images import to_png_bytes

try:  # ollama python client 底層用 httpx；用它的例外型別分類逾時／連線失敗
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

DEFAULT_MODEL = os.environ.get("VISION_MODEL", "gemma4:e4b")
DEFAULT_TIMEOUT = int(os.environ.get("VISION_TIMEOUT", "180"))   # 秒；CPU 推論多張圖可能需要一兩分鐘
DEFAULT_OPTIONS = {"temperature": 0.2, "num_ctx": 12288}

# Web Console 附圖時使用的 sub-session system prompt：主對話看不到原圖，
# 所以這裡除了回答問題，還要把影像中跟問題相關的事實一併寫出來，
# 讓主 Agent 能據此決定下一步（例如影像裡的錯誤訊息文字可以拿去 grep）。
SUBSESSION_SYSTEM_PROMPT = """你是視覺分析助手。使用者附上了影像並提出問題或指令。
另一個負責決策的 AI 看不到原圖，只會看到你的文字回覆，因此請：
1. 直接針對使用者的問題回答。
2. 逐字抄錄影像中與問題相關的文字、數值、錯誤訊息、狀態指示（不要改寫或翻譯）。
3. 若有多張影像，分別標明「影像 1」「影像 2」…。
4. 不確定的地方明說「看不清楚」或「無法判斷」，不要猜。
使用繁體中文回答，簡潔、條列。"""


def _to_bytes(item, index):
    if isinstance(item, (bytes, bytearray)):
        if not item:
            raise VisionError(f"第 {index} 張影像內容為空")
        return bytes(item)
    if hasattr(item, "save"):  # PIL.Image
        return to_png_bytes(item)
    raise VisionError(f"第 {index} 張影像型別不支援: {type(item).__name__}（需為 PIL.Image 或 bytes）")


def analyze(images, prompt, model=None, timeout=None, system_prompt=None, options=None):
    """對 images（PIL.Image 或 PNG/JPEG bytes 的列表）依 prompt 推論，回傳文字。
    失敗一律拋出 VisionError，訊息可直接顯示。"""
    model = model or DEFAULT_MODEL
    timeout = DEFAULT_TIMEOUT if timeout is None else timeout
    prompt = (prompt or "").strip()

    images = list(images or [])
    if not images:
        raise VisionError("沒有任何影像可供分析")
    if not prompt:
        raise VisionError("請提供提示詞（要對影像問什麼）")

    payload = [_to_bytes(img, i + 1) for i, img in enumerate(images)]
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt, "images": payload})

    try:
        client = ollama.Client(timeout=timeout)
        response = client.chat(
            model=model,
            messages=messages,
            options=options or DEFAULT_OPTIONS,
            think=False,
        )
    except Exception as e:  # 分類成可行動的說明
        if httpx is not None and isinstance(e, httpx.TimeoutException):
            raise VisionError(
                f"視覺推論逾時（超過 {timeout} 秒沒有回應）。影像張數多或解析度高時較慢，"
                f"可減少張數、先裁切出重點區域，或用 VISION_TIMEOUT 環境變數加大。"
            )
        if httpx is not None and isinstance(e, httpx.ConnectError):
            raise VisionError("無法連線到 Ollama 服務，請確認 `ollama serve` 正在執行")
        if isinstance(e, ollama.ResponseError):
            hint = f"（模型不存在，請先 `ollama pull {model}`）" if "not found" in str(e).lower() else ""
            raise VisionError(f"Ollama 回應錯誤: {e}{hint}")
        raise VisionError(f"視覺推論失敗: {e}")

    try:
        content = (response["message"]["content"] or "").strip()
    except (KeyError, TypeError):
        raise VisionError("Ollama 回傳的格式不如預期（缺少 message.content）")
    if not content:
        raise VisionError("模型沒有回傳任何內容（可能把回答放進 thinking 或影像無法解析）")
    return content
