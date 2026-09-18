---
type: Tool
title: 進入指定容器
description: 進入指定名稱的 container 中，確認連線狀態。
version: 1.0.0
dependencies: ["docker"]
---

# 背景 / 運作原理
以模糊比對方式（子字串）在現有容器清單中尋找真實容器名稱，再以 `docker exec` 執行 `pwd` 與 `whoami` 驗證連線是否成功。

# 語法 / 參數規範
* `container_name` (string, required): 容器名稱或其中一部分（模糊比對）。
* 核心腳本：`scripts/docker_open_cmd.py`

# 執行步驟 (Steps)
1. 執行 `docker ps -a` 取得所有容器名稱，尋找包含輸入字串的真實名稱。
2. 對真實名稱執行 `docker exec ... bash -c "echo 連線成功; pwd; whoami"`。
3. 回傳連線結果。

# 範例 (Examples)
* 進入容器：`EXECUTE: docker_open ros2_humble`

# 異常處理 (Edge Cases)
* 找不到名稱包含輸入字串的容器時回傳 `[ERROR]`。
* exec 失敗（容器未啟動等）時回傳 `[ERROR]` 與 stderr 訊息。
