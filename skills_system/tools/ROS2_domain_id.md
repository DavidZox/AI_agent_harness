---
type: Tool
title: ROS2 Domain ID 查詢與修改
description: 查詢或修改指定容器（或目標容器）內的 ROS_DOMAIN_ID 環境變數。
version: 1.0.0
dependencies: ["docker"]
---

# 用途
在指定的 Docker 容器內查看目前的 `ROS_DOMAIN_ID` 環境變數值，或將其設定/修改為新的 Domain ID（介於 0 到 232 之間的整數）。若省略容器名稱，預設使用目前的目標容器。逾時 15 秒。

# 語法
`EXECUTE: scripts/ROS2_domain_id_cmd.py [container_name] [new_domain_id]`
* `container_name`：可省略＝目前的目標容器。
* `new_domain_id`：可選。若不填則為**查詢**；若填入整數（0–232）則為**修改**。

# 範例
* 查詢目標容器的 ROS_DOMAIN_ID：
  `EXECUTE: scripts/ROS2_domain_id_cmd.py`
* 查詢指定容器 `ros2_humble` 的 ROS_DOMAIN_ID：
  `EXECUTE: scripts/ROS2_domain_id_cmd.py ros2_humble`
* 將目標容器的 ROS_DOMAIN_ID 修改為 42：
  `EXECUTE: scripts/ROS2_domain_id_cmd.py 42`
* 將指定容器 `ros2_humble` 的 ROS_DOMAIN_ID 修改為 42：
  `EXECUTE: scripts/ROS2_domain_id_cmd.py ros2_humble 42`

# 回傳
* 查詢成功：`[PASS] 容器 <名稱> 當前的 ROS_DOMAIN_ID 為: <ID>`（若未設定則顯示預設值 `0 (未設定)`）。
* 修改成功：`[PASS] 成功將容器 <名稱> 的 ROS_DOMAIN_ID 修改為: <ID>`。
* 失敗：`[ERROR] 原因`。

# 異常
* 容器不存在或未運行：訊息會指出原因，請先使用 `docker_containers` 確認狀態。
* 輸入非有效整數或超出 0~232 範圍：回傳錯誤提示並要求重新輸入合法 Domain ID。