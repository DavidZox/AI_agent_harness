---
type: Tool
title: ROS2 節點詳細資訊
description: 指定 node 名稱進行 info 命令，獲得 node 詳細資訊。
version: 1.0.0
dependencies: ["docker", "ros2"]
---

# 背景 / 運作原理
以 `ros2 node info <node>` 取得指定節點的訂閱、發布、服務與動作介面清單。

# 語法 / 參數規範
* `container_name` (string, required): 容器名稱。
* `node_name` (string, required): 欲查詢的節點名稱（需含完整命名空間）。
* 核心腳本：`scripts/ROS2_node_info_cmd.py`

# 執行步驟 (Steps)
1. 組合 `docker exec <container> bash -ic "ros2 node info <node>"`。
2. 執行並回傳節點詳細資訊。

# 範例 (Examples)
* 查詢節點資訊：`EXECUTE: ROS2_node_info ros2_humble /nav_node`

# 異常處理 (Edge Cases)
* 節點名稱錯誤或不存在時回傳 `[ERROR]`。
