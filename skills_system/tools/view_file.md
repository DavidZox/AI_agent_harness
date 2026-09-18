---
type: Tool
title: 查看檔案內容
description: 查看診斷報告、腳本或技能規格文件內容。
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
讀取指定檔案的純文字內容並回傳。這也是載入本索引系統中「工具詳細規格文件」（`tools/*.md`）的標準方式：當需要更完整的參數規範或範例時，先以此工具讀取對應的技能文件，再執行實際指令。

# 語法 / 參數規範
* `file_path` (string, required): 欲讀取的檔案路徑，可為相對路徑或絕對路徑。
* 核心腳本：`scripts/cat_cmd.py`
* 安全邊界：檔案大小超過 1MB 會拒絕讀取。

# 執行步驟 (Steps)
1. 展開並轉換為絕對路徑。
2. 確認檔案存在、非目錄、且未超過 1MB。
3. 讀取並回傳完整文字內容。

# 範例 (Examples)
* 查看設定檔：`EXECUTE: view_file config.yaml`
* 載入工具詳細規格：`EXECUTE: view_file /home/david/AI_agent_harness/skills_system/tools/docker_runcmd.md`

# 異常處理 (Edge Cases)
* 檔案不存在、是目錄、或超過 1MB 時回傳 `[ERROR]`。
