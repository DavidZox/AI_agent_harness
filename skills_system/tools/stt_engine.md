---
type: Tool
title: 語音輸入引擎
description: 使用者需要進行語音輸入時使用
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
透過 Windows 端 `ffmpeg.exe`（經 WSL 的 `/mnt/c/` 路徑呼叫）錄製 10 秒音訊，再以本機 `faster_whisper`（`small` 模型，CPU/int8）進行語音辨識，將結果轉為文字回傳並寫入 `stt_output.txt`。**高度綁定此特定機器的環境**（固定麥克風裝置名稱、固定 Windows 暫存路徑），非泛用工具。

# 語法 / 參數規範
* 不接受任何參數（呼叫時忽略所有輸入）。

# 執行步驟 (Steps)
1. 呼叫 Windows 端 `ffmpeg.exe` 錄音 10 秒，存至 `C:\temp\input.wav`。
2. 載入 `faster_whisper` 模型並轉錄音檔。
3. 將辨識文字印出並寫入 `stt_output.txt`。

# 範例 (Examples)
* `EXECUTE: stt_engine_cmd.py`

# 異常處理 (Edge Cases)
* 錄音裝置名稱寫死於腳本中，若更換麥克風或作業系統環境，需直接修改腳本內的 `MIC_NAME`。
* 若找不到 `/mnt/c/ffmpeg/bin/ffmpeg.exe` 或 `faster_whisper` 套件未安裝，將直接拋出例外並以非 0 狀態碼結束。
* 找不到錄音檔時回傳 `FileNotFoundError`：`找不到音訊檔案: ...，錄音可能未成功。`。
