---
type: Tool
title: 查看目錄清單
description: 查看指定環境語義單元（目錄）下的檔案清單。
version: 1.1.0
dependencies: []
---

# 背景 / 運作原理
呼叫系統內建的 `ls -laF` 取得目錄內容，並自動過濾掉與預設旗標重複的參數（如 -l -a -F），避免指令組合錯誤。

# 語法 / 參數規範
* `path` (string, optional): 欲查看的目錄路徑，預設為當前工作目錄。
* `flags` (string, optional): 額外的 ls 旗標（例如 `-h`），會與預設旗標合併而非重複疊加。
* 核心腳本：`scripts/ls_cmd.py`

# 執行步驟 (Steps)
1. 接收參數字串並以 shlex 拆分。
2. 過濾重複旗標，組合成最終 `ls -laF [flags] [path]` 指令。
3. 以 5 秒逾時執行並回傳標準輸出。

# 範例 (Examples)
* 查看 ROS 套件目錄：`EXECUTE: scripts/ls_cmd.py -la /opt/ros`
* 查看當前目錄：`EXECUTE: scripts/ls_cmd.py`

# 回傳格式
* 成功：`[PASS] 目錄列表 (<path>):` 後接 `ls -laF` 的輸出。
* 失敗：`[ERROR] ...`。

# 異常處理 (Edge Cases)
* 路徑不存在或無權限時回傳 `[ERROR] 無法讀取目錄: <stderr>`。
* 超過 5 秒回傳 `[ERROR] 目錄列表逾時（超過 5 秒）...`，通常是目錄過大或位於無回應的網路／掛載裝置，請改指定較小的子目錄。
