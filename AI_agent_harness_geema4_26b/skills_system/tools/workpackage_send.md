---
type: Tool
title: 發送 work package（任務協調器）
description: 對 orchestrtor 發送多站點 work package；站點可用語意名稱，可設 N 輪或無限循環。
version: 1.1.0
dependencies: ["fih_rmf_system web_console (port 8020)", "或 ROS2 容器內的 ros2 CLI（--ros2）"]
---

# 用途
web_console `POST /api/send_tasks` → `incoming_work_packages`，orchestrtor 逐站派給 distribute。站點寫代號（a3）或語意名稱（加工線通道-6）皆可，自動對應；對不到或對到多站回 `[ERROR]` 列候選。只負責發送：查狀態用 `workpackage_status`，取消用 `workpackage_cancel`，看站點用 `semantic_map`。

# 語法
`EXECUTE: scripts/workpackage_send_cmd.py <task_id|auto> <站點1,站點2,...> [--amr 機器人] [--type regular|charge|park] [--level normal|middle|emergency] [--weight 整數] [--loop [輪數]] [--desc 描述1,描述2] [--ros2 [容器]] [--dry-run]`
* `task_id` 須唯一，`auto` 自動產生。預設：`--amr` 空＝交給 distribute 挑；`--type regular`；`--level normal`；`--weight 0`＝自動算。
* **循環**：「無限循環／一直跑／直到叫停」→ `--loop` 後面**不接數字**（`--loop 0`、`--forever` 也可）；「N 輪」→ `--loop N`；沒提就不加（只跑一輪）。回傳第一行會寫出循環設定，請核對。
* `--ros2 [容器]`：web_console 沒在跑時改在 ROS2 容器內直接發布（容器省略＝目標容器）。`--dry-run` 只顯示不送。

# 範例
`EXECUTE: scripts/workpackage_send_cmd.py WP001 home,加工線通道-6,a7 --amr tb1`（只跑一輪）
`EXECUTE: scripts/workpackage_send_cmd.py 巡邏 home,a3,a7 --amr tb1 --loop`（無限循環）　`EXECUTE: scripts/workpackage_send_cmd.py 巡邏 home,a3,a7 --amr tb1 --loop 3`（3 輪）

# 回傳
`[PASS] work package「id」已送出：N 站，循環：…，機器人：…` + 站點對應、參數、送出的 JSON；送出成功只代表 orchestrtor 收到。失敗 `[ERROR] 原因`。

# 異常
* 無法連線／逾時：web_console 未啟動，回報使用者或改 `--ros2`，勿原樣重試；HTTP 503 稍候重試一次；HTTP 422／參數錯誤依訊息修正。
* 沒有機器人可承接時工作包停在當前站等待（⏳），不是錯誤。
