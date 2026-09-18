---
type: Tool
title: 檔案內容過濾
description: 在檔案「內部文字」中過濾特定關鍵字。
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
包裝系統 `grep` 指令，自動補上 `-r -n -i -I` 旗標（遞迴、行號、忽略大小寫、跳過二進位檔），並阻擋對根目錄 `/` 的遞迴搜尋以避免系統過載。

# 語法 / 參數規範
* `keyword` (string, required): 欲搜尋的關鍵字。
* `path` (string, required): 搜尋範圍，必須為當前工作目錄（`.`）或其子目錄，嚴禁使用 `/`。
* 核心腳本：`scripts/grep_cmd.py`

# 執行步驟 (Steps)
1. 拆解參數並移除誤帶入的 `grep` 字樣。
2. 補齊必要旗標並檢查路徑是否為根目錄（阻斷）。
3. 以 8 秒逾時執行並回傳結果。

# 範例 (Examples)
* 搜尋錯誤紀錄：`EXECUTE: scripts/grep_cmd.py "ERROR" .`
* 搜尋設定值：`EXECUTE: scripts/grep_cmd.py "model" ./skills_system`

# 異常處理 (Edge Cases)
* 搜尋目標為 `/` 時會直接阻斷並回傳安全邊界錯誤。
* 找不到關鍵字時回傳 `[PASS] 找不到符合該關鍵字的內容`（非錯誤）。
* 搜尋範圍過大導致逾時（8 秒）會中止並提示縮小範圍。
