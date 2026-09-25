---
type: Tool
title: 查看目錄清單
description: 列出目錄內容並算好數量與最新修改；可 --filter 關鍵字計數、--newest 依修改時間排序。
version: 1.4.0
dependencies: []
---

# 用途
列出本機目錄（容器內的目錄請用 docker_runcmd）。標頭由腳本算好「共 N 項（幾個檔案、幾個目錄）」，並附一行「最新修改：A（時間）；其次：B、C」。使用者問「有幾個 X」「哪個最新／最近改過」時用 `--filter`／`--newest` 讓腳本算，直接引用標頭與「最新修改」那一行，不要自己數或比時間。逾時 5 秒。

# 語法
`EXECUTE: scripts/ls_cmd.py [path] [--filter 關鍵字] [--newest [N]]`
* `path`：預設目前工作目錄。`--filter 關鍵字`：只列名稱含關鍵字的項目（不分大小寫），標頭給符合的數量。`--newest [N]`：依修改時間新→舊只列前 N 項（預設 5）。舊的 ls 旗標（`-la`）可省略，給了也會忽略。

# 範例
`EXECUTE: scripts/ls_cmd.py /opt/ros`　`EXECUTE: scripts/ls_cmd.py launch --filter .launch.py`　`EXECUTE: scripts/ls_cmd.py . --newest 3`

# 回傳
`[PASS] 目錄列表 (<path>)：共 N 項（F 個檔案、D 個目錄）[；名稱含「關鍵字」的 K 項][；依修改時間新→舊只列前 N 項]`，第二行 `最新修改：…`，之後每行「權限 大小 修改時間 名稱」（目錄加 `/`）。失敗：`[ERROR] 無法讀取目錄: 原因`。

# 異常
* 路徑不存在／不是目錄／無權限：`[ERROR]`，修正路徑；看檔案內容用 `view_file`。
* 逾時 5 秒：目錄過大或掛載裝置無回應，改指定較小的子目錄。
