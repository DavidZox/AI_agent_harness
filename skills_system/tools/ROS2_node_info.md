---
type: Tool
title: ROS2節點詳細
description: 指定 node 名稱進行 info 命令，獲得 node 詳細資訊
version: 1.0.0
dependencies: [docker_est]
---

# 背景 / 運作原理
於指定容器內執行 `ros2 node info <node>`，取得該節點訂閱／發布的 topic、service、action 等詳細資訊。

# 語法 / 參數規範
* `container_name` (string, required)：容器名稱。
* `node_name` (string, required)：欲查詢的 ROS2 節點名稱（例如 `/route_executor`）。

# 執行步驟 (Steps)
1. 接收容器名稱與節點名稱。
2. 執行 `docker exec <container_name> bash -ic "ros2 node info <node_name>"`。
3. 回傳節點詳細資訊文字。

# 範例 (Examples)
* `EXECUTE: ROS2_node_info_cmd.py ros_humble_image /route_executor`

# 異常處理 (Edge Cases)
* 缺少任一必要參數時回傳用法提示 `用法: python3 scripts/ROS2_node_info_cmd.py <container_name> <node_name>`。
* 節點名稱不存在時，`ros2 node info` 本身會回報找不到節點的錯誤。
