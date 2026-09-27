---
type: Tool
title: 任務清單（自己規劃、不需要使用者核准）
description: 多步驟任務先列出步驟清單，之後每一輪都附在最後面，做到哪一步一眼就看得到；可更新、勾完成、記失敗。
version: 1.0.0
dependencies: []
---

# 用途
任務要連續做 3 個以上動作、或中間可能失敗要換路線時，先用這個把步驟列出來。清單存在系統裡（不是對話內容），之後每一輪都附在送給你的內容最後面（`[→]` 是現在這一步），不會因為對話變長或被壓縮而忘記原本要做什麼。不需要使用者核准；使用者用 `/plan on` 核准的計畫也會變成同一份清單。進度：harness 模式下，步驟文字裡寫的技能執行成功，系統會自動打勾；claude_code 模式由你自己用 `done` 勾。單一動作就能完成的任務不用列。

# 語法
`EXECUTE: scripts/plan_task_cmd.py set "<步驟1>" "<步驟2>" …`（重列＝整份換掉；也可以一個字串裡用 `｜` 分隔）
`EXECUTE: scripts/plan_task_cmd.py add "<步驟>"`　`EXECUTE: scripts/plan_task_cmd.py done <編號>[,<編號>]`
`EXECUTE: scripts/plan_task_cmd.py fail <編號> "<原因>"`　`EXECUTE: scripts/plan_task_cmd.py clear`　`EXECUTE: scripts/plan_task_cmd.py show`
* 每一步寫「技能名稱：要做什麼；失敗→怎麼辦」。技能名稱用 SKILLS.md 裡的名稱（harness 模式靠它自動打勾）；失敗的處理三選一或組合：修正參數重試一次、換一個技能查清楚、停下來回報使用者——不要只寫「重試」。
* 需要反覆查的步驟（搜尋、觀察一段時間）寫清楚最多幾次、每次調整什麼、超過怎麼辦。
* 會改變系統狀態的步驟（workpackage_send、workpackage_cancel、docker_est…）照常列，執行時系統會請使用者確認。
* 最多 12 步，每步 120 字以內。

# 範例
`EXECUTE: scripts/plan_task_cmd.py set "docker_containers：確認 rmf_sim 在跑；沒在跑→回報使用者" "ROS2_topic_echo：讀 /fleet_states_json 一筆看各車電量；失敗→ROS2_topic_list 確認 topic 名稱再讀一次" "ROS2_node_info：看 /task_agent 是否在線；失敗→回報" "整理結果回報使用者"`
`EXECUTE: scripts/plan_task_cmd.py done 2`
`EXECUTE: scripts/plan_task_cmd.py fail 3 "node 不存在"`

# 回傳
`[PASS]` 加更新後的清單（`[✓]` 完成、`[✗]` 失敗、`[→]` 現在這一步、`[ ]` 還沒做）。不會執行任何東西、不產生存檔。

# 異常
* 沒有步驟、編號超出範圍、或動作不認得：`[ERROR]` 附用法。
* 使用者換了新任務、舊清單不適用：用 `set` 重列或 `clear`。全部完成後，使用者送出下一個新任務時清單自動清除。
