---
type: Tool
title: ROS2 節點資訊
description: 查詢指定 node 的訂閱、發布、服務介面（容器可省略＝目標容器）。
version: 1.3.0
dependencies: ["docker", "ros2"]
---

# 用途
在容器內執行 `ros2 node info <node>`（bash -ic，自動補齊 ROS2 環境與節點所在的 ROS_DOMAIN_ID）。逾時 30 秒。

# 語法
`EXECUTE: scripts/ROS2_node_info_cmd.py [container_name] <node_name>`
* `container_name`：可省略＝目前的目標容器（CURRENT_TARGET_CONTAINER）。
* `node_name` 需含命名空間（如 `/nav_node`），不確定先用 `ROS2_node_list` 查。

# 範例
`EXECUTE: scripts/ROS2_node_info_cmd.py /nav_node`（用目標容器）
`EXECUTE: scripts/ROS2_node_info_cmd.py ros2_humble /nav_node`

# 回傳
成功：`ros2 node info` 原始輸出（操作的容器與目標不同時末行附 `[TARGET_CONTAINER] <名稱>`）。失敗：`[ERROR] 原因`。

# 異常
* 節點不存在：用 `ROS2_node_list` 確認名稱，不要猜名稱重試。
* 沒有目標容器又沒給名稱：`[ERROR]` 提示先用 `docker_containers` 查、`docker_open` 選定。
* 容器不存在／未運行／找不到 ros2／逾時：訊息會指出原因，回報使用者。
