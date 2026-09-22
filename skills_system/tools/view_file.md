---
type: Tool
title: 查看檔案內容
description: 查看診斷報告、腳本或技能規格文件內容。
version: 1.1.0
dependencies: []
---

# 背景 / 運作原理
讀取指定檔案的純文字內容並回傳，用於查看一般檔案（設定檔、log、腳本原始碼等）。技能規格文件不需要用這個工具載入——直接 `EXECUTE: [技能名稱]` 系統就會自動注入對應的 `tools/<name>.md`；view_file 只用於讀取規格文件以外的一般檔案。

# 語法 / 參數規範
* `file_path` (string, required): 欲讀取的檔案路徑，可為相對路徑或絕對路徑。
* 核心腳本：`scripts/cat_cmd.py`
* 安全邊界：檔案大小超過 1MB 會拒絕讀取。

# 執行步驟 (Steps)
1. 展開並轉換為絕對路徑。
2. 確認檔案存在、非目錄、且未超過 1MB。
3. 讀取並回傳完整文字內容。

# 範例 (Examples)
* 查看設定檔：`EXECUTE: scripts/cat_cmd.py config.yaml`

# 回傳格式
* 成功：`[PASS] 檔案內容 (<path>):` 後接完整文字內容。
* 失敗：`[ERROR] ...`。

# 異常處理 (Edge Cases)
* 檔案不存在、是目錄、或超過 1MB 時回傳 `[ERROR]`。
* 目標不是一般檔案（裝置、socket、FIFO 等，例如 `/dev/zero`）時回傳 `[ERROR]` 並拒絕讀取，避免無限等待。
