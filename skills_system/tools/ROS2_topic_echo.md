---
type: Tool
title: ROS2 話題內容（單筆或一段時間）
description: 讀取指定 topic 的一筆訊息，或用 --duration 擷取一段時間並整理欄位變化、頻率。
version: 1.3.0
dependencies: ["docker", "ros2"]
---

# 用途
在容器內執行 `ros2 topic echo`。不加 `--duration`：`--once` 讀一筆就結束。加 `--duration 秒`：持續擷取這段時間的所有訊息，回報則數與頻率、第一則與最後一則原文、每個欄位的變化（數值 min→max、字串有幾種值、固定不變的欄位），給你分析用，不會把幾百則原文全部丟回來。找不到 ros2 時自動 source `/opt/ros/$ROS_DISTRO/setup.bash`。

# 語法
`EXECUTE: scripts/ROS2_topic_echo_cmd.py <container_name> <topic_name> [timeout_seconds] [--duration 秒]`
* `timeout_seconds`：單筆模式等第一則訊息的上限，預設 15，範圍 1～570；低頻 topic 請加大。
* `--duration 秒`（2～300）：使用者提到「觀察一段時間／持續／有沒有變化／頻率／幾秒」時使用；高頻 topic 請縮短秒數。

# 範例
`EXECUTE: scripts/ROS2_topic_echo_cmd.py ros2_humble /cmd_vel`
`EXECUTE: scripts/ROS2_topic_echo_cmd.py ros2_humble /diagnostics 60`
`EXECUTE: scripts/ROS2_topic_echo_cmd.py ros2_humble /odom --duration 10`

# 回傳
單筆：一筆訊息的 YAML。一段時間：`[PASS] 觀察 '<topic>' N 秒：收到 M 則（約 X Hz）` 加原文樣本與欄位變化清單；沒收到訊息也是 `[PASS]` 並說明可能原因。失敗：`[ERROR] 原因`。

# 異常
* 找不到 topic 或沒有 publisher：先用 `ROS2_topic_list` 確認名稱。
* 單筆逾時：確定是低頻 topic 才加大秒數重試一次，否則回報「目前沒有資料在發布」。
* 容器不存在／未運行／找不到 ros2：訊息會指出原因，回報使用者。
