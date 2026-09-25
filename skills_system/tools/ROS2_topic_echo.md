---
type: Tool
title: ROS2 話題內容（單筆或一段時間）
description: 讀取指定 topic 的一筆訊息，或用 --duration 擷取一段時間：欄位變化、頻率、--where 門檻判斷與全部原始訊息（容器可省略＝目標容器）。
version: 1.7.0
dependencies: ["docker", "ros2"]
---

# 用途
在容器內執行 `ros2 topic echo`（自動補齊 ROS2 環境）。不加 `--duration`：讀一筆就結束。加 `--duration 秒`：擷取這段時間的所有訊息，回報則數、頻率、每個欄位的變化（數值 min→max、字串有幾種值、固定不變的欄位）與全部原始訊息。一段時間的輸出多半超過門檻、會由獨立 session 依你這一步的目的擷取重點，所以執行前在 reply 說清楚想確認什麼。

# 語法
`EXECUTE: scripts/ROS2_topic_echo_cmd.py [container_name] <topic_name> [timeout_seconds] [--duration 秒] [--where 欄位<值]...`
* `container_name`：可省略＝目標容器；topic 以 `/` 開頭，系統據此分辨。
* `timeout_seconds`：單筆模式等第一則訊息的上限，預設 15（1～570）；低頻 topic 加大。
* `--duration 秒`（2～300）：使用者提到「觀察一段時間／持續／有沒有變化／頻率」時用；高頻 topic 縮短秒數。
* `--where 欄位<值`（可重複，須搭配 `--duration`）：問「有沒有低於／超過某值、第幾則開始、有幾則」時用，由腳本算出幾則符合、第一則符合的序號與值。運算子 `< <= > >= == !=`；欄位用攤平後的名稱（`voltage`、`data.step_counter`、`pose.pose.position.x`）。結論直接引用該行，不要自己逐則比較。

# 範例
`EXECUTE: scripts/ROS2_topic_echo_cmd.py /odom --duration 10`（用目標容器）
`EXECUTE: scripts/ROS2_topic_echo_cmd.py /battery_state --duration 10 --where voltage<24`
`EXECUTE: scripts/ROS2_topic_echo_cmd.py ros2_humble /diagnostics 60`（單筆，等 60 秒）

# 回傳
單筆：`[PASS] '<topic>' 的一筆訊息:` + YAML。一段時間：`[PASS] 觀察 '<topic>' N 秒：收到 M 則（約 X Hz）` + 欄位變化 + 每個 `--where` 一行 `條件 …：M 則中 K 則符合；第一則符合 #k（…）` + 原始訊息（`--- #序號`）；沒收到訊息也是 `[PASS]` 並說明可能原因。完整原始訊息已存成結果檔：使用者追問某一則或某欄位時用 `result_grep <編號> <關鍵字> --block`，不要重新擷取。失敗：`[ERROR] 原因`。

# 異常
* 找不到 topic 或沒有 publisher：先用 `ROS2_topic_list` 確認名稱。
* 單筆逾時：確定是低頻 topic 才加大秒數重試一次，否則回報「目前沒有資料在發布」。
* 沒有目標容器、容器不存在、找不到 ros2：訊息會指出原因，依提示處理。
