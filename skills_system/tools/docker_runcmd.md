---
type: Tool
title: 容器內執行指令
description: 若要在容器內使用指令時使用。
version: 1.1.0
dependencies: ["docker"]
---

# 背景 / 運作原理
在指定容器內以 `bash -c "<command> && pwd"` 執行指令。輸出的最後一行是指令執行完後容器內的工作目錄（目前僅供 AI 參考，尚未自動同步到系統的 `CONTAINER_CWD` 狀態）。

逾時採兩層設計：容器內以 coreutils `timeout` 包住指令，逾時會真正終止容器內的程序（不會留下孤兒程序）；宿主機端另有 +10 秒的後盾處理 Docker daemon 本身無回應的情況。若容器內沒有 `timeout` 指令會自動退回只用宿主機端逾時。

# 語法 / 參數規範
* `--timeout <秒數>` (int, optional): 必須放在最前面。預設 120 秒，範圍 1～570 秒（上限低於系統總逾時 600 秒）。長時間建置／安裝請明確加大。
* `container_name` (string, required): 目標容器名稱（需完整名稱；不確定時先用 `docker_containers` 查看）。
* `command` (string, required): 欲在容器內執行的指令，建議用引號包成一個參數；多個指令請用 `&&` 串接（被逾時終止的指令回傳非零，後面的指令就不會繼續執行）。
* 核心腳本：`scripts/docker_runcmd_cmd.py`

# 執行步驟 (Steps)
1. 解析可選的 `--timeout`，再取容器名稱與指令。
2. 組合 `docker exec <container> timeout -k 2 <秒數> bash -c "<command> && pwd"` 並執行。
3. 成功回傳標準輸出（含指令結果與最終路徑）；失敗回傳 `[ERROR]` 與原因、exit code、部分輸出。

# 範例 (Examples)
* 於容器內列出檔案：`EXECUTE: scripts/docker_runcmd_cmd.py ros2_humble "ls -la /opt/ros"`
* 長時間建置（指定 10 分鐘內的逾時）：`EXECUTE: scripts/docker_runcmd_cmd.py --timeout 540 ros2_humble "cd /ws && colcon build"`

# 回傳格式
* 成功：指令的標準輸出，最後一行為容器內 `pwd`。若指令沒有任何輸出則回傳 `[PASS] 指令執行成功，但沒有任何輸出（...）`。
* 指令失敗：`[ERROR] 容器 '<name>' 內的 \`<command>\` 失敗（exit code N）: <stderr 或原因說明>`，若有部分標準輸出會附在 `--- 部分標準輸出 ---` 之後（只保留結尾 20 行）。
* 逾時：`[ERROR] ... 逾時（超過 N 秒），容器內的程序已被終止。` 並提示可用 `--timeout` 加大。

# 異常處理 (Edge Cases)
* 參數不足或 `--timeout` 不是 1～570 的整數 → `[ERROR]` 並附用法。
* 容器不存在／未運行／docker 不可用 → `[ERROR]`，訊息會指出是哪一種，不要原樣重試。
* 指令逾時 → 容器內程序已被終止；若確定只是需要更多時間，用 `--timeout` 加大後重試一次即可。
* 指令本身失敗（非零 exit code）→ 依 `[ERROR]` 內的 stderr 修正指令，不要重複執行同一指令。
