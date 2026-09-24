---
type: Tool
title: 列出容器
description: 列出主機上所有容器（名稱、狀態、映像檔）。
version: 1.2.0
dependencies: ["docker"]
---

# 用途
包裝 `docker ps -a`，第一行統計總數與運行中數量。進入容器或執行指令前先用它取得完整名稱；查到後用 `docker_open` 選定為目標容器，之後其他容器技能可省略名稱。逾時 15 秒。

# 語法
`EXECUTE: scripts/docker_containers_cmd.py [--running] [keyword]`
* `--running`：只列運行中的。`keyword`：過濾名稱（子字串、不分大小寫），最多一個。

# 範例
`EXECUTE: scripts/docker_containers_cmd.py`
`EXECUTE: scripts/docker_containers_cmd.py --running rmf`

# 回傳
成功：`[PASS] 容器...：共 N 個，運行中 M 個` + 表格（NAME / STATUS / IMAGE / ID）。沒有符合：`[PASS] 目前沒有任何容器...`（不是錯誤）。失敗：`[ERROR] 原因`。

# 異常
* docker 不可用／daemon 未啟動／逾時：回報使用者。
* 狀態 Exited 的容器無法執行指令，需先請使用者啟動。
* 沒有目標容器且有多個運行中的容器時，請使用者指定要用哪一個，不要自行挑第一個。
