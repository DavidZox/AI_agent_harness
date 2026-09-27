---
type: Tool
title: 檔案內容過濾
description: 在檔案「內部文字」中過濾特定關鍵字；標頭算好命中筆數與檔案數。
version: 1.2.0
dependencies: []
---

# 用途
包裝 `grep -r -n -i -I`（遞迴、行號、忽略大小寫、跳過二進位檔），阻擋對根目錄 `/` 的搜尋。逾時 8 秒。

# 語法
`EXECUTE: scripts/grep_cmd.py "<keyword>" <path>`
* `keyword`：關鍵字（含空白用引號）。`path`：目前工作目錄（`.`）或其子目錄，嚴禁 `/`。

# 範例
`EXECUTE: scripts/grep_cmd.py "ERROR" .`　`EXECUTE: scripts/grep_cmd.py "model" ./skills_system`

# 回傳
`[PASS] 搜尋結果：共 N 筆命中，分布在 M 個檔案:` + `路徑:行號:內容`。筆數與檔案數由腳本算好，直接引用。找不到：`[PASS] 找不到符合該關鍵字的內容`（不是錯誤）。

# 異常
* 路徑為 `/`：`[ERROR]` 安全邊界阻斷，改指定具體目錄。
* 逾時 8 秒：範圍過大，縮小目錄或關鍵字。
