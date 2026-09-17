---
type: Tool
title: 查看檔案內容
description: 查看診斷報告或腳本內容
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
包裝 `cat`，用於讀取單一檔案的完整文字內容，例如設定檔、日誌、程式碼。依 `ROBOT_AGENT.md` 規範，執行前必須先向使用者確認。

# 語法 / 參數規範
* `file_path` (string, required)：欲讀取的檔案路徑，僅接受單一路徑（不可為目錄）。

# 執行步驟 (Steps)
1. 接收檔案路徑，展開為絕對路徑。
2. 確認檔案存在、非目錄。
3. 檢查檔案大小是否超過安全上限（1MB）。
4. 以 UTF-8（錯誤字元以替代符號處理）讀取並回傳完整內容。

# 範例 (Examples)
* `EXECUTE: cat_cmd.py config.yaml`

# 異常處理 (Edge Cases)
* 檔案不存在時回傳 `[ERROR] 檔案不存在: ...`。
* 路徑為目錄時回傳 `[ERROR] '...' 是一個目錄，無法使用 cat 讀取。`。
* 檔案大於 1MB 時回傳 `[ERROR] 檔案大小 (...) 超過安全邊界 (1MB)，請改用其他過濾工具。`（例如改用 `search_text`）。
