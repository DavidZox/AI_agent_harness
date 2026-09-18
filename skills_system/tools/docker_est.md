---
type: Tool
title: 建立容器環境
description: 創造指定映像檔的 container。
version: 1.0.0
dependencies: ["docker"]
---

# 背景 / 運作原理
以背景模式（detached）啟動指定映像檔的容器，並用 `tail -f /dev/null` 讓容器保持存活，供後續 `docker_open` / `docker_runcmd` 使用。

# 語法 / 參數規範
* `image_name` (string, required): 映像檔名稱（同時作為容器名稱）。
* 核心腳本：`scripts/docker_est_cmd.py`

# 執行步驟 (Steps)
1. 以 `docker run -d --name <image_name> <image_name> tail -f /dev/null` 啟動容器。
2. 回傳啟動結果。

# 範例 (Examples)
* 建立容器：`EXECUTE: scripts/docker_est_cmd.py ros2_humble`

# 異常處理 (Edge Cases)
* 找不到 docker 指令時回傳 `[ERROR]`（請確認 Docker 是否安裝）。
* 容器同名已存在時啟動會失敗。
