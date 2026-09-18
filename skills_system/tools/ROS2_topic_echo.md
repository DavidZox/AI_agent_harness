---
type: Tool
title: ROS2 話題詳細資訊
description: 指定 topic 名稱進行 echo 命令，獲得 topic 詳細資訊。
version: 1.0.0
dependencies: ["docker", "ros2"]
---

# 背景 / 運作原理
以 `ros2 topic echo <topic> --once` 讀取一筆訊息後即結束，避免持續佔用連線。

# 語法 / 參數規範
* `container_name` (string, required): 容器名稱。
* `topic_name` (string, required): 欲查詢的 topic 名稱。
* 核心腳本：`scripts/ROS2_topic_echo_cmd.py`

# 執行步驟 (Steps)
1. 組合 `docker exec <container> bash -ic "ros2 topic echo <topic> --once"`。
2. 執行並回傳單筆訊息內容。

# 範例 (Examples)
* 查看話題資料：`EXECUTE: ROS2_topic_echo ros2_humble /cmd_vel`

# 異常處理 (Edge Cases)
* topic 不存在或無資料發布時可能無輸出，且此工具無內建逾時保護，需留意可能卡住等待。
