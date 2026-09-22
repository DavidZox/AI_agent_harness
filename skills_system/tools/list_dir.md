---
type: Tool
title: 查看目錄清單
description: 查看指定目錄下的檔案清單。
version: 1.2.0
dependencies: []
---

# 用途
以 `ls -laF` 列出目錄內容，自動過濾與預設旗標重複的參數。逾時 5 秒。

# 語法
`EXECUTE: scripts/ls_cmd.py [flags] [path]`
* `path`：預設目前工作目錄。`flags`：額外 ls 旗標（如 `-h`），會與預設合併。

# 範例
`EXECUTE: scripts/ls_cmd.py -la /opt/ros`
`EXECUTE: scripts/ls_cmd.py`

# 回傳
成功：`[PASS] 目錄列表 (<path>):` + 輸出。失敗：`[ERROR] 無法讀取目錄: <stderr>`。

# 異常
* 路徑不存在或無權限：`[ERROR]`，修正路徑。
* 逾時 5 秒：目錄過大或掛載裝置無回應，改指定較小的子目錄。
