---
type: Tool
title: 進入指定容器
description: 進入指定名稱的 container 中，確認連線狀態。
version: 1.1.0
dependencies: ["docker"]
---

# 背景 / 運作原理
在現有容器清單（含未運行的）中找出真實容器名稱，再以 `docker exec` 執行 `pwd` 與 `whoami` 驗證連線是否成功。名稱比對順序：完全相同 → 唯一的子字串符合；若有多個容器都包含輸入字串，會回報候選清單而不會猜第一個。

# 語法 / 參數規範
* `container_name` (string, required): 容器名稱或其中一部分（模糊比對）。
* 核心腳本：`scripts/docker_open_cmd.py`
* 逾時：查詢容器清單 15 秒、exec 驗證連線 15 秒。

# 執行步驟 (Steps)
1. 執行 `docker ps -a --format {{.Names}}` 取得所有容器名稱（15 秒逾時）。
2. 比對名稱：完全相同優先；否則取唯一包含輸入字串者；多個符合則回報 `[ERROR]` 並列出候選。
3. 對真實名稱執行 `docker exec ... bash -c "echo 連線成功 && pwd && whoami"`（15 秒逾時）。
4. 回傳連線結果。

# 範例 (Examples)
* 進入容器：`EXECUTE: scripts/docker_open_cmd.py ros2_humble`

# 回傳格式
* 成功：`[PASS] 已成功連線至 '<真實名稱>'！` 後接 `連線成功`、容器內目前路徑、使用者名稱各一行。
* 失敗：`[ERROR] ...`，含原因說明。

# 異常處理 (Edge Cases)
* 未提供名稱 → `[ERROR]` 並附用法。
* 找不到名稱包含輸入字串的容器 → `[ERROR]`，訊息會列出目前現有的容器名稱（最多 10 個）供比對。
* 多個容器名稱都包含輸入字串 → `[ERROR]` 列出全部候選，請改用更完整的名稱重試。
* 容器存在但未運行 → `[ERROR] ...存在但未在運行中`，請先啟動容器。
* 找不到 docker 指令／daemon 未啟動／無權限 → `[ERROR]`，訊息會指出是哪一種。
* 逾時（daemon 無回應）→ `[ERROR] ... 逾時`。
