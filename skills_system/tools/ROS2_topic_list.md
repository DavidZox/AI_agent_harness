---
type: Tool
title: ROS2 話題列表
description: 在容器內使用查詢 topic 列表。
version: 1.0.0
dependencies: ["docker", "ros2"]
---

# 背景 / 運作原理
以 `docker exec <container> bash -ic "ros2 topic list"` 在容器互動式 shell 中執行，確保容器內的 ROS2 環境變數（如 `/opt/ros/<distro>/setup.bash`）已被載入。

# 語法 / 參數規範
* `container_name` (string, required): 已啟動且安裝 ROS2 的容器名稱。
* 核心腳本：`scripts/ROS2_topic_list_cmd.py`

# 執行步驟 (Steps)
1. 組合 `docker exec <container> bash -ic "ros2 topic list"`。
2. 執行並回傳話題清單。

# 範例 (Examples)
* 查詢話題列表：`EXECUTE: scripts/ROS2_topic_list_cmd.py ros2_humble`

# 異常處理 (Edge Cases)
* 容器未啟動或未安裝 ROS2 時回傳 `[ERROR]`。
