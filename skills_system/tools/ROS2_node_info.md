---
type: Tool
title: ROS2 節點詳細資訊
description: 指定 node 名稱進行 info 命令，獲得 node 詳細資訊。
version: 1.1.0
dependencies: ["docker", "ros2"]
---

# 背景 / 運作原理
以 `ros2 node info <node>` 取得指定節點的訂閱、發布、服務與動作介面清單。若容器 shell 找不到 `ros2`，自動 fallback 載入 `/opt/ros/$ROS_DISTRO/setup.bash`。

# 語法 / 參數規範
* `container_name` (string, required): 容器完整名稱。
* `node_name` (string, required): 欲查詢的節點名稱，需含完整命名空間（例如 `/nav_node`）；不確定時先用 `ROS2_node_list` 查。
* 核心腳本：`scripts/ROS2_node_info_cmd.py`
* 逾時：30 秒（容器內以 `timeout` 終止，宿主機端另有 +10 秒後盾）。

# 執行步驟 (Steps)
1. 組合 `docker exec <container> timeout 30 bash -ic "ros2 node info <node>"`。
2. 執行並回傳節點詳細資訊。

# 範例 (Examples)
* 查詢節點資訊：`EXECUTE: scripts/ROS2_node_info_cmd.py ros2_humble /nav_node`

# 回傳格式
* 成功：`ros2 node info` 的原始輸出（Subscribers / Publishers / Service Servers / ... 區段）。
* 失敗：`[ERROR] ...`，含原因說明與 stderr。

# 異常處理 (Edge Cases)
* 參數不足 → `[ERROR]` 並附用法。
* 節點不存在 → `[ERROR] ...找不到指定的 ROS2 節點...`，請先用 `ROS2_node_list` 確認名稱（含 `/` 前綴），不要猜名稱重試。
* 容器不存在／未運行／容器內找不到 `ros2` → `[ERROR]`，訊息會指出是哪一種。
* 超過 30 秒無回應 → `[ERROR] ... 逾時`。
