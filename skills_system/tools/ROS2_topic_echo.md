---
type: Tool
title: ROS2 話題詳細資訊
description: 指定 topic 名稱進行 echo 命令，獲得 topic 詳細資訊。
version: 1.1.0
dependencies: ["docker", "ros2"]
---

# 背景 / 運作原理
以 `ros2 topic echo <topic> --once` 讀取一筆訊息後即結束。沒有 publisher 時 `--once` 會永遠等待，因此本工具一定帶逾時：容器內以 `timeout` 終止 echo 程序（不會留下孤兒程序），宿主機端另有 +10 秒後盾。若容器 shell 找不到 `ros2`，自動 fallback 載入 `/opt/ros/$ROS_DISTRO/setup.bash`。

# 語法 / 參數規範
* `container_name` (string, required): 容器完整名稱。
* `topic_name` (string, required): 欲查詢的 topic 名稱；不確定時先用 `ROS2_topic_list` 查。
* `timeout_seconds` (int, optional): 等待訊息的秒數，預設 15，範圍 1～570。低頻 topic（例如每分鐘才發一次）請加大。
* 核心腳本：`scripts/ROS2_topic_echo_cmd.py`

# 執行步驟 (Steps)
1. 解析參數與可選的逾時秒數。
2. 組合 `docker exec <container> timeout <秒數> bash -ic "ros2 topic echo <topic> --once"` 並執行。
3. 回傳單筆訊息內容，或 `[ERROR]`。

# 範例 (Examples)
* 查看話題資料（預設 15 秒）：`EXECUTE: scripts/ROS2_topic_echo_cmd.py ros2_humble /cmd_vel`
* 低頻話題等 60 秒：`EXECUTE: scripts/ROS2_topic_echo_cmd.py ros2_humble /diagnostics 60`

# 回傳格式
* 成功：一筆訊息的 YAML 內容。
* 逾時：`[ERROR] ... 逾時（超過 N 秒），容器內的程序已被終止。` 並說明最可能的原因（沒有 publisher／名稱錯誤／QoS 不相容）。
* 其他失敗：`[ERROR] ...`，含原因說明與 stderr。

# 異常處理 (Edge Cases)
* 參數不足或 `timeout_seconds` 不是 1～570 的整數 → `[ERROR]` 並附用法。
* topic 不存在或目前沒有任何 publisher → `[ERROR] ...找不到指定的 topic 或目前沒有任何 publisher...`（ros2 無法判斷型別），請先用 `ROS2_topic_list` 確認。
* topic 存在但在逾時內沒有訊息 → `[ERROR] ... 逾時`。若確定該 topic 是低頻的，加大第三個參數重試一次；否則回報使用者「目前沒有資料在發布」。
* 容器不存在／未運行／容器內找不到 `ros2` → `[ERROR]`，訊息會指出是哪一種。
