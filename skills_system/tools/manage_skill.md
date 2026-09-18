---
type: Tool
title: 自我進化 / 建立新技能
description: 當現有工具組合無法完成任務時，動態建立新的 CLI 技能並註冊至技能索引。
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
呼叫 `manager.SkillManager.create_skill()`，依模板產生新的 `scripts/<name>_cmd.py`，並將新技能同時註冊進技能索引（`SKILLS.md`）與其詳細規格文件（`tools/<name>.md`）。

# 語法 / 參數規範
* 輸入格式：`名稱 | 描述 | 參數名 | 程式碼主體`（以 `|` 分隔，四個欄位缺一不可）。
* 程式碼主體撰寫規範：禁止使用 `def`，多行需以 `\n` 換行並手動加 4 空格縮排（詳見 ROBOT_AGENT.md 自我進化協議）。
* 核心腳本：`scripts/manage_skill_cmd.py`

# 執行步驟 (Steps)
1. 以 `|` 拆分輸入為名稱、描述、參數、程式碼四個欄位。
2. 產生新的 `_cmd.py` 腳本檔案。
3. 於 `SKILLS.md` 新增一行索引項目，並自動產生對應的 `tools/<name>.md` 規格文件。

# 範例 (Examples)
* 建立電量計算技能：
  `EXECUTE: scripts/manage_skill_cmd.py calc_battery | 電量計算 | voltage | v = float(voltage)\nreturn "Low" if v < 20 else "OK"`

# 異常處理 (Edge Cases)
* 輸入缺少 `|` 分隔符或欄位不足 4 個時回傳格式錯誤訊息。
* 若技能名稱已存在於索引中，不會重複寫入。
* 新工具的規格文件為自動生成的骨架，正式大量使用前建議以 `view_file` 檢視並視需要人工補完。
