---
type: Tool
title: 系統內建技能
description: 查看指定環境語義單元（目錄）下的檔案清單
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
包裝系統 `ls -laF`，用於查看目前或指定目錄下的檔案與子目錄清單，是最基礎的環境探索工具。

# 語法 / 參數規範
* `args` (string, optional)：欲附加的 `ls` 參數與/或路徑，例如 `-la /opt/ros`。
* 若輸入包含 `-l`、`-a`、`-f`、`-F` 旗標，會被自動去除以避免與內建的 `-laF` 重複。

# 執行步驟 (Steps)
1. 接收參數字串，以 `shlex` 拆分。
2. 過濾掉與內建旗標重複的部分，組合成 `ls -laF [其餘旗標] [路徑]`。
3. 以 5 秒逾時執行並回傳結果。

# 範例 (Examples)
* `EXECUTE: ls_cmd.py -la /opt/ros`
* `EXECUTE: ls_cmd.py .`

# 異常處理 (Edge Cases)
* 找不到目錄或無權限時回傳 `[ERROR] 無法讀取目錄: ...`。
* 執行逾時或其他例外時回傳 `[ERROR] 執行異常: ...`。
