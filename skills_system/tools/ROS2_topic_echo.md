---
type: Tool
title: ROS2 話題內容
description: 讀取指定 topic 的一筆訊息。
version: 1.2.0
dependencies: ["docker", "ros2"]
---

# 用途
在容器內執行 `ros2 topic echo <topic> --once`。沒有 publisher 時會等到逾時，逾時會確實終止容器內程序。找不到 ros2 時自動 source `/opt/ros/$ROS_DISTRO/setup.bash`。

# 語法
`EXECUTE: scripts/ROS2_topic_echo_cmd.py <container_name> <topic_name> [timeout_seconds]`
* `timeout_seconds`：預設 15，範圍 1～570；低頻 topic 請加大。

# 範例
`EXECUTE: scripts/ROS2_topic_echo_cmd.py ros2_humble /cmd_vel`
`EXECUTE: scripts/ROS2_topic_echo_cmd.py ros2_humble /diagnostics 60`

# 回傳
成功：一筆訊息的 YAML。失敗：`[ERROR] 原因`。

# 異常
* 找不到 topic 或沒有 publisher：先用 `ROS2_topic_list` 確認名稱。
* 逾時：確定是低頻 topic 才加大秒數重試一次，否則回報「目前沒有資料在發布」。
* 容器不存在／未運行／找不到 ros2：訊息會指出原因，回報使用者。
