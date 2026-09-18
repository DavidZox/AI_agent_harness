---
type: Tool
title: 寫入經驗記憶
description: 寫入經驗記憶（長期記憶）。
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
將格式化的記憶內容附加寫入 `Memory.md`，並自動加上時間戳記。僅應於使用者明確要求時觸發（詳見 ROBOT_AGENT.md 記憶寫入協議）。

# 語法 / 參數規範
* `memory_content` (string, required): 已分類格式化的記憶內容，格式為 `[問題種類] | [問題描述] | [解決方法或結論]`。
* 核心腳本：`scripts/modify_memory_cmd.py`

# 執行步驟 (Steps)
1. 檢查內容是否為空。
2. 加上時間戳記並附加寫入 `Memory.md`（檔案不存在則自動建立）。
3. 回傳寫入結果確認。

# 範例 (Examples)
* 記錄偏好：`EXECUTE: modify_memory "偏好問題 | 使用者偏好繁體中文 | 後續回答優先使用繁體中文"`

# 異常處理 (Edge Cases)
* 內容為空時回傳 `[ERROR]`。
* 禁止寫入敏感資訊、重複內容或未確認推測（見 ROBOT_AGENT.md）。
