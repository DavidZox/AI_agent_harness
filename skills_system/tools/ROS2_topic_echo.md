---
type: Tool
title: ROS2 話題內容（單筆或一段時間）
description: 讀取指定 topic 的一筆訊息，或用 --duration 擷取一段時間：欄位變化、頻率、--where 門檻判斷與全部原始訊息（容器可省略＝目標容器）。
version: 1.6.0
dependencies: ["docker", "ros2"]
---

# 用途
在容器內執行 `ros2 topic echo`。不加 `--duration`：`--once` 讀一筆就結束。加 `--duration 秒`：持續擷取這段時間的所有訊息，回報則數與頻率、每個欄位的變化（數值 min→max、字串有幾種值、固定不變的欄位），並附上全部原始訊息（篇幅過長時保留頭尾並註明省略幾則）。輸出多半超過工具回傳門檻，會由獨立 session 依你這一步的目的擷取重點，所以執行前在 reply 裡說清楚你想從這些訊息確認什麼。自動補齊 ROS2 環境與節點所在的 ROS_DOMAIN_ID。

# 語法
`EXECUTE: scripts/ROS2_topic_echo_cmd.py [container_name] <topic_name> [timeout_seconds] [--duration 秒] [--where 欄位<值]...`
* `container_name`：可省略＝目前的目標容器（CURRENT_TARGET_CONTAINER）；topic 以 `/` 開頭，系統據此分辨。
* `timeout_seconds`：單筆模式等第一則訊息的上限，預設 15，範圍 1～570；低頻 topic 請加大。
* `--duration 秒`（2～300）：使用者提到「觀察一段時間／持續／有沒有變化／頻率／幾秒」時使用；高頻 topic 請縮短秒數。
* `--where 欄位<值`（可重複，須搭配 `--duration`）：使用者問「有沒有低於／超過某值、第幾則開始、有幾則」時用它，由腳本算出幾則符合、第一則符合的序號與值。運算子 `< <= > >= == !=`；欄位用攤平後的名稱（`voltage`、`data.step_counter`、`pose.pose.position.x`）。結論直接引用該行，不要自己逐則比較。

# 範例
`EXECUTE: scripts/ROS2_topic_echo_cmd.py /odom --duration 10`（用目標容器）
`EXECUTE: scripts/ROS2_topic_echo_cmd.py /battery_state --duration 10 --where voltage<24`
`EXECUTE: scripts/ROS2_topic_echo_cmd.py ros2_humble /cmd_vel`
`EXECUTE: scripts/ROS2_topic_echo_cmd.py ros2_humble /diagnostics 60`

# 回傳
單筆：一筆訊息的 YAML。一段時間：`[PASS] 觀察 '<topic>' N 秒：收到 M 則（約 X Hz）` + 欄位變化清單 + 每個 `--where` 一行 `條件 voltage<24：M 則中 K 則符合；第一則符合 #k（voltage=…）；前一則 #k-1 不符合（…）；…` + 全部原始訊息；沒收到訊息也是 `[PASS]` 並說明可能原因。操作的容器與目標不同時末行附 `[TARGET_CONTAINER] <名稱>`。失敗：`[ERROR] 原因`。

# 異常
* 找不到 topic 或沒有 publisher：先用 `ROS2_topic_list` 確認名稱。
* 單筆逾時：確定是低頻 topic 才加大秒數重試一次，否則回報「目前沒有資料在發布」。
* 沒有目標容器又沒給名稱：`[ERROR]` 提示先用 `docker_containers` 查、`docker_open` 選定。
* 容器不存在／未運行／找不到 ros2：訊息會指出原因，回報使用者。
