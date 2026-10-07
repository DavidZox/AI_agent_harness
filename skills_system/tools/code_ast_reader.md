---
type: Tool
title: AST 程式碼結構化閱讀
description: 利用 AST (Abstract Syntax Tree) 解析 Python 檔案，提取 Class、Function/Method 簽名、Docstring 與繼承關係，免去讀取全文的 Token 消耗。
version: 1.0.0
dependencies: ["python3"]
---

# 用途
在不讀取整個 Python 檔全文的情況下，快速分析目標 `.py` 檔案的結構。透過 AST 解析輸出模組層級的 Docstring、類別定義、繼承關係、函式/方法簽名（含參數與型別標註）以及常數變數，幫助 Agent 快速掌握程式碼藍圖。逾時 15 秒。

# 語法
`EXECUTE: scripts/code_ast_reader_cmd.py <file_path> [--no-docstrings] [--private]`
* `<file_path>`：目標 Python 檔案路徑（相對或絕對路徑）。
* `--no-docstrings`：可選。隱藏 Docstring 說明說明，僅印出結構與簽名。
* `--private`：可選。包含底線開頭的私有函式與私有方法（預設會隱藏私有成員以保持精簡）。

# 範例
* 讀取檔名為 `scripts/ROS2_node_info_cmd.py` 的結構：
  `EXECUTE: scripts/code_ast_reader_cmd.py scripts/ROS2_node_info_cmd.py`
* 讀取結構但不顯示 Docstring：
  `EXECUTE: scripts/code_ast_reader_cmd.py scripts/ROS2_node_info_cmd.py --no-docstrings`
* 讀取結構並包含私有函式/方法：
  `EXECUTE: scripts/code_ast_reader_cmd.py scripts/ROS2_node_info_cmd.py --private`

# 回傳
成功：`[PASS] 成功解析 <file_path> 的 AST 結構` + 結構樹狀圖。失敗：`[ERROR] 原因`。

# 異常
* 檔案不存在或副檔名不是 `.py`：回傳錯誤提示。
* 檔案存在語法錯誤（SyntaxError）：AST 解析失敗並回傳語法錯誤行號與原因。