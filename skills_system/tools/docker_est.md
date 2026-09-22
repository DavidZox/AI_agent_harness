---
type: Tool
title: 建立容器環境
description: 創造指定映像檔的 container。
version: 1.1.0
dependencies: ["docker"]
---

# 背景 / 運作原理
以背景模式（detached）啟動指定映像檔的容器，並用 `tail -f /dev/null` 讓容器保持存活，供後續 `docker_open` / `docker_runcmd` 使用。容器名稱即為映像檔名稱。

# 語法 / 參數規範
* `image_name` (string, required): 映像檔名稱（同時作為容器名稱，因此不可含 `:` 或 `/`）。
* 核心腳本：`scripts/docker_est_cmd.py`
* 逾時：120 秒。本機已有映像檔時數秒即完成；需從 registry 拉取時較久，超過即中止並回報。

# 執行步驟 (Steps)
1. 以 `docker run -d --name <image_name> <image_name> tail -f /dev/null` 啟動容器（120 秒逾時）。
2. 成功回傳 `[PASS]` 與容器 ID；失敗回傳 `[ERROR]` 與原因說明。

# 範例 (Examples)
* 建立容器：`EXECUTE: scripts/docker_est_cmd.py ros2_humble`

# 回傳格式
* 成功：`[PASS] 容器 '<name>' 已在背景啟動（ID: xxxxxxxxxxxx）。`
* 失敗：`[ERROR] 以映像檔 '<name>' 建立容器 失敗（exit code N）: <原因>`，常見原因已翻成中文說明，後面附上 docker 原始 stderr。

# 異常處理 (Edge Cases)
* 未提供映像檔名稱 → `[ERROR]` 並附用法。
* 找不到 docker 指令／Docker daemon 未啟動／無權限 → `[ERROR]`，訊息會指出是哪一種，請回報使用者，不要重試。
* 本機沒有該映像檔且無法拉取 → `[ERROR] ...本機沒有這個映像檔...`，請確認名稱或請使用者先 pull。
* 同名容器已存在 → `[ERROR] ...同名容器已存在`，此時應改用 `docker_open` 直接進入，不要再重試建立。
* 映像檔名稱含 `:` 或 `/` → `[ERROR]`，容器名稱不允許這些字元。
* 超過 120 秒 → `[ERROR] ... 逾時`，可能是 daemon 無回應或映像檔拉取過慢。
