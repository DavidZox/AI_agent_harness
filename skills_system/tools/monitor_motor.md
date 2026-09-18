---
type: Tool
title: 動力馬達監測
description: 監測動力組件溫度，評估運作狀態。
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
依據溫度數值分級回傳運作狀態，作為現場診斷的簡易規則引擎。

# 語法 / 參數規範
* `temp` (float, required): 馬達溫度（攝氏）。
* 核心腳本：`scripts/monitor_motor_cmd.py`

# 執行步驟 (Steps)
1. 嘗試將輸入轉為浮點數。
2. 依區間回傳對應等級：`>80` CRITICAL、`60~80` WARNING、`20~60` NORMAL、`<20` NOTICE。

# 範例 (Examples)
* 檢查馬達溫度：`EXECUTE: monitor_motor 75`

# 異常處理 (Edge Cases)
* 溫度超過 80 度時回傳 `[CRITICAL]`，Agent 應建議立即停機。
