---
type: Tool
title: ROS2 節點列表
description: 在容器內使用查詢 node 列表。
version: 1.1.0
dependencies: ["docker", "ros2"]
---

# 背景 / 運作原理
以 `docker exec <container> bash -ic "ros2 node list"` 取得目前運行中的節點清單。若容器 shell 找不到 `ros2`，自動 fallback 載入 `/opt/ros/$ROS_DISTRO/setup.bash`。

# 語法 / 參數規範
* `container_name` (string, required): 容器完整名稱。
* 核心腳本：`scripts/ROS2_node_list_cmd.py`
* 逾時：30 秒（容器內以 `timeout` 終止，宿主機端另有 +10 秒後盾）。

# 執行步驟 (Steps)
1. 組合並執行 `ros2 node list`（30 秒逾時）。
2. 回傳節點清單。

# 範例 (Examples)
* 查詢節點列表：`EXECUTE: scripts/ROS2_node_list_cmd.py ros2_humble`

# 回傳格式
* 成功：每行一個 node 名稱（含命名空間）。若沒有任何 node 在運行，回傳 `[PASS] 指令執行成功，但沒有任何輸出（目前沒有任何 node 在運行）。`——這是正常結果，不是錯誤。
* 失敗：`[ERROR] ...`，含原因說明與 stderr。

# 異常處理 (Edge Cases)
* 未提供容器名稱 → `[ERROR]` 並附用法。
* 容器不存在／未運行／容器內找不到 `ros2` → `[ERROR]`，訊息會指出是哪一種。
* 超過 30 秒無回應 → `[ERROR] ... 逾時`。
