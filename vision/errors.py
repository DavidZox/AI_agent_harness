class VisionError(Exception):
    """vision library 的所有可預期錯誤（檔案不存在、無法解碼、推論逾時、Ollama 連不上…）。

    訊息一律是可直接顯示給使用者／Agent 的中文說明；呼叫端依自己的慣例包裝
    （腳本印成 `[ERROR] ...`、HTTP handler 轉成 JSON error）。"""
