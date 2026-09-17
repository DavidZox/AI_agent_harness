---
type: Tool
title: 切換工作目錄
description: 切換當前環境的工作目錄（CWD）
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
當後續多項操作皆依賴於特定專案目錄，需要變更 CWD（Current Working Directory）狀態時調用。切換結果由 `Agent_Runner.py` 透過 `[CWD_CHANGED]` 標記持久化維護，供後續所有工具呼叫的 `cwd` 使用。

# 語法 / 參數規範
* `path` (string, required)：欲切換到的目標工作目錄路徑（例如：`/home/robot/workspace`）。支援 `~` 展開。

# 執行步驟 (Steps)
1. 接收目標路徑，展開為絕對路徑。
2. 確認路徑存在且為目錄。
3. 回傳 `[CWD_CHANGED] <絕對路徑>`，由 `Agent_Runner.py` 讀取此標記並更新後端維護的 CWD 狀態。

# 範例 (Examples)
* 切換至機器人工作區：`EXECUTE: cd_cmd.py "/home/robot/workspace"`

# 異常處理 (Edge Cases)
* 若路徑不存在或無切換權限，狀態將保持不變，並回傳 `[ERROR] 找不到指定的環境語義單元（路徑不存在或非目錄）: ...`；Agent 應建議目錄或修正路徑。
