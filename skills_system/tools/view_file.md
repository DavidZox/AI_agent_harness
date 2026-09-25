---
type: Tool
title: 查看檔案內容
description: 查看診斷報告、腳本或設定檔的純文字內容。
version: 1.2.0
dependencies: []
---

# 用途
讀取本機一般檔案（設定檔、log、腳本原始碼）的完整文字。技能規格文件不用它：`action.command` 填技能名稱系統就會注入規格。容器內的檔案請用 docker_runcmd（`cat`）。

# 語法
`EXECUTE: scripts/cat_cmd.py <file_path>`
* `file_path`：相對目前工作目錄或絕對路徑；超過 1MB 拒絕讀取。

# 範例
`EXECUTE: scripts/cat_cmd.py config.yaml`

# 回傳
成功：`[PASS] 檔案內容 (<path>):` + 完整文字（過長時由獨立 session 依你的目的擷取，完整原文有存檔可 `result_grep`）。失敗：`[ERROR] 原因`。

# 異常
* 檔案不存在、是目錄、超過 1MB、或不是一般檔案（裝置、socket、FIFO）：`[ERROR]`，修正路徑；找檔名用 `find_file`、列目錄用 `list_dir`。
