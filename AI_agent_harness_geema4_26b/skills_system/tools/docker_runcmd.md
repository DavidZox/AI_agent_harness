---
type: Tool
title: 容器內執行指令
description: 在指定容器（或目前的目標容器）內執行 shell 指令。
version: 1.4.0
dependencies: ["docker"]
---

# 用途
在容器內以 `bash -c "<command> && pwd"` 執行，輸出末行是執行後的容器內路徑。查看容器內的目錄、檔案、程序都用它（list_dir／view_file 只看本機）。

# 語法
`EXECUTE: scripts/docker_runcmd_cmd.py [--timeout 秒] [container_name] "<command>"`
* `--timeout`：放最前面，預設 120，範圍 1～570；長時間建置請加大。
* `container_name`：可省略＝目前的目標容器（CURRENT_TARGET_CONTAINER）；要指定時給完整名稱（不確定先用 `docker_containers` 查）。
* `command`：用引號包住；多個指令用 `&&` 串接。逾時會終止容器內程序。

# 範例
`EXECUTE: scripts/docker_runcmd_cmd.py "ls -la /opt/ros"`（用目標容器）
`EXECUTE: scripts/docker_runcmd_cmd.py ros2_humble "ls -la /opt/ros"`
`EXECUTE: scripts/docker_runcmd_cmd.py --timeout 540 ros2_humble "cd /ws && colcon build"`

# 回傳
成功：`[PASS] 容器 '<名稱>' 內 `<command>` 執行完成:` + 標準輸出（操作的容器與目前目標不同時末行附 `[TARGET_CONTAINER] <名稱>`，之後它就是目標）。失敗：`[ERROR] ... 失敗（exit code N）: 原因`；逾時：`[ERROR] ... 逾時`。

# 異常
* 容器不存在／未運行／docker 不可用：訊息會指出原因，勿原樣重試。
* 逾時且確定只是需要更多時間：加大 --timeout 重試一次；指令本身失敗：依 stderr 修正後再執行。
