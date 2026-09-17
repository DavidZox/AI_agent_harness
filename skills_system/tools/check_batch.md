---
type: Tool
title: 批次數值檢查
description: 接收逗號分隔的數值字串，檢查是否有數值超過限制 100
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
用於一次性批次檢查多筆數值（例如多顆感測器讀數）是否有任一筆超出安全限制 100，任一筆超標即整批判定為異常。

# 語法 / 參數規範
* `data_list` (string, required)：逗號分隔的數值字串，例如 `98,102,45`。

# 執行步驟 (Steps)
1. 接收逗號分隔字串，依逗號拆分並逐一轉為 float。
2. 逐筆檢查是否大於 100。
3. 若任一筆超過 100，回傳 `[ALERT] 偵測到物理邊界突破，請立即手動介入`；否則回傳 `[PASS] 全數值正常`。

# 範例 (Examples)
* `EXECUTE: check_batch_cmd.py 98,102,45`

# 異常處理 (Edge Cases)
* 缺少參數時回傳用法提示 `Usage: check_batch_cmd.py <data_list>`。
* 若字串中含非數值項目，拆分轉型時會拋出例外並以非 0 狀態碼結束。
