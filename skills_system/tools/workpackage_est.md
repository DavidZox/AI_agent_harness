---
type: Tool
title: 工作包：發送／取消／查詢（任務協調器）
description: 對 orchestrtor 發送多站點 work package、取消 work package、刪除 OverPending 任務、查詢執行狀態與場域站點／機隊。
version: 2.1.0
dependencies: ["fih_rmf_system web_console (port 8020)", "或 ROS2 容器內的 ros2 CLI（--ros2 僅發送）"]
---

# 用途
對齊 web_console 橋接節點的全部能力，走 `http://localhost:8020`（容器 network_mode: host，宿主機直連）：發送 work package（`POST /api/send_tasks` → `incoming_work_packages`，orchestrtor 逐站派給 distribute）、取消（`DELETE /api/work_packages/<id>`）、刪除 OverPending 任務、讀狀態快照（WebSocket `/api/ws`）、查站點與機隊。逾時 15 秒。

# 語法
發送：`EXECUTE: scripts/workpackage_est_cmd.py <task_id|auto> <站點1,站點2,...> [--amr 機器人] [--type regular|charge|park] [--level normal|middle|emergency] [--weight 整數] [--loop [輪數]] [--desc 描述1,描述2] [--ros2 容器名稱] [--dry-run]`
取消：`--cancel <task_id>`　刪除 OverPending：`--cancel-overpending <任務id>`
查詢：`--status [task_id]`　`--stations`　`--robots`（各模式皆可加 `--url 位址`）
* `task_id` 須唯一（重複的被 orchestrtor 忽略），`auto` 自動產生；站點依執行順序、逗號分隔，須為拓譜圖站點（`--stations` 可查）。
* 預設：`--amr` 空＝交給 distribute 挑機器人、`--type regular`、`--level normal`、`--weight 0`＝依類型與等級自動算。`--loop` 接正整數＝輪數，不接＝循環到取消。
* `--status` 不帶 id 列全部 work package、distribute 佇列、機器人；帶 id 只看該包。`--ros2` 不經 web_console 直接 `ros2 topic pub`（只有發送）。欄位不可含逗號。

# 範例
`EXECUTE: scripts/workpackage_est_cmd.py WP001 home,a3,a7 --amr tb1`
`EXECUTE: scripts/workpackage_est_cmd.py auto a3,a7 --loop 3 --weight 5 --desc "取料,卸料"`
`EXECUTE: scripts/workpackage_est_cmd.py --status WP001`　`EXECUTE: scripts/workpackage_est_cmd.py --cancel WP001`

# 回傳
成功 `[PASS]`：發送附站點摘要、送出的 JSON、web_console 回應；取消附快照確認是否已移除；查詢每個 work package 一行（狀態、第幾站、工作項、機器人或 ⏳ 等待、循環進度）加 distribute 佇列、機器人、最近事件。失敗 `[ERROR] 原因`。送出成功只代表 orchestrtor 收到，進度用 `--status` 追蹤。

# 異常
* 無法連線／逾時：web_console 未啟動（rmf_launch.py 帶起），回報使用者，發送可改 `--ros2 容器名稱`；勿原樣重試。
* HTTP 503：橋接節點未就緒，稍候重試一次。HTTP 422／參數錯誤：依訊息修正。
* `--status` 找不到 id：未送達、已刪除或 orchestrtor 沒在跑。`--cancel` 只停止派下一站，當前站可能仍執行完；`--cancel-overpending` 只影響已進入 OverPending 的任務。⏳ 等待機器人不是錯誤。
