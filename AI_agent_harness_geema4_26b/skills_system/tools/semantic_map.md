---
type: Tool
title: 語義地圖與目前佈局
description: 站點代號、語意名稱、說明、路段，並對應目前機器人與 work package 在哪；也可只列站點或機器人。
version: 1.1.0
dependencies: ["fih_rmf_system web_console (port 8020)"]
---

# 用途
web_console `GET /api/topology`（拓譜圖 + semantics.yaml 的語意名稱、說明、座標、路段）加一幀快照：每站標出誰在此、誰前往中、哪個 work package 派工中。永遠是 web_console 目前載入的場域。發送任務前想確認站點名稱、或使用者問「某站是什麼／誰在那裡」時用。

# 語法
`EXECUTE: scripts/semantic_map_cmd.py [關鍵字或站點代號] [--stations] [--robots]`
* 不帶參數：全部站點（代號=語意名稱）與目前有機器人／工作包的站點。帶關鍵字或代號：該站／路段的完整說明、目前狀況、連接路段（含空白請用引號）。
* `--stations`：只列站點清單（含語意名稱）；`--robots`：只列機器人清單。

# 範例
`EXECUTE: scripts/semantic_map_cmd.py`　`EXECUTE: scripts/semantic_map_cmd.py 加工線通道`　`EXECUTE: scripts/semantic_map_cmd.py a3`　`EXECUTE: scripts/semantic_map_cmd.py --robots`

# 回傳
`[PASS] 語義地圖：N 站、M 邊…` + 清單或該站詳情。`--stations`／`--robots`：`[PASS] 目前場域的…`。失敗 `[ERROR] 原因`。

# 異常
* 找不到關鍵字：訊息列出現有站點，改用代號或其他關鍵字。即時快照讀取失敗時仍回地圖，只是沒有機器人／工作包位置。
* 無法連線／404：web_console 未啟動或版本不含 /api/topology，回報使用者。
