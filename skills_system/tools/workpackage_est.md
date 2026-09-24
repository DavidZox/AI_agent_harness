---
type: Tool
title: 發送調度工作項目
description: 以 HTTP POST 發送任務給調度系統。
version: 1.2.0
dependencies: ["requests", "調度系統 API (localhost:8000)"]
---

# 用途
組裝 JSON payload POST 到 `http://localhost:8000/plan_sequence`。連線 5 秒、回應 15 秒逾時，服務未啟動會立刻回報。

# 語法
`EXECUTE: scripts/workitem_est_cmd.py <robot_name> <workstations> <priority> <state> <is_authored>`
* 5 個參數依序、空白分隔、缺一不可。`workstations` 逗號分隔（至少一站）；`priority` 必須是整數。

# 範例
`EXECUTE: scripts/workitem_est_cmd.py tb1 a0,a8,a6,a4 10 duty yes`

# 回傳
成功：`[PASS] 任務發送成功: {回傳 JSON}`。失敗：`[ERROR] 原因`。

# 異常
* 參數不足／priority 非整數／站點為空：修正參數重送。
* 無法連線或逾時：調度系統未啟動，回報使用者，勿重試。
* HTTP 非 200：任務未被接受，回報內容給使用者。
