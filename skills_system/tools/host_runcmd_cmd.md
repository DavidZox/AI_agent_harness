---
type: Tool
title: 本機執行 Shell 指令
description: 在本機當前工作目錄（CWD）執行任意 bash 指令。
version: 1.0.0
dependencies: ["bash"]
---

# 用途
在本機系統執行通用 shell 指令（以 `bash -c "<command> && pwd"` 執行）。常用於檔案編譯、Git 操作、環境檢測、安裝套件或執行自訂腳本。輸出末行會自動附帶執行後的工作目錄。

# 語法
`EXECUTE: scripts/host_runcmd_cmd.py [--timeout 秒] "<command>"`
* `--timeout`：可選，放最前面，預設 120 秒，範圍 1～570 秒；長時間建置（如 `cmake --build` 或 `make`）請明確加大。
* `command`：要執行的 Shell 指令，請用雙引號包住；多個指令用 `&&` 串接。逾時會終止該程序。

# 範例
`EXECUTE: scripts/host_runcmd_cmd.py "ls -la"`
`EXECUTE: scripts/host_runcmd_cmd.py "git status && git log -n 3"`
`EXECUTE: scripts/host_runcmd_cmd.py --timeout 300 "mkdir -p build && cd build && cmake .. && make -j4"`

# 回傳
成功：`[PASS] 本機內 `<command>` 執行完成（末行為執行後的工作目錄）:` + 標準輸出 + 末行 `[HOST_CWD] <最新路徑>`（系統據此更新本機 CWD）。
失敗：`[ERROR] 本機執行 `<command>` 失敗（exit code N）: 原因`；逾時：`[ERROR] ... 逾時`。

# 異常
* 權限不足：訊息會包含 `Permission denied`，建議依提示修正權限或加上必要路徑。
* 指令不存在：請確認工具是否已安裝（如 `which cmake`）。
* 逾時且確定需要更多時間：加大 `--timeout` 重試一次。