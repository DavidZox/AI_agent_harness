# 文件導覽

| 文件 | 給誰看 | 內容 |
|---|---|---|
| [架構說明](架構說明.md) | 想看懂或修改核心的人 | `Agent_Runner.py` 由哪幾塊組成、一回合怎麼跑、工具回傳管線、狀態與持久化、CLI／Web 對照、常數、「要改什麼看哪裡」 |
| [設計筆記與已知限制](設計筆記與已知限制.md) | 想知道某個機制為什麼長這樣的人 | 每個機制的做法、理由與實測數據；完整技能清單；驗證方法；已知限制；未來方向（2026-09-26 從 README 移來） |
| [長期願景](長期願景.md) | 關心延伸方向的人 | AGV/AMR 車隊調度場景的構想（尚未實作） |

## 圖

| 圖 | 來源 | 內容 |
|---|---|---|
| ![架構](images/AI_agent_harness的說明.png) | `AI_agent_harness的說明.puml` | 元件與資料流 |
| ![一回合](images/AI_agent_harness的狀態轉移.png) | `AI_agent_harness的狀態轉移.puml` | 一回合的狀態機 |
| ![工具回傳管線](images/AI_agent_harness的工具回傳管線.png) | `AI_agent_harness的工具回傳管線.puml` | 原文存檔 → 摘要 → 檢索清單 → result_recall |

改完 `.puml` 後重新產生 PNG（純標準庫，透過 PlantUML 官方伺服器，需要網路）：

```bash
python3 doc/流程圖產生器.py                 # 全部
python3 doc/流程圖產生器.py 某張圖.puml     # 只轉一張
```
