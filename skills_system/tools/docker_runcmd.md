---
type: Tool
title: 容器內執行指令
description: 若要在容器內使用指令時使用。
version: 1.0.0
dependencies: ["docker"]
---

# 背景 / 運作原理
在指定容器內以 `bash -c "<command> && pwd"` 執行指令，並藉由附加的 `pwd` 取得容器內最新工作目錄，供 Agent 同步 `CONTAINER_CWD` 狀態。

# 語法 / 參數規範
* `container_name` (string, required): 目標容器名稱。
* `command` (string, required): 欲在容器內執行的指令（其餘參數會合併為一個指令字串）。
* 核心腳本：`scripts/docker_runcmd_cmd.py`

# 執行步驟 (Steps)
1. 組合 `docker exec <container_name> bash -c "<command> && pwd"`。
2. 執行並回傳標準輸出（含指令結果與最終路徑）。

# 範例 (Examples)
* 於容器內列出檔案：`EXECUTE: scripts/docker_runcmd_cmd.py ros2_humble "ls -la /opt/ros"`
* 於容器內建置：`EXECUTE: scripts/docker_runcmd_cmd.py ros2_humble "colcon build"`

# 異常處理 (Edge Cases)
* 指令執行失敗時回傳 `[ERROR]` 與 stderr。
* 容器不存在或未啟動會導致執行失敗。
