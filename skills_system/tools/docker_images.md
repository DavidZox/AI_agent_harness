---
type: Tool
title: 列出映像檔
description: 列出本機所有 Docker 映像檔（名稱、tag、大小、建立時間），可用關鍵字過濾。
version: 1.0.0
dependencies: ["docker"]
---

# 背景 / 運作原理
包裝 `docker images`，以表格列出本機每個映像檔的 repository、tag、ID、大小與建立時間，並統計總數與 `<none>` 懸空映像的數量。在使用 `docker_est` 建立容器之前，先用這個技能確認映像檔名稱存在於本機。

# 語法 / 參數規範
* `keyword` (string, optional): 以子字串過濾 repository 名稱或 tag（不分大小寫），只能給一個。
* 核心腳本：`scripts/docker_images_cmd.py`
* 逾時：15 秒。

# 執行步驟 (Steps)
1. 執行 `docker images --format ...`（15 秒逾時）。
2. 依 keyword 過濾，整理成對齊的表格。
3. 回傳 `[PASS]` 統計行 + 表格，或 `[ERROR]`。

# 範例 (Examples)
* 列出全部映像檔：`EXECUTE: scripts/docker_images_cmd.py`
* 找名稱含 rmf 的映像檔：`EXECUTE: scripts/docker_images_cmd.py rmf`

# 回傳格式
* 成功：`[PASS] 本機映像檔：共 N 個[，其中 K 個為 <none> 懸空映像...]` 換行後接表格，欄位為 `REPOSITORY  TAG  IMAGE ID  SIZE  CREATED`。
* 沒有符合的映像檔：`[PASS] 本機目前沒有任何映像檔...`（正常結果，不是錯誤）。
* 失敗：`[ERROR] 查詢映像檔清單 (docker images) 失敗: <原因>`。

# 異常處理 (Edge Cases)
* 不支援的選項或多個關鍵字 → `[ERROR]` 並附用法。
* 找不到 docker 指令／Docker daemon 未啟動／無權限 → `[ERROR]`，訊息會指出是哪一種，請回報使用者。
* 超過 15 秒無回應 → `[ERROR] ... 逾時`，代表 Docker daemon 無回應。
* `<none>` 的懸空映像無法用名稱啟動；`docker_est` 需要 REPOSITORY 名稱且不可含 `:` 或 `/`（它會拿映像檔名稱當容器名稱），tag 不是 latest 的映像檔目前無法直接用 `docker_est` 啟動。
