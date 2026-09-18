---
type: Tool
title: 批次數值檢查
description: 接收逗號分隔的數值字串，檢查是否有數值超過限制 100。
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
將逗號分隔的字串轉為浮點數列表，逐一檢查是否超過物理邊界限制 100。

# 語法 / 參數規範
* `data_list` (string, required): 逗號分隔的數值字串，如 `98,102,45`。
* 核心腳本：`scripts/check_batch_cmd.py`

# 執行步驟 (Steps)
1. 依逗號拆分並轉為浮點數列表。
2. 逐一檢查是否有數值 `> 100`。

# 範例 (Examples)
* 批次檢查：`EXECUTE: check_batch 98,102,45`

# 異常處理 (Edge Cases)
* 任一數值超過 100 立即回傳 `[ALERT]` 並中止檢查。
* 輸入含非數值字元時會拋出轉型例外。
