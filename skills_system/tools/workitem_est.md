---
type: Tool
title: 發送調度工作項目
description: 發送工作項目給調度系統時使用。
version: 1.1.0
dependencies: ["requests", "調度系統 API (localhost:8000)"]
---

# 背景 / 運作原理
以 HTTP POST 呼叫本機調度服務 `http://localhost:8000/plan_sequence`，將任務參數組裝為 JSON payload 送出。連線與回應皆有逾時，服務未啟動時會立刻回報而不會卡住。

# 語法 / 參數規範
* `robot_name` (string, required): 機器人名稱，如 `tb1`。
* `workstations` (string, required): 逗號分隔的站點清單，如 `a0,a8,a6,a4`，至少一個。
* `priority` (int, required): 優先權，必須是整數。
* `state` (string, required): 任務狀態，如 `duty`。
* `is_authored` (string, required): 是否已授權，如 `yes`。
* 核心腳本：`scripts/workitem_est_cmd.py`（5 個參數需依序、以空白分隔傳入，缺一不可）
* 逾時：建立連線 5 秒、等待回應 15 秒。

# 執行步驟 (Steps)
1. 依序解析 5 個必要參數並檢查格式（priority 需為整數、站點不可為空）。
2. 將站點字串拆分為列表並組裝 JSON payload。
3. POST 至調度系統（連線 5 秒／回應 15 秒逾時）並回傳結果。

# 範例 (Examples)
* 發送任務：`EXECUTE: scripts/workitem_est_cmd.py tb1 a0,a8,a6,a4 10 duty yes`

# 回傳格式
* 成功：`[PASS] 任務發送成功: {調度系統回傳的 JSON}`
* 失敗：`[ERROR] ...`，含原因說明。

# 異常處理 (Edge Cases)
* 參數不足、`priority` 不是整數、站點清單為空、`robot_name` 為空 → `[ERROR]` 並附用法，請修正參數後重送。
* 調度系統未啟動或位址錯誤 → `[ERROR] 無法連線到調度系統 ...`，請回報使用者啟動服務，不要重試。
* 5 秒內連不上 → `[ERROR] 連線調度系統逾時`；連上但 15 秒內沒回應 → `[ERROR] 調度系統回應逾時`。
* 回傳非 200 → `[ERROR] 調度系統回應 HTTP <code>，任務未被接受: <內容>`。
* 環境缺少 `requests` 套件 → `[ERROR] 缺少 requests 套件`。
