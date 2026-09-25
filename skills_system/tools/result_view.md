---
type: Tool
title: 讀工具結果存檔的一段原文
description: 讀某個工具結果存檔的第 N 行起 M 行，配合 result_grep 的行號看命中處的整段原文。
version: 1.0.0
dependencies: []
---

# 用途
`result_grep` 只給命中行與前後幾行；要看整段結構（例如一則完整的 topic 訊息、一段完整的錯誤堆疊）時，用它從指定行號讀一段。也可以從頭讀，了解存檔的欄位長什麼樣再決定關鍵字。不會重跑工具。

# 語法
`EXECUTE: scripts/result_view_cmd.py <編號|latest> [--from 行號] [--lines 行數]`
* `--from`：起始行號（與 `result_grep` 顯示的 `L` 行號一致），預設原文第一行。`--lines`：行數，預設 80（上限 300）。

# 範例
`EXECUTE: scripts/result_view_cmd.py 16 --from 120 --lines 40`
`EXECUTE: scripts/result_view_cmd.py latest`

# 回傳
`[PASS] #編號 腳本（時間，狀態）｜指令｜原文共 L 行…；顯示第 a～b 行` + `當時的任務：…` + 每行 `L行號: 內容`；未到檔尾會提示 `--from` 下一個行號。

# 異常
* 起始行超過總行數：`[ERROR]`，改小 `--from`。
* 找不到編號：用 `result_list` 確認。
