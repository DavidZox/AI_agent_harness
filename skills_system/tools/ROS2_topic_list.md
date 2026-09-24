---
type: Tool
title: ROS2 話題列表
description: 列出容器內目前的 topic，標頭算好總數；--filter 關鍵字只列符合的並計數（容器可省略＝目標容器）。
version: 1.4.0
dependencies: ["docker", "ros2"]
---

# 用途
在容器內執行 `ros2 topic list`（bash -ic，自動補齊 ROS2 環境與節點所在的 ROS_DOMAIN_ID）。逾時 30 秒。

# 語法
`EXECUTE: scripts/ROS2_topic_list_cmd.py [container_name] [--filter 關鍵字]...`
* `container_name`：可省略＝目前的目標容器（CURRENT_TARGET_CONTAINER）；要指定時給完整名稱。
* `--filter 關鍵字`：只列名稱含關鍵字的 topic（不分大小寫，可重複給多個），標頭給符合的數量。使用者問「有幾個／哪些 topic 跟 X 有關」時用它，數量直接引用標頭，不要自己數。

# 範例
`EXECUTE: scripts/ROS2_topic_list_cmd.py`（用目標容器）
`EXECUTE: scripts/ROS2_topic_list_cmd.py --filter costmap`
`EXECUTE: scripts/ROS2_topic_list_cmd.py ros2_humble --filter scan --filter odom`

# 回傳
成功：`[PASS] 容器 '<名稱>' 的 topic：共 N 個[，其中含「關鍵字」的 M 個]：` + 每行一個 topic（操作的容器與目標不同時末行附 `[TARGET_CONTAINER] <名稱>`）。沒有 topic：`[PASS] …目前沒有任何 topic`（不是錯誤）。失敗：`[ERROR] 原因`。

# 異常
* 沒有目標容器又沒給名稱：`[ERROR]` 提示先用 `docker_containers` 查、`docker_open` 選定。
* 容器不存在／未運行：先用 `docker_containers` 確認名稱與狀態。
* 找不到 ros2：容器未安裝 ROS2 或無 ROS_DISTRO，回報使用者。
* 逾時：ROS2 daemon 或 DDS 網路問題，回報使用者。
