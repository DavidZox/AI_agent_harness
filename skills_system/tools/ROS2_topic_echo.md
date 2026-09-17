---
type: Tool
title: ROS2話題詳細
description: 指定 topic 名稱進行 echo 命令，獲得 topic 詳細資訊
version: 1.0.0
dependencies: [docker_est]
---

# 背景 / 運作原理
於指定容器內執行 `ros2 topic echo <topic> --once`，僅讀取一筆訊息即結束（避免無限阻塞）。若需要持續監聽，須改寫腳本移除 `--once`。

# 語法 / 參數規範
* `container_name` (string, required)：容器名稱。
* `topic_name` (string, required)：欲查詢的 ROS2 topic 名稱（例如 `/odom`）。

# 執行步驟 (Steps)
1. 接收容器名稱與 topic 名稱。
2. 執行 `docker exec <container_name> bash -ic "ros2 topic echo <topic_name> --once"`。
3. 回傳該筆訊息內容。

# 範例 (Examples)
* `EXECUTE: ROS2_topic_echo_cmd.py ros_humble_image /odom`

# 異常處理 (Edge Cases)
* 缺少任一必要參數時回傳用法提示 `用法: python3 scripts/ROS2_topic_echo_cmd.py <container_name> <topic_name>`。
* topic 不存在或當下無發布者時，指令可能逾時等待，需視 ROS2 執行環境而定。
