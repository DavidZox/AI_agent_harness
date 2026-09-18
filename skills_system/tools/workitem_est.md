---
type: Tool
title: 發送調度工作項目
description: 發送工作項目給調度系統時使用。
version: 1.0.0
dependencies: ["requests", "調度系統 API (localhost:8000)"]
---

# 背景 / 運作原理
以 HTTP POST 呼叫本機調度服務 `http://localhost:8000/plan_sequence`，將任務參數組裝為 JSON payload 送出。

# 語法 / 參數規範
* `robot_name` (string, required): 機器人名稱，如 `tb1`。
* `workstations` (string, required): 逗號分隔的站點清單，如 `a0,a8,a6,a4`。
* `priority` (int, required): 優先權數值。
* `state` (string, required): 任務狀態，如 `duty`。
* `is_authored` (string, required): 是否已授權，如 `yes`。
* 核心腳本：`scripts/workitem_est_cmd.py`（5 個參數需依序、以空白分隔傳入，缺一不可）

# 執行步驟 (Steps)
1. 依序解析 5 個必要參數。
2. 將站點字串拆分為列表並組裝 JSON payload。
3. POST 至調度系統並回傳結果。

# 範例 (Examples)
* 發送任務：`EXECUTE: workitem_est tb1 a0,a8,a6,a4 10 duty yes`

# 異常處理 (Edge Cases)
* 參數不足 5 個時腳本會直接印出用法並結束。
* 調度系統未啟動或連線異常時回傳連線異常訊息。
* 回傳非 200 狀態碼視為發送失敗。
