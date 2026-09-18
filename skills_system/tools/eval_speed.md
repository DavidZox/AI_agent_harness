---
type: Tool
title: 物理速度評估
description: 評估指定速度是否超出物理邊界限制。
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
呼叫 `NavBrain.calculate_risk(speed)`，將輸入速度與安全門檻 1.2 m/s 比較。

# 語法 / 參數規範
* `speed` (float, required): 欲評估的速度值（m/s）。
* 核心腳本：`scripts/eval_speed_cmd.py`

# 執行步驟 (Steps)
1. 將輸入轉為浮點數。
2. 與安全門檻 1.2 m/s 比較並回傳結果。

# 範例 (Examples)
* 評估速度：`EXECUTE: eval_speed 0.5`

# 異常處理 (Edge Cases)
* 輸入非數字時回傳「錯誤：速度參數必須為數字」。
* 超過 1.2 m/s 僅回傳警告訊息，不會自動阻斷，需 Agent 依此自行決策。
