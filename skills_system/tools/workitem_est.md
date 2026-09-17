---
type: Tool
title: 發送調度工作
description: 發送工作項目給調度系統時使用
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
將工作項目以 HTTP POST 方式送至調度系統（FastAPI 服務 `/plan_sequence` 端點，預設 `http://localhost:8000`）。開發測試時可先啟動 `skills_system/scripts/mock_server.py` 模擬該端點。

# 語法 / 參數規範
* `robot_name` (string, required)：機器人名稱，例如 `tb1`。
* `workstations` (string, required)：逗號分隔的站點清單，例如 `a0,a8,a6,a4`。
* `priority` (int, required)：優先權數值，例如 `10`。
* `state` (string, required)：任務狀態，例如 `duty`。
* `is_authored` (string, required)：是否已授權，例如 `yes`。
* 五個參數皆為必填，且需依序提供。

# 執行步驟 (Steps)
1. 依序接收 5 個參數。
2. 將 `workstations` 依逗號拆分為列表。
3. 組裝 JSON payload 並以 POST 送至調度系統端點。
4. 回傳送出結果（成功／失敗／連線異常）。

# 範例 (Examples)
* `EXECUTE: workitem_est_cmd.py tb1 a0,a8,a6,a4 10 duty yes`

# 異常處理 (Edge Cases)
* 參數不足 5 個時回傳用法提示。
* 調度系統未啟動或連線失敗時回傳 `🚨 連線異常: ...`；可先以 `mock_server.py` 驗證流程。
* 調度系統回傳非 200 狀態碼時回傳 `❌ 發送失敗 (Status ...): ...`。
