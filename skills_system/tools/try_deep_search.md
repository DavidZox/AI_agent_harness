---
type: Tool
title: 搜尋並進入技能目錄
description: 當使用者要求尋找特定關鍵字相關的技能資料夾，並進入該資料夾進行後續操作時使用。
version: 0.1.0
dependencies: []
source: make_skill 2026-09-24 22:58（組合技能，步驟來自實際操作軌跡；可直接編輯）
---

# 用途
此技能用於先列出當前目錄內容，接著搜尋包含特定關鍵字的檔案，最後切換到找到的技能目錄，以集中處理相關文件。
依序執行 4 個既有技能的腳本，任一步回 `[ERROR]` 即停止並回報該步原因：
1. 列出當前目錄的內容，了解檔案結構。（list_dir：`scripts/ls_cmd.py {search_path}`）
2. 搜尋包含特定關鍵字的檔案，定位目標技能文件。（find_file：`scripts/find_file_cmd.py {search_keyword} {search_path}`）
3. 切換到找到的技能目錄，準備後續操作。（change_dir：`scripts/cd_cmd.py {target_dir}`）
4. 再次列出當前目錄的內容，確認已進入正確的技能目錄。（list_dir：`scripts/ls_cmd.py {search_path}`）

# 語法
`EXECUTE: scripts/try_deep_search_cmd.py <search_keyword> <search_path> <target_dir>`
* `search_keyword`：需要搜尋的關鍵字。（例：`skill`）
* `search_path`：開始搜尋的目錄路徑。（例：`.`）
* `target_dir`：需要切換進入的目標目錄路徑。（例：`AI_agent_harness/skills_system/`）

# 範例
`EXECUTE: scripts/try_deep_search_cmd.py skill . AI_agent_harness/skills_system/`

# 回傳
成功：`[PASS] try_deep_search 完成 4/4 步` 加各步驟輸出（標明步驟編號）。 成功列出目錄內容，並順利切換到目標技能目錄後，再次列出目錄內容確認結構。
失敗：`[ERROR] ... 在第 k/N 步失敗` 加該步原因，之前步驟的輸出保留供診斷；依原因修正參數，不要原樣重試。

# 異常
* 如果搜尋關鍵字過於籠統，可能會找到太多不相關的檔案，需要仔細檢查輸出結果。
* 切換目錄時，請確保提供的路徑是正確的，否則後續的檔案操作將會失敗。
* 各步驟的參數規則與其他異常見底層技能的規格：change_dir、find_file、list_dir。
