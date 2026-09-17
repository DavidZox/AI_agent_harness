---
type: Tool
title: 進入指定容器
description: 進入指定名稱的 container 中
version: 1.0.0
dependencies: [docker_est]
---

# 背景 / 運作原理
用於「確認可連線」到已存在的容器：會模糊比對 `docker ps -a` 中的容器名稱清單，找到名稱包含輸入字串者，再對其執行連線測試指令。**注意**：此工具僅回傳一次性連線確認結果（`pwd`／`whoami`），並不會維持一個持續的容器內 shell 階段——後續若要在容器內執行指令，仍須使用 `docker_runcmd`（每次呼叫都是獨立的 `docker exec`）。

# 語法 / 參數規範
* `container_name` (string, required)：容器名稱或其中一部分（支援模糊比對）。

# 執行步驟 (Steps)
1. 執行 `docker ps -a --format {{.Names}}` 取得所有容器名稱。
2. 找出名稱中包含輸入字串的第一個容器。
3. 對該容器執行 `docker exec <container> bash -c "echo '連線成功'; pwd; whoami"`。

# 範例 (Examples)
* `EXECUTE: docker_open_cmd.py ros_humble_image`

# 異常處理 (Edge Cases)
* 找不到名稱包含輸入字串的容器時回傳 `[ERROR] 在 Docker 中找不到名稱包含 '...' 的容器。請確認容器是否已啟動。`。
* 容器存在但未在執行狀態時，`docker exec` 會失敗並回傳 `[ERROR] 連線失敗: ...`。
