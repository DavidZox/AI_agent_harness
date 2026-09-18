---
type: Tool
title: 語音輸入引擎
description: 使用者需要進行語音輸入時使用。
version: 1.0.0
dependencies: ["faster_whisper", "ffmpeg (Windows)"]
---

# 背景 / 運作原理
透過 Windows 端 ffmpeg 錄製 10 秒音訊至 `C:\temp\input.wav`，再以 faster-whisper（small 模型、CPU int8）進行語音辨識，輸出辨識文字並寫入 `stt_output.txt`。此工具設計於 WSL 環境下呼叫 Windows 原生錄音裝置，以避開 WSL 麥克風權限限制。

# 語法 / 參數規範
* 無參數（固定錄音 10 秒，使用腳本內建的麥克風裝置名稱）。
* 核心腳本：`scripts/stt_engine_cmd.py`

# 執行步驟 (Steps)
1. 呼叫 Windows 端 `ffmpeg.exe` 錄音 10 秒，存至 `C:\temp\input.wav`。
2. 載入 Whisper 模型並轉錄。
3. 輸出辨識文字。

# 範例 (Examples)
* 啟動語音輸入：`EXECUTE: stt_engine`

# 異常處理 (Edge Cases)
* 找不到音訊檔案（錄音失敗）時拋出例外並中止。
* 麥克風裝置名稱為腳本內寫死值，更換硬體時需修改腳本。
