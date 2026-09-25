---
type: Tool
title: 把追問交給獨立 session 重新理解存檔原文
description: 使用者的追問跟前面的工具回傳有關，但你想不出（或想錯）能命中原文的精確關鍵字時，把問題原封不動交給獨立 session 重新讀那份存檔的完整原文並針對這個新問題擷取重點；不是自己比對文字。
version: 1.0.0
dependencies: []
---

# 用途
`result_grep` 要你自己想出能命中原文的關鍵字（純文字比對，同義詞要自己用 `|` 列出）；`result_ask` 反過來，把使用者的問題原文交給另一個獨立 session，讓它重新讀那份存檔的完整原文、針對這個問題擷取重點——語意相近但字面不同的內容它答得出來，`result_grep` 找不到。答得出來時優先用 `result_grep`（比較快、比較省）；只有在你想不出精確關鍵字、或使用者的話明顯是隱性追問（沒有重複前面出現過的名詞）時才用這個。不會重跑工具，也不會修改任何東西。

# 語法
`EXECUTE: scripts/result_ask_cmd.py <編號|latest> "<使用者的問題原文>"`
* 編號：跟 `result_grep`／`result_view` 一樣，`result_list` 給的編號或 `latest`。
* 問題：把使用者的話原封不動放進去（可以稍微去掉語氣詞），不要自己先改寫成關鍵字——改寫成關鍵字就直接用 `result_grep` 了。

# 範例
`EXECUTE: scripts/result_ask_cmd.py 16 "那溫度正常嗎"`
`EXECUTE: scripts/result_ask_cmd.py latest "剛剛那個機器人後來去哪一站了"`

# 回傳
`[tool result - 任務導向摘要]` 開頭，第二行註明「針對追問「…」重新擷取存檔 #編號」，其餘跟工具摘要格式相同：回答／相關事實／錯誤／未涵蓋。跟其他技能一樣會記軌跡、產生新的存檔編號（可以再對這個新編號用 `result_grep`／`result_view`）。

# 異常
* `[ERROR] 找不到結果 #N`：用 `result_list` 確認編號。
* 沒有附問題內容：`[ERROR]`，補上問題文字。
