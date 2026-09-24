---
type: Tool
title: ROS2 節點列表
description: 列出容器內運行中的 node（容器可省略＝目標容器）。
version: 1.3.0
dependencies: ["docker", "ros2"]
---

# 用途
在容器內執行 `ros2 node list`（bash -ic，自動補齊 ROS2 環境與節點所在的 ROS_DOMAIN_ID）。逾時 30 秒。

# 語法
`EXECUTE: scripts/ROS2_node_list_cmd.py [container_name]`
* `container_name`：可省略＝目前的目標容器（CURRENT_TARGET_CONTAINER）；要指定時給完整名稱。

# 範例
`EXECUTE: scripts/ROS2_node_list_cmd.py`（用目標容器）
`EXECUTE: scripts/ROS2_node_list_cmd.py ros2_humble`

# 回傳
成功：每行一個 node（含命名空間；操作的容器與目標不同時末行附 `[TARGET_CONTAINER] <名稱>`）。沒有 node：`[PASS] ...沒有任何輸出`（正常結果）。失敗：`[ERROR] 原因`。

# 異常
* 沒有目標容器又沒給名稱：`[ERROR]` 提示先用 `docker_containers` 查、`docker_open` 選定。
* 容器不存在／未運行／找不到 ros2：訊息會指出原因，回報使用者。
* 逾時：ROS2 daemon 或 DDS 網路問題。
