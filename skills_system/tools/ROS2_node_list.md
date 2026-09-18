---
type: Tool
title: ROS2 節點列表
description: 在容器內使用查詢 node 列表。
version: 1.0.0
dependencies: ["docker", "ros2"]
---

# 背景 / 運作原理
以 `docker exec <container> bash -ic "ros2 node list"` 取得目前運行中的節點清單。

# 語法 / 參數規範
* `container_name` (string, required): 容器名稱。
* 核心腳本：`scripts/ROS2_node_list_cmd.py`

# 執行步驟 (Steps)
1. 組合並執行 `ros2 node list`。
2. 回傳節點清單。

# 範例 (Examples)
* 查詢節點列表：`EXECUTE: ROS2_node_list ros2_humble`

# 異常處理 (Edge Cases)
* 容器未啟動或無節點運行時回傳空結果或 `[ERROR]`。
