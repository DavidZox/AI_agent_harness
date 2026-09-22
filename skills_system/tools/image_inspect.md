---
type: Tool
title: 影像檔分析
description: 對影像檔依提示詞做視覺模型分析，回傳文字。
version: 1.1.0
dependencies: ["Pillow", "ollama 多模態模型"]
---

# 用途
讓 Agent 自己讀取磁碟上的影像（截圖、相機快照）。使用者用 📷 附圖時不需此技能。

# 語法
`EXECUTE: scripts/image_inspect_cmd.py <image_path> ["prompt"]`
* `image_path`：相對目前工作目錄或絕對路徑；PNG/JPEG/WebP/BMP/GIF，上限 20MB。
* `prompt`：可省略，預設為「詳細描述並逐字列出文字、數值、狀態」。
* 逾時 300 秒。

# 範例
`EXECUTE: scripts/image_inspect_cmd.py logs/err.png "錯誤訊息是什麼？逐字抄錄"`

# 回傳
成功：`[PASS] 影像分析結果（路徑，尺寸，模型）:` 接模型回覆。失敗：`[ERROR] 原因`。

# 異常
* 檔案不存在／非影像／超過 20MB：修正路徑後重試。
* Ollama 未啟動或模型不存在：回報使用者，勿重試。
* 逾時：改問更具體的問題。
* 模型回覆「看不清楚」時如實回報，不要推測。
