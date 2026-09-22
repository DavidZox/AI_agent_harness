---
type: Tool
title: 語音輸入引擎
description: 使用者需要進行語音輸入時使用。
version: 1.1.0
dependencies: ["faster_whisper", "ffmpeg (Windows)"]
---

# 背景 / 運作原理
透過 Windows 端 ffmpeg 錄製 10 秒音訊至 `C:\temp\input.wav`（WSL 端對應 `/mnt/c/temp/input.wav`），再以 faster-whisper（small 模型、CPU int8）進行語音辨識，輸出辨識文字並寫入目前工作目錄的 `stt_output.txt`。此工具設計於 WSL 環境下呼叫 Windows 原生錄音裝置，以避開 WSL 麥克風權限限制；ffmpeg 路徑與麥克風裝置 ID 寫死在腳本頂部的設定區。

# 語法 / 參數規範
* 無參數（固定錄音 10 秒，使用腳本內建的麥克風裝置）。
* 核心腳本：`scripts/stt_engine_cmd.py`
* 逾時：錄音階段 30 秒（10 秒錄音 + 20 秒裝置啟動／寫檔緩衝）。辨識階段在程序內執行、無法中斷，只受系統總逾時 600 秒保護。

# 執行步驟 (Steps)
1. 檢查 `faster_whisper` 套件與 `ffmpeg.exe` 是否存在（缺任一項立即回報，不會白錄 10 秒）。
2. 呼叫 Windows 端 `ffmpeg.exe` 錄音 10 秒（30 秒逾時）。
3. 載入 Whisper 模型並轉錄。
4. 回傳辨識文字，並寫入 `stt_output.txt`。

# 範例 (Examples)
* 啟動語音輸入：`EXECUTE: scripts/stt_engine_cmd.py`

# 回傳格式
* 成功：`[PASS] 語音辨識結果:` 換行後接辨識文字。沒有偵測到語音時回傳 `[PASS] 辨識完成，但沒有偵測到任何語音內容...`。
* 失敗：`[ERROR] ...`，含原因說明。

# 異常處理 (Edge Cases)
* 缺少 `faster_whisper` 套件 → `[ERROR] 缺少 faster_whisper 套件`，請回報使用者安裝。
* 找不到 `ffmpeg.exe` → `[ERROR] 找不到 ffmpeg.exe: <路徑>`，需安裝或修改腳本內 `FFMPEG_PATH`。
* 錄音失敗（裝置名稱錯誤、權限不足）→ `[ERROR] 錄音失敗（ffmpeg exit code N）` 並附 ffmpeg stderr 結尾。
* 錄音逾時（裝置無回應）→ `[ERROR] 錄音逾時`，已強制終止 ffmpeg。
* 辨識失敗 → `[ERROR] 語音辨識失敗: <例外>`。
* 麥克風裝置 ID 為腳本內寫死值，更換硬體時需修改腳本。
