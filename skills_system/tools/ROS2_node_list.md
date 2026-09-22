---
type: Tool
title: ROS2 節點列表
description: 列出容器內運行中的 node。
version: 1.2.0
dependencies: ["docker", "ros2"]
---

# 用途
在容器內執行 `ros2 node list`。找不到 ros2 時自動 source `/opt/ros/$ROS_DISTRO/setup.bash`。逾時 30 秒。

# 語法
`EXECUTE: scripts/ROS2_node_list_cmd.py <container_name>`（需完整名稱）

# 範例
`EXECUTE: scripts/ROS2_node_list_cmd.py ros2_humble`

# 回傳
成功：每行一個 node（含命名空間）。沒有 node：`[PASS] ...沒有任何輸出`（正常結果）。失敗：`[ERROR] 原因`。

# 異常
* 容器不存在／未運行／找不到 ros2：訊息會指出原因，回報使用者。
* 逾時：ROS2 daemon 或 DDS 網路問題。
