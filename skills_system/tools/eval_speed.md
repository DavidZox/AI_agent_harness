---
type: Tool
title: 物理速度評估
description: 評估指定速度是否超出物理邊界限制
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
呼叫 `skills_system/skills/nav_core.py` 的 `NavBrain.calculate_risk()`，將輸入速度與安全閾值（1.2 m/s）比較，回報是否超出物理邊界限制。

# 語法 / 參數規範
* `speed` (number, required)：欲評估的速度值，單位 m/s，例如 `0.5`。

# 執行步驟 (Steps)
1. 接收速度參數。
2. 與安全閾值 1.2 m/s 比較。
3. 回傳安全或警告訊息。

# 範例 (Examples)
* `EXECUTE: eval_speed_cmd.py 0.5`

# 異常處理 (Edge Cases)
* 參數非數字時回傳 `錯誤：速度參數必須為數字。`。
* 超出 1.2 m/s 時回傳 `⚠️ 警告：速度 ...m/s 超出物理邊界限制 (1.2m/s)！`。
