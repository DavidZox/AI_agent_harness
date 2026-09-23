---
type: Tool
title: 寫入經驗記憶
description: 寫入經驗記憶（全域常駐，或綁定特定技能按需載入）。
version: 2.0.0
dependencies: []
---

# 用途
把使用者明確要求記住的經驗寫入記憶，兩種目標：不加參數寫入全域 `Memory.md`（每輪常駐 system prompt）；加 `--skill <技能名稱>` 寫入該技能的經驗記憶，只在載入該技能規格時一併出現。內容與某技能的用法、參數、前置條件、曾發生的錯誤有關就綁技能；通用原則、技能之間的取捨、溝通風格、專案經驗寫全域。

# 語法
`EXECUTE: scripts/modify_memory_cmd.py "問題種類 | 問題描述 | 解決方法或結論" [--skill 技能名稱]`
技能名稱須為 SKILLS.md 裡的名稱。

# 範例
* 全域：`EXECUTE: scripts/modify_memory_cmd.py "偏好問題 | 使用者偏好繁體中文 | 後續回答優先使用繁體中文"`
* 綁技能：`EXECUTE: scripts/modify_memory_cmd.py "錯誤執行 | stt_engine 缺少 faster-whisper | 先安裝該模組再重試" --skill stt_engine`

# 回傳
`[PASS]` 含寫入位置、內容與該技能目前的條目數；相同內容已存在時 `[PASS]` 提示未重複寫入；技能條目過多時附整併提醒。

# 異常
內容為空、技能名稱不存在、或 `--skill modify_memory`（工具本身不是記憶主題）回傳 `[ERROR]`。禁止寫入敏感資訊、過長 log、未確認推測（見 AGENT.md）。
