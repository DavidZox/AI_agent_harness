---
type: Tool
title: 語音輸入引擎
description: 錄音 10 秒並以 Whisper 辨識成文字。
version: 1.2.0
dependencies: ["faster_whisper", "ffmpeg (Windows)"]
---

# 用途
WSL 環境下呼叫 Windows 端 ffmpeg 錄音 10 秒至 `C:\temp\input.wav`，再以 faster-whisper（small、CPU int8）辨識，結果同時寫入 `stt_output.txt`。ffmpeg 路徑與麥克風 ID 寫死在腳本頂部。

# 語法
`EXECUTE: scripts/stt_engine_cmd.py`（無參數）
* 逾時：錄音階段 30 秒；辨識階段無法中斷，只受系統總逾時 600 秒保護。

# 回傳
成功：`[PASS] 語音辨識結果:` + 文字；沒聲音：`[PASS] ...沒有偵測到任何語音內容`。失敗：`[ERROR] 原因`。

# 異常
* 缺 faster_whisper 或找不到 ffmpeg.exe：錄音前即回報，請使用者安裝或改腳本設定。
* 錄音失敗／逾時：麥克風裝置名稱或權限問題，回報使用者。
* 更換硬體需修改腳本內的麥克風 ID。
