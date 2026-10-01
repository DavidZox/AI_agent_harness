---
type: Tool
title: 寫入經驗記憶
description: 寫入長期記憶（Memory.md，每輪常駐 system prompt）。
version: 3.0.0
dependencies: []
---

# 用途
把使用者明確要求記住的經驗寫入長期記憶 `Memory.md`，之後每一輪的 system prompt 都會帶著它：適合通用原則、選技能的規則（例如「list_dir 不能看容器內」）、某個技能的用法或前置條件、溝通風格、專案經驗。不要寫某個時間點的狀態（「系統整體健康」這類），它會過期。

# 語法
`EXECUTE: scripts/modify_memory_cmd.py "問題種類 | 問題描述 | 解決方法或結論"`（只有記憶內容，沒有任何選項）

# 範例
`EXECUTE: scripts/modify_memory_cmd.py "偏好問題 | 使用者偏好繁體中文 | 後續回答優先使用繁體中文"`
`EXECUTE: scripts/modify_memory_cmd.py "錯誤執行 | stt_engine 缺少 faster-whisper | 先安裝該模組再重試"`

# 回傳
`[PASS]` 含寫入的內容與時間；相同內容已存在時 `[PASS]` 提示未重複寫入。

# 異常
內容為空、或帶了任何選項（例如 `--xxx`）回傳 `[ERROR]`。禁止寫入敏感資訊、過長 log、未確認推測。
