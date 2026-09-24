---
type: Tool
title: 建立容器環境
description: 以指定映像檔建立並啟動容器。
version: 1.3.0
dependencies: ["docker"]
---

# 用途
`docker run -d --name <image_name> <image_name> tail -f /dev/null` 讓容器在背景常駐，容器名稱即映像檔名稱。逾時 120 秒（需從 registry 拉取時較久）。

# 語法
`EXECUTE: scripts/docker_est_cmd.py <image_name>`
* `image_name`：不可含 `:` 或 `/`（會當容器名稱）。先用 `docker_images` 確認存在。

# 範例
`EXECUTE: scripts/docker_est_cmd.py ros2_humble`

# 回傳
成功：`[PASS] 容器 '<name>' 已在背景啟動（ID: ...），並設為目前的目標容器`（末行 `[TARGET_CONTAINER] <name>`，之後容器技能可省略名稱）。失敗：`[ERROR] ... 失敗（exit code N）: 原因`。

# 異常
* 同名容器已存在：改用 `docker_open` 進入，勿重試建立。
* 映像檔不存在且無法拉取：用 `docker_images` 確認名稱。
* docker 不可用／daemon 未啟動／逾時：回報使用者。
