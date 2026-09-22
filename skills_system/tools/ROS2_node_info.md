---
type: Tool
title: ROS2 節點資訊
description: 查詢指定 node 的訂閱、發布、服務介面。
version: 1.2.0
dependencies: ["docker", "ros2"]
---

# 用途
在容器內執行 `ros2 node info <node>`。找不到 ros2 時自動 source `/opt/ros/$ROS_DISTRO/setup.bash`。逾時 30 秒。

# 語法
`EXECUTE: scripts/ROS2_node_info_cmd.py <container_name> <node_name>`
* `node_name` 需含命名空間（如 `/nav_node`），不確定先用 `ROS2_node_list` 查。

# 範例
`EXECUTE: scripts/ROS2_node_info_cmd.py ros2_humble /nav_node`

# 回傳
成功：`ros2 node info` 原始輸出。失敗：`[ERROR] 原因`。

# 異常
* 節點不存在：用 `ROS2_node_list` 確認名稱，不要猜名稱重試。
* 容器不存在／未運行／找不到 ros2／逾時：訊息會指出原因，回報使用者。
