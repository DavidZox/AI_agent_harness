---
type: Tool
title: 容器內執行指令
description: 若要在容器內使用指令時使用
version: 1.0.0
dependencies: [docker_est]
---

# 背景 / 運作原理
在指定容器內執行任意 shell 指令（每次呼叫皆為獨立的 `docker exec`，不維持狀態），並在指令後自動附加 `&& pwd`，方便確認容器內當前所在目錄。

# 語法 / 參數規範
* `container_name` (string, required)：目標容器名稱。
* `cmd_name` (string, required)：欲在容器內執行的完整指令（可包含空白，會原樣組合）。

# 執行步驟 (Steps)
1. 接收容器名稱與指令字串。
2. 組合為 `<cmd_name> && pwd`。
3. 執行 `docker exec <container_name> bash -c "<組合後指令>"`。

# 範例 (Examples)
* `EXECUTE: docker_runcmd_cmd.py ros_humble_image "ls -la /opt/ros"`

# 異常處理 (Edge Cases)
* 缺少必要的兩個參數時回傳用法提示 `用法: docker_run.py <container_name> <command>`。
* 容器不存在或指令執行失敗時回傳 `[ERROR] 執行失敗: ...`。
