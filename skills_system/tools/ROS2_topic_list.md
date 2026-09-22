---
type: Tool
title: ROS2 話題列表
description: 列出容器內目前的 topic。
version: 1.2.0
dependencies: ["docker", "ros2"]
---

# 用途
在容器內執行 `ros2 topic list`（bash -ic）。找不到 ros2 時自動 source `/opt/ros/$ROS_DISTRO/setup.bash`。逾時 30 秒。

# 語法
`EXECUTE: scripts/ROS2_topic_list_cmd.py <container_name>`（需完整名稱）

# 範例
`EXECUTE: scripts/ROS2_topic_list_cmd.py ros2_humble`

# 回傳
成功：每行一個 topic。沒有 topic：`[PASS] ...沒有任何輸出`（不是錯誤）。失敗：`[ERROR] 原因`。

# 異常
* 容器不存在／未運行：先用 `docker_containers` 確認名稱與狀態。
* 找不到 ros2：容器未安裝 ROS2 或無 ROS_DISTRO，回報使用者。
* 逾時：ROS2 daemon 或 DDS 網路問題，回報使用者。
