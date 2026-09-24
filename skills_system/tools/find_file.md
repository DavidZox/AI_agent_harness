---
type: Tool
title: 檔案名稱搜尋
description: 只搜尋「檔案名稱」本身；標頭算好找到幾個並附最新修改的檔案。
version: 1.1.0
dependencies: []
---

# 背景 / 運作原理
包裝系統 `find <path> -iname *pattern*`，自動判斷參數中何者為路徑、何者為檔名關鍵字，並阻擋對根目錄 `/` 的搜尋。

# 語法 / 參數規範
* `filename_keyword` (string, required): 檔名關鍵字（模糊比對，不分大小寫）。
* `path` (string, optional): 搜尋起始目錄，預設為 `.`。
* 核心腳本：`scripts/find_file_cmd.py`

# 執行步驟 (Steps)
1. 拆解參數，依「是否為現存目錄」判斷 path 與 pattern。
2. 檢查 path 是否為根目錄（阻斷）。
3. 以 5 秒逾時執行 `find -iname` 並回傳結果。

# 範例 (Examples)
* 尋找 Modelfile：`EXECUTE: scripts/find_file_cmd.py "Modelfile" .`

# 回傳
`[PASS] 找到 N 個檔名含「關鍵字」的項目（搜尋 <path>）:`，第二行 `最新修改：路徑（時間）；其次：…`（結果 ≤300 個時），之後每行一個路徑。數量與最新由腳本算好，直接引用，不要自己數。找不到：`[PASS] 找不到…（0 個）`。

# 異常處理 (Edge Cases)
* 搜尋路徑為 `/` 時阻斷。
* 找不到檔案時回傳 `[PASS]` 訊息（非錯誤）。
