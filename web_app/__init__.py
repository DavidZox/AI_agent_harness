"""Web Console（2026-09-26 從 web_console.py 拆出）：純標準庫（不需要 flask／fastapi）的網頁介面。

設計原則：
- 重用 agent_core 的 SkillAgent 與共用函式，不重新定義任何核心規則。
- 畫面分兩欄：左邊「使用者 ↔ Agent 對話」；右邊「系統／工具回傳」（規格載入、腳本結果、💭 思考、AI 實際收到的內容）。
- slash 指令沿用 CLI 的字串（/auto on、/compress…），另有 /menu 列出全部指令。
- 即時串流：後端每完成一次推論或工具執行，就把該筆事件以 NDJSON（一行一個 JSON）寫回並 flush，前端邊收邊渲染；
  流程函式的 events 參數實際上是 EventStream，append 即送出。

模組：state（全域狀態）；flows（回合流程）與 commands（slash 指令）都只依賴 state；server（HTTP 與 main）把三者接起來；網頁在 static/index.html。
"""
