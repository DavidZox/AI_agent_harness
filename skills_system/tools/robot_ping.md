---
type: Tool
title: 核心運維檢查
description: 檢查 V4.9 機器人核心與語義層狀態
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
呼叫 `skills_system/skills/nav_core.py` 的 `NavBrain.get_v49_status()`，回傳一段固定的核心狀態描述文字，用於快速確認核心引擎與語義拓撲是否對齊正常。

# 語法 / 參數規範
* 不需任何參數。

# 執行步驟 (Steps)
1. 直接呼叫，不需參數。
2. 回傳固定格式的狀態字串。

# 範例 (Examples)
* `EXECUTE: robot_ping_cmd.py`

# 異常處理 (Edge Cases)
* 這是靜態狀態回報（非即時感測），僅表示核心程式邏輯可正常載入與呼叫，不代表實體機器人硬體狀態。
