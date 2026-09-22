---
type: Tool
title: 影像檔分析
description: 對指定路徑的影像檔（截圖、相機快照）依提示詞做視覺模型分析，回傳文字描述。
version: 1.0.0
dependencies: ["Pillow", "ollama 多模態模型（預設 gemma4:e4b）"]
---

# 背景 / 運作原理
透過專案的 `vision/` library 把影像檔載入後交給多模態模型推論。這是「Agent 自己主動看圖」的入口：機器人相機快照、ROS image topic 存下來的圖、使用者放在磁碟上的截圖，都可以用這個技能讀取內容。使用者在 Web Console 用 📷 附圖則不需要這個技能，系統會自動處理。

# 語法 / 參數規範
* `image_path` (string, required): 影像檔路徑，相對於目前工作目錄或絕對路徑；支援 PNG / JPEG / WebP / BMP / GIF，單檔上限 20MB。
* `prompt` (string, optional): 要對影像問什麼；省略時使用預設的「詳細描述並逐字列出文字、數值、狀態」提示詞。含空白請用引號包住。
* 核心腳本：`scripts/image_inspect_cmd.py`
* 逾時：300 秒（推論在 Ollama 端執行，多張或高解析度影像較慢）。

# 執行步驟 (Steps)
1. 檢查路徑存在、是檔案、大小未超過上限、可解碼為影像。
2. 將影像與提示詞交給視覺模型（300 秒逾時）。
3. 回傳 `[PASS]` 與模型的文字分析，或 `[ERROR]`。

# 範例 (Examples)
* 讀取截圖中的錯誤訊息：`EXECUTE: scripts/image_inspect_cmd.py logs/error_screen.png "圖中的錯誤訊息是什麼？逐字抄錄"`
* 檢視相機快照：`EXECUTE: scripts/image_inspect_cmd.py /tmp/camera_front.jpg "前方是否有障礙物？"`
* 使用預設提示詞：`EXECUTE: scripts/image_inspect_cmd.py capture.png`

# 回傳格式
* 成功：`[PASS] 影像分析結果（<path>，<寬>x<高>，模型 <name>）:` 換行 `提示詞：...`，`---` 之後為模型回覆。
* 失敗：`[ERROR] <原因>`。

# 異常處理 (Edge Cases)
* 未提供路徑 → `[ERROR]` 並附用法。
* 檔案不存在、是目錄、超過 20MB、不是可辨識的影像格式 → `[ERROR]`，訊息會指出是哪一種；請確認路徑（注意目前工作目錄）後重試。
* Ollama 未啟動 → `[ERROR] 無法連線到 Ollama 服務`；模型不存在 → `[ERROR] ... 請先 ollama pull`。這兩種請回報使用者，不要重試。
* 超過 300 秒 → `[ERROR] 視覺推論逾時`，可改問更具體的問題或請使用者先裁切影像。
* 模型無法辨識內容時會在回覆中說「看不清楚」，屆時應回報使用者而不是自行推測。
