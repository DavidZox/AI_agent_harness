---
type: Tool
title: 工作包：發送／取消／查詢／語義地圖（任務協調器）
description: 對 orchestrtor 發送多站點 work package、取消 work package、刪除 OverPending 任務、觀察執行狀態、讀語義地圖並對應目前機器人與工作包位置。
version: 2.2.0
dependencies: ["fih_rmf_system web_console (port 8020)", "或 ROS2 容器內的 ros2 CLI（--ros2 僅發送）"]
---

# 用途
全部走 web_console `http://localhost:8020`（容器 network_mode: host）：發送 work package（`POST /api/send_tasks` → `incoming_work_packages`，orchestrtor 逐站派給 distribute）、取消、刪除 OverPending 任務、WebSocket 狀態快照、語義地圖（`/api/topology`：站點代號、語意名稱、說明如 VLM 判讀紀錄、座標、路段）。站點可用代號或語意名稱指定，會自動對應。

# 語法
發送：`EXECUTE: scripts/workpackage_est_cmd.py <task_id|auto> <站點1,站點2,...> [--amr 機器人] [--type regular|charge|park] [--level normal|middle|emergency] [--weight 整數] [--loop [輪數]] [--desc 描述1,描述2] [--ros2 容器名稱] [--dry-run]`
取消：`--cancel <task_id>`；`--cancel-overpending <任務id> [--wait 秒]`
查詢：`--status [task_id] [--watch 秒]`；`--map [關鍵字或站點代號]`；`--stations`；`--robots`（各模式可加 `--url 位址`）
* `task_id` 須唯一（重複的被忽略），`auto` 自動產生。站點寫代號（a3）或語意名稱（加工線通道-6）皆可，找不到或對應到多站會回 `[ERROR]` 列出候選。
* 預設：`--amr` 空＝交給 distribute 挑；`--type regular`；`--level normal`；`--weight 0`＝自動算。`--loop` 接正整數＝輪數，不接＝循環到取消。
* **時間**：一幀快照只是瞬間。使用者提到「觀察一段時間／持續／變化／過程」時用 `--status --watch 秒`（最多 300），會整理站點推進、OverPending 進出、機器人狀態變化與事件。
* **OverPending 會來回**：任務在 raw 等超過約 10 秒才進 OverPending，再約 10 秒又回流 raw；`--cancel-overpending` 只在它「此刻在逾時區」時刪得掉，不在時回報所在佇列並建議加 `--wait 30` 等它進來再刪。
* `--map` 不帶參數列全部站點（代號｜語意名稱｜目前誰在此／前往／哪個工作包派工中）與機器人位置；帶關鍵字或代號時顯示該站／路段的完整說明與連接路段。

# 範例
`EXECUTE: scripts/workpackage_est_cmd.py WP001 home,加工線通道-6,a7 --amr tb1`
`EXECUTE: scripts/workpackage_est_cmd.py --map 加工線通道`　`EXECUTE: scripts/workpackage_est_cmd.py --status --watch 20`
`EXECUTE: scripts/workpackage_est_cmd.py --cancel-overpending "WP001::0::0::3" --wait 30`

# 回傳
成功 `[PASS]`：發送附站點對應、摘要、送出的 JSON、web_console 回應；取消附快照確認；`--status` 每個 work package 一行（狀態、第幾站含語意名稱、工作項、機器人或 ⏳ 等待、循環進度）加 distribute 佇列、機器人、事件；`--watch` 先列變化清單再附最後一幀。失敗 `[ERROR] 原因`。送出成功只代表 orchestrtor 收到。

# 異常
* 無法連線／逾時：web_console 未啟動，回報使用者；發送可改 `--ros2 容器名稱`，勿原樣重試。HTTP 503：稍候重試一次。HTTP 422／參數錯誤：依訊息修正。
* `--status` 找不到 id：未送達、已刪除或 orchestrtor 沒在跑。`--cancel` 只停止派下一站，當前站可能仍執行完。`--cancel-overpending` 回「未刪除：在 raw」不是錯誤，是時序，用 `--wait`。
* 沒有機器人可承接時工作包停在當前站等待（⏳），不是錯誤。
