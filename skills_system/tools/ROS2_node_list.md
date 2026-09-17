---
type: Tool
title: ROS2節點列表
description: 在容器內使用查詢 node 列表
version: 1.0.0
dependencies: [docker_est]
---

# 背景 / 運作原理
於指定容器內以互動式 shell 執行 `ros2 node list`，列出目前所有作用中的 ROS2 節點。

# 語法 / 參數規範
* `container_name` (string, required)：已啟動且已安裝 ROS2 的容器名稱。

# 執行步驟 (Steps)
1. 接收容器名稱。
2. 執行 `docker exec <container_name> bash -ic "ros2 node list"`。
3. 回傳節點清單文字。

# 範例 (Examples)
* `EXECUTE: ROS2_node_list_cmd.py ros_humble_image`

# 異常處理 (Edge Cases)
* 缺少容器名稱時回傳用法提示。
* 容器內 ROS2 環境未正確設定時回傳 `[ERROR] 執行失敗: ...`。
