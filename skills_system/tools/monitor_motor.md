---
type: Tool
title: 動力馬達監測
description: 監測動力組件溫度，評估運作狀態
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
依輸入溫度值分級回報馬達運作狀態，用於快速判斷是否需要降速或停機。純數值運算，不讀取即時感測器。

# 語法 / 參數規範
* `temp` (number, required)：馬達溫度，例如 `75`。純數字（不含逗號）字串會自動轉為 float。

# 執行步驟 (Steps)
1. 接收溫度參數並嘗試轉型為數字。
2. 依下列門檻分級：
   * `> 80`：`[CRITICAL] 觸發物理邊界約束，請立即停機`
   * `>= 60`：`[WARNING] 負載異常，建議降速 30%`
   * `>= 20`：`[NORMAL] 運作正常`
   * `< 20`：`[NOTICE] 正在進行環境語義單元預熱`

# 範例 (Examples)
* `EXECUTE: monitor_motor_cmd.py 75`

# 異常處理 (Edge Cases)
* 缺少參數時回傳用法提示 `Usage: monitor_motor_cmd.py <temp>`。
* 非數值輸入若無法轉型，將以原始字串進入比較邏輯，可能導致非預期的比較結果，應確保呼叫時傳入單純數字。
