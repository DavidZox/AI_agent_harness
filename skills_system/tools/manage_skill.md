---
type: Tool
title: 建立新技能（自我進化）
description: 當現有技能組合無法完成任務時，動態建立新技能（腳本 + 規格書 + 索引三者同步生成）
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
當 Agent 發現系統缺乏某項功能且無法用現有 CLI 組合完成時，可呼叫本工具即時建立新技能。呼叫後會同步產生三個產物：
1. `skills_system/scripts/<名稱>_cmd.py`：實際執行的腳本（自動包上單參數 `execute()` 外殼與數值自動轉型防護）。
2. `skills_system/tools/<名稱>.md`：新技能自己的規格書（依本檔案相同的樣板自動產生，內容為佔位描述，建議事後人工補完）。
3. `skills_system/INDEX.md`：新增一列「自我進化」分類的索引項目，指向上述規格書。

# 語法 / 參數規範
* 唯一參數為一段以 `|` 分隔、恰好 4 個欄位的字串：`名稱 | 描述 | 參數名 | 程式碼主體`。
* **代碼撰寫規範（核心指令）**：
  1. 嚴禁使用 `def` 關鍵字或自訂函數名稱，只需撰寫函數體內的邏輯主體（系統會自動包上 `def execute(參數名):` 外殼）。
  2. 系統會自動將傳入參數定義為同名變數；若參數為列表字串（如 `"1,2,3"`），需自行對變數執行 `.split(',')`。
  3. 多行邏輯必須使用字面 `\n` 換行，並在其後以 4 個空格代表 Python 縮排層級。

# 執行步驟 (Steps)
1. 依 `名稱 | 描述 | 參數名 | 程式碼主體` 格式組合輸入字串。
2. 呼叫 `EXECUTE: manage_skill_cmd.py <名稱> | <描述> | <參數名> | <程式碼主體>`。
3. 確認回傳訊息包含腳本、規格書、索引三者皆已建立。
4. 後續即可對新技能先 `NEED_TOOL: <名稱>` 載入規格書，再 `EXECUTE: <名稱>_cmd.py <參數>` 執行。

# 範例 (Examples)
* 單行邏輯：`EXECUTE: manage_skill_cmd.py calc_battery | 電量計算 | voltage | return "High" if float(voltage) > 100 else "Low"`
* 多行迴圈：`EXECUTE: manage_skill_cmd.py check_list | 批次檢查 | val | data_list = [float(x) for x in val.split(',')]\nfor x in data_list:\n    if x > 100: return "ALERT"\nreturn "SAFE"`

# 異常處理 (Edge Cases)
* 輸入缺少 `|` 分隔符或欄位不足 4 個時回傳錯誤，不會建立任何檔案。
* 若同名技能已在 `INDEX.md` 中存在，索引不會重複新增該列，但腳本檔案仍會被覆寫更新。
* 新技能的規格書內容為自動產生的佔位文字，未經人工審核，實際行為以腳本原始碼為準。
