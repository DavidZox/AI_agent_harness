---
type: Tool
title: 切換工作目錄
description: 切換當前環境的工作目錄（CWD）。
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
當後續多項操作皆依賴於特定專案目錄，需要變更 CWD（Current Working Directory）狀態時調用。

# 語法 / 參數規範
* `path` (string, required): 欲切換到的目標工作目錄路徑（例如："/home/robot/workspace"）

# 執行步驟 (Steps)
1. 接收目標路徑。
2. 調用後端 `scripts/cd_cmd.py` 更新工作目錄。
3. 後端狀態管理持久化維護此 CWD 狀態。

# 範例 (Examples)
* 切換至機器人工作區：
  `EXECUTE: scripts/cd_cmd.py "/home/robot/workspace"`

# 異常處理 (Edge Cases)
* 若路徑不存在或無切換權限，狀態將保持不變，AI 應建立目錄或修正路徑。
