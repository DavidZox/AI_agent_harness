---
type: Tool
title: ROS2 話題列表
description: 在容器內使用查詢 topic 列表。
version: 1.1.0
dependencies: ["docker", "ros2"]
---

# 背景 / 運作原理
以 `docker exec <container> bash -ic "ros2 topic list"` 在容器互動式 shell 中執行。若容器的 `.bashrc` 沒有載入 ROS2 環境、導致找不到 `ros2`，腳本會自動 fallback 載入 `/opt/ros/$ROS_DISTRO/setup.bash`（需容器內有設定 `ROS_DISTRO`）。

# 語法 / 參數規範
* `container_name` (string, required): 已啟動且安裝 ROS2 的容器完整名稱。
* 核心腳本：`scripts/ROS2_topic_list_cmd.py`
* 逾時：30 秒（容器內以 `timeout` 終止，宿主機端另有 +10 秒後盾）。

# 執行步驟 (Steps)
1. 組合 `docker exec <container> timeout 30 bash -ic "<ROS2 環境 fallback>; ros2 topic list"`。
2. 執行並回傳話題清單（每行一個 topic）。

# 範例 (Examples)
* 查詢話題列表：`EXECUTE: scripts/ROS2_topic_list_cmd.py ros2_humble`

# 回傳格式
* 成功：每行一個 topic 名稱。若沒有任何 topic，回傳 `[PASS] 指令執行成功，但沒有任何輸出（目前沒有任何 topic 被發布）。`
* 失敗：`[ERROR] ...`，含原因說明與 stderr。

# 異常處理 (Edge Cases)
* 未提供容器名稱 → `[ERROR]` 並附用法。
* 容器不存在／未運行 → `[ERROR]`，請先用 `docker_open` 確認名稱。
* 容器內找不到 `ros2`（ROS2 未安裝或無 `ROS_DISTRO`）→ `[ERROR] ...容器內找不到 ros2 指令...`。
* 超過 30 秒無回應 → `[ERROR] ... 逾時`，通常是 ROS2 daemon 或 DDS 網路設定問題，請回報使用者。
