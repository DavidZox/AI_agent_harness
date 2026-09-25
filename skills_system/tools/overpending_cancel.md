---
type: Tool
title: 刪除 OverPending 逾時任務
description: 刪除卡在 distribute OverPending 逾時區的單站任務；不在逾時區時可等它進來再刪。
version: 1.1.0
dependencies: ["fih_rmf_system web_console (port 8020)"]
---

# 用途
web_console `DELETE /api/overpending_tasks/{id}`。**OverPending 會來回**：任務在 raw 等超過約 10 秒才進 OverPending，再約 10 秒又回流 raw，DELETE 只對「此刻在逾時區」的任務有效。本技能先看快照：此刻在逾時區才刪；不在時回報所在佇列（不是錯誤），加 `--wait 秒` 會等它進來立刻刪並確認消失。

# 語法
`EXECUTE: scripts/overpending_cancel_cmd.py <任務id> [--wait 秒]`
* 任務 id 形如 `<package>::<輪>::<站>::<序>`，用 `workpackage_status` 查。`--wait` 1～300。

# 範例
`EXECUTE: scripts/overpending_cancel_cmd.py "WP001::0::0::3"`　`EXECUTE: scripts/overpending_cancel_cmd.py "WP001::0::0::3" --wait 30`

# 回傳
刪除：`[PASS] 已刪除 OverPending 任務 …` + 快照確認。未刪除：`[PASS] 未刪除：… 目前在 raw 佇列 …`，建議加 `--wait`。失敗 `[ERROR] 原因`。

# 異常
* 「未刪除：在 raw」是時序不是錯誤：加 `--wait 30` 重做，或改用 `workpackage_cancel` 取消整個 work package。
* 不在任何佇列：可能已被機器人領走、id 打錯或 distribute 沒在跑。
