---
type: Tool
title: 建立容器環境
description: 創造指定映像檔的 container
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
以背景模式（detached）啟動一個 Docker 容器，並用 `tail -f /dev/null` 讓容器保持存活不退出，供後續 `docker_open`／`docker_runcmd`／ROS2 系列工具使用。

# 語法 / 參數規範
* `image_name` (string, required)：映像檔名稱（例如 `ros:humble`）。
* 容器名稱固定等於映像檔名稱（`--name <image_name>`），目前不支援自訂容器名稱。

# 執行步驟 (Steps)
1. 接收映像檔名稱。
2. 執行 `docker run -d --name <image_name> <image_name> tail -f /dev/null`。
3. 回傳容器啟動結果。

# 範例 (Examples)
* `EXECUTE: docker_est_cmd.py ros_humble_image`

# 異常處理 (Edge Cases)
* 缺少映像檔名稱時回傳 `[ERROR] 請提供一個有效的映像檔 (image) 名稱。`。
* 找不到 `docker` 指令時回傳 `[ERROR] 找不到 docker 指令，請確認是否已安裝 Docker。`。
* 若同名容器已存在，`docker run` 本身會失敗並回傳對應錯誤訊息。
