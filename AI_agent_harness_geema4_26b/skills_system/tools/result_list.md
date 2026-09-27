---
type: Tool
title: 工具結果存檔索引
description: 列出最近的工具結果存檔（編號、時間、腳本、狀態、大小、當時的任務、摘要回答），先看這個再決定要搜哪一個。
version: 1.0.0
dependencies: []
---

# 用途
每次技能執行的完整原始輸出都由系統存成結果檔（`logs/tool_results/`），主對話只拿到摘要；使用者追問細節、或你想回查之前的輸出時，先用這個看有哪些存檔與編號，再用 `result_grep` 搜內容、`result_view` 看原文。不會重跑任何工具。

# 語法
`EXECUTE: scripts/result_list_cmd.py [N] [--all]`
* `N`：最近幾筆（預設 20，上限 100）。`--all`：連同之前 session 的存檔。

# 範例
`EXECUTE: scripts/result_list_cmd.py`　`EXECUTE: scripts/result_list_cmd.py 5`

# 回傳
`[PASS] 工具結果存檔：目前 session 共 K 筆…` + 每行 `#編號 | 時間 | 腳本 | 狀態 | 大小 | 檔名 | 任務：… | 回答：…`（新→舊）。沒有存檔也是 `[PASS]`。

# 異常
* 目前 session 沒有存檔：訊息會說之前 session 有幾筆，加 `--all` 可列。
