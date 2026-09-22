---
type: Tool
title: 列出映像檔
description: 列出本機所有 Docker 映像檔。
version: 1.1.0
dependencies: ["docker"]
---

# 用途
包裝 `docker images`，列出 repository、tag、ID、大小、建立時間，並統計 `<none>` 懸空映像數。建立容器前先用它確認映像檔存在。逾時 15 秒。

# 語法
`EXECUTE: scripts/docker_images_cmd.py [keyword]`
* `keyword`：過濾 repository 或 tag（子字串、不分大小寫），最多一個。

# 範例
`EXECUTE: scripts/docker_images_cmd.py`
`EXECUTE: scripts/docker_images_cmd.py rmf`

# 回傳
成功：`[PASS] 本機映像檔：共 N 個...` + 表格（REPOSITORY / TAG / IMAGE ID / SIZE / CREATED）。沒有符合：`[PASS] ...沒有任何映像檔`（不是錯誤）。失敗：`[ERROR] 原因`。

# 異常
* docker 不可用／daemon 未啟動／逾時：回報使用者。
* `<none>` 懸空映像無法用名稱啟動；`docker_est` 需要不含 `:` 或 `/` 的 REPOSITORY 名稱。
