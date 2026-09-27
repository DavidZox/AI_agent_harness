---
type: Tool
title: 在工具結果存檔裡搜關鍵字
description: 在指定編號（或 latest／all）的工具結果存檔裡搜關鍵字（a|b 同義詞、正則、不分大小寫），回命中行與前後幾行；使用者追問細節時用，不要重跑工具。
version: 1.1.0
dependencies: []
---

# 用途
使用者要找「某個字串／名稱出現在存檔哪幾行」時用（「有沒有出現 timeout」「DEFAULT_MODEL 定義在哪一行」「第 17 則長什麼樣」）：純文字比對，標頭給每個檔的命中數，直接引用；結果太長時系統會依使用者的問題擷取重點。不要重新執行觀察型工具。一般的追問（「那溫度正常嗎」「隨便挑一個分析」「預設用哪個模型」）改用 `result_recall`：把使用者的話交給獨立 session 回原文依問題提煉，不用自己想關鍵字。

# 語法
`EXECUTE: scripts/result_grep_cmd.py <編號|latest|all> <關鍵字> [-C 行數] [--max 命中上限] [--block]`
* 關鍵字：同義詞用 `|` 分隔（`error|fail|timeout`），可含正則，不分大小寫；含空白用引號。多個關鍵字一律用 `|` 連成一串，不要寫 `OR`。
* `-C`：命中行前後各幾行，預設 3（上限 10）。`--max`：命中上限，預設 40（上限 200）。
* `--block`：命中時回整則訊息（`ROS2_topic_echo` 存檔以 `--- #序號` 分隔每則）。`all`：目前 session 最近 20 個存檔。

# 範例
`EXECUTE: scripts/result_grep_cmd.py 16 "motor_rear_left|temperature"`
`EXECUTE: scripts/result_grep_cmd.py latest "voltage: 23\." --block`
`EXECUTE: scripts/result_grep_cmd.py all "error|fail" -C 2`

# 回傳
`[PASS] 在 F 個結果檔中搜「…」：共 K 行命中…` + 每個檔一段：`#編號 腳本（時間，狀態）：命中 K 行（原文共 L 行）` + `> L行號: 命中行`／`  L行號: 前後行`。0 命中也是 `[PASS]` 並建議同義詞。

# 異常
* `[ERROR] 找不到結果 #N`：用 `result_list` 確認編號。
* 關鍵字太長或比對逾時（複雜正則）：簡化關鍵字。
