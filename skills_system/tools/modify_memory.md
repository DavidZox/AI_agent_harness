---
type: Tool
title: 寫入經驗記憶
description: 寫入經驗記憶（全域常駐，或綁定特定技能按需載入）。
version: 2.1.0
dependencies: []
---

# 用途
把使用者明確要求記住的經驗寫入記憶：不加參數寫入全域 `Memory.md`（每輪常駐 system prompt）；加 `--skill <技能名稱>` 寫入該技能的經驗記憶，只在載入該技能規格時出現。載入某技能規格之後、執行它時才用得到的（參數、前置條件、曾發生的錯誤）綁技能；選技能的時候就要知道的規則（例如「list_dir 不能看容器內」）、通用原則、溝通風格、專案經驗寫全域。不要寫某個時間點的狀態（「系統整體健康」這類），它會過期。

# 語法
`EXECUTE: scripts/modify_memory_cmd.py "問題種類 | 問題描述 | 解決方法或結論" [--skill 技能名稱]`（技能名稱須為 SKILLS.md 裡的名稱）
`EXECUTE: scripts/modify_memory_cmd.py --move-last-to-skill 技能名稱`：把最新一則全域記憶改綁到該技能（寫入全域後，回傳請你問使用者要不要改綁、使用者同意時才用）

# 範例
`EXECUTE: scripts/modify_memory_cmd.py "偏好問題 | 使用者偏好繁體中文 | 後續回答優先使用繁體中文"`
`EXECUTE: scripts/modify_memory_cmd.py "錯誤執行 | stt_engine 缺少 faster-whisper | 先安裝該模組再重試" --skill stt_engine`

# 回傳
`[PASS]` 含寫入位置、內容與條目數；相同內容已存在時 `[PASS]` 提示未重複寫入；技能條目過多時附整併提醒。全域寫入的內容剛好提到一個技能時，回傳附一段 ℹ️：照它用一句話問使用者要不要改綁，不要自己決定。

# 異常
內容為空、技能名稱不存在、或 `--skill modify_memory`（工具本身不是記憶主題）回傳 `[ERROR]`。禁止寫入敏感資訊、過長 log、未確認推測。
