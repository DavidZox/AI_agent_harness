---
type: Tool
title: 列出容器
description: 列出目前主機上的所有容器（名稱、狀態、映像檔），可只看運行中或以關鍵字過濾。
version: 1.0.0
dependencies: ["docker"]
---

# 背景 / 運作原理
包裝 `docker ps -a`，以表格列出每個容器的名稱、狀態（`Up ...` 為運行中、`Exited ...` 為已停止）、來源映像檔與 ID，並在第一行統計總數與運行中數量。在使用 `docker_open` / `docker_runcmd` / ROS2 技能之前，先用這個技能取得**完整的容器名稱**，避免名稱猜錯或模糊比對到多個容器。

# 語法 / 參數規範
* `--running` (flag, optional): 只列出運行中的容器；預設包含已停止的。
* `keyword` (string, optional): 以子字串過濾容器名稱（不分大小寫），只能給一個。
* 核心腳本：`scripts/docker_containers_cmd.py`
* 逾時：15 秒。

# 執行步驟 (Steps)
1. 執行 `docker ps [-a] --format ...`（15 秒逾時）。
2. 依 keyword 過濾名稱，整理成對齊的表格。
3. 回傳 `[PASS]` 統計行 + 表格，或 `[ERROR]`。

# 範例 (Examples)
* 列出全部容器：`EXECUTE: scripts/docker_containers_cmd.py`
* 只看運行中：`EXECUTE: scripts/docker_containers_cmd.py --running`
* 找名稱含 rmf 的容器：`EXECUTE: scripts/docker_containers_cmd.py rmf`

# 回傳格式
* 成功：`[PASS] 容器（含已停止）：共 N 個，運行中 M 個` 換行後接表格，欄位為 `NAME  STATUS  IMAGE  CONTAINER ID`。
* 沒有符合的容器：`[PASS] 目前沒有任何容器（含已停止）...`（這是正常結果，不是錯誤）。
* 失敗：`[ERROR] 查詢容器清單 (docker ps) 失敗: <原因>`。

# 異常處理 (Edge Cases)
* 不支援的選項或多個關鍵字 → `[ERROR]` 並附用法。
* 找不到 docker 指令／Docker daemon 未啟動／無權限 → `[ERROR]`，訊息會指出是哪一種，請回報使用者。
* 超過 15 秒無回應 → `[ERROR] ... 逾時`，代表 Docker daemon 無回應。
* 容器狀態為 `Exited` 時，`docker_runcmd` 與 ROS2 技能會失敗，需先請使用者啟動該容器。
