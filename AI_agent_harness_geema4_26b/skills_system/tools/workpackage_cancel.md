---
type: Tool
title: 取消 work package
description: 刪除一個 work package，orchestrtor 停止派下一站。
version: 1.0.0
dependencies: ["fih_rmf_system web_console (port 8020)"]
---

# 用途
web_console `DELETE /api/work_packages/{id}` → `cancel_work_package`；送出後讀一幀快照確認清單中已不存在。只停止派下一站，已送到 distribute 的當前站可能仍被機器人執行完。卡在 OverPending 逾時區的單站任務要刪用 `overpending_cancel`。

# 語法
`EXECUTE: scripts/workpackage_cancel_cmd.py <task_id>`（task_id 用 `workpackage_status` 查）

# 範例
`EXECUTE: scripts/workpackage_cancel_cmd.py WP001`

# 回傳
`[PASS] 已送出取消 work package … 快照確認：…`。失敗 `[ERROR] 原因`。

# 異常
* 快照中仍在清單：請求剛送出，稍後用 `workpackage_status` 再確認；一直存在表示 orchestrtor 沒收到。
* 無法連線：web_console 未啟動，回報使用者。
