---
type: Tool
title: 進入指定容器
description: 找出容器真實名稱並驗證連線。
version: 1.2.0
dependencies: ["docker"]
---

# 用途
在所有容器中比對名稱（完全相同優先，否則唯一的子字串符合），再以 `docker exec` 執行 `pwd`、`whoami` 驗證。多個容器都符合時不猜，回報候選清單。逾時各 15 秒。

# 語法
`EXECUTE: scripts/docker_open_cmd.py <container_name>`（可為部分名稱）

# 範例
`EXECUTE: scripts/docker_open_cmd.py ros2_humble`

# 回傳
成功：`[PASS] 已成功連線至 '<真實名稱>'！` + 容器內路徑與使用者。失敗：`[ERROR] 原因`。

# 異常
* 找不到：訊息列出現有容器名稱；完整清單用 `docker_containers`。
* 多個符合：依候選清單改用更完整的名稱重試。
* 容器未運行／docker 不可用／逾時：回報使用者。
