---
type: Tool
title: 查 work package 執行狀態（一幀或一段時間）
description: 讀 web_console 快照看 work package、distribute 佇列、機器人與事件；--watch 觀察一段時間並整理變化。
version: 1.1.0
dependencies: ["fih_rmf_system web_console (port 8020)"]
---

# 用途
WebSocket `/api/ws` 快照：每個 work package 一行（狀態、第幾站含語意名稱、工作項、機器人或 ⏳ 等待、循環進度）、distribute 佇列（raw／processing／OverPending）、機器人、最近事件。**一幀只是瞬間**：使用者提到「觀察一段時間／持續／變化／過程」時用 `--watch 秒`，會整理站點推進、OverPending 進出、機器人狀態變化與新增事件，結尾一行摘要。

# 語法
`EXECUTE: scripts/workpackage_status_cmd.py [task_id] [--watch 秒]`
* 不帶 `task_id`：全部；帶：只看它與相關工作項。`--watch` 1～300 秒。

# 範例
`EXECUTE: scripts/workpackage_status_cmd.py`　`EXECUTE: scripts/workpackage_status_cmd.py WP001`　`EXECUTE: scripts/workpackage_status_cmd.py --watch 20`

# 回傳
一幀：`[PASS] 目前狀態…`。`--watch`：`[PASS] 觀察 N 秒…` + 變化清單 + 結束時摘要。失敗 `[ERROR] 原因`。

# 異常
* 找不到 id：未送達、已刪除或 orchestrtor 沒在跑；`⏳ 等待機器人` 不是錯誤。
* OverPending 佇列裡的任務要刪用 `overpending_cancel`；取消整個 work package 用 `workpackage_cancel`。無法連線：web_console 未啟動，回報使用者。
