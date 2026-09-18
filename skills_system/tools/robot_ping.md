---
type: Tool
title: 核心運維檢查
description: 檢查 V4.9 機器人核心與語義層狀態。
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
呼叫 `NavBrain.get_v49_status()` 回傳機器人核心引擎與導航語義層的靜態健康狀態文字。

# 語法 / 參數規範
* 無參數。
* 核心腳本：`scripts/robot_ping_cmd.py`

# 執行步驟 (Steps)
1. 初始化 `NavBrain`。
2. 回傳狀態字串。

# 範例 (Examples)
* 檢查核心狀態：`EXECUTE: robot_ping`

# 異常處理 (Edge Cases)
* `skills/nav_core.py` 載入失敗時會拋出例外並由外層印出錯誤訊息。
