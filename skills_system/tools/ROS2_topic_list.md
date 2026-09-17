---
type: Tool
title: ROS2話題列表
description: 在容器內使用查詢 topic 列表
version: 1.0.0
dependencies: [docker_est]
---

# 背景 / 運作原理
於指定容器內以互動式 shell（`bash -ic`）執行 `ros2 topic list`，確保容器的 `.bashrc`（含 ROS2 環境設定，如 `source /opt/ros/<distro>/setup.bash`）會被載入，否則會找不到 `ros2` 指令。

# 語法 / 參數規範
* `container_name` (string, required)：已啟動且已安裝 ROS2 的容器名稱。

# 執行步驟 (Steps)
1. 接收容器名稱。
2. 執行 `docker exec <container_name> bash -ic "ros2 topic list"`。
3. 回傳話題清單文字。

# 範例 (Examples)
* `EXECUTE: ROS2_topic_list_cmd.py ros_humble_image`

# 異常處理 (Edge Cases)
* 缺少容器名稱時回傳用法提示。
* 容器內未正確設定 ROS2 環境（`.bashrc` 未 source setup.bash）時會回傳 `[ERROR] 執行失敗: ...`（找不到 ros2 指令）。
