# AI_agent_harness

以本地 Ollama 模型（預設 `gemma4:e4b`）為核心的機器人維運助理，提供 CLI 與 Web 兩種介面。核心想法是**模型不寫程式、只挑技能**：可用的能力拆成一份輕量索引，加上一批規格文件與既有腳本；模型需要時才載入某個技能的規格，再依規格標明的腳本路徑執行。操作對象包含宿主機檔案系統、Docker 容器與容器內的 ROS2，以及 fih_rmf_system 的任務調度 web_console。決策模型刻意只用本地模型（離線的工廠環境）。

## 目前狀態（2026-09）

- **可用**：CLI 與 Web Console；26 個技能（檔案系統、容器與 ROS2、調度系統、多模態、工具結果存檔、記憶）；Plan 模式；`/make_skill` 把做對的步驟編成組合技能；Web 附圖的視覺分析；上下文自動壓縮；工具回傳原文存檔、跨 session 的工具使用檢索清單與 `result_recall`。
- **驗證方式**：假 `ollama.chat` 的 stub 測試，加上 `gemma4:e4b` 真模型情境；專案內還沒有正式的測試套件。
- **主要限制**：小模型偶爾不下 `action` 或自己編結果；比較、計數要靠腳本先算好；檢索清單「跟哪一筆有關」仍由小模型判斷。完整清單見〈[設計筆記與已知限制](doc/設計筆記與已知限制.md#6-已知限制)〉。

## 運作方式

![架構圖](doc/images/AI_agent_harness的說明.png)

1. 使用者下指令，主 session 每輪回一個 JSON `{thought, reply, action}`，harness **只看 `action`**，`reply` 裡的文字不會被執行。
2. `action` 填技能名稱 → 回傳該技能的規格文件（這一輪不執行）；填規格標明的腳本路徑 → 才真的執行。沒有「技能名稱 → 腳本」的對照表，模型一定要先讀規格。
3. 每次執行的完整原文都存成 `#編號` 並顯示給使用者。回傳超過 500 tokens 時，交給一次性的獨立 session，依使用者的問題擷取重點；主對話只收到重點與下一步建議。
4. 大量回傳會在 `logs/tool_results/tools_use_index.md` 留下一句話描述，每輪附在 system prompt 尾端。使用者之後（包括下次啟動）追問舊資料時，模型執行 `result_recall <編號>`，讓獨立 session 回到原文重新提煉；問題要一起看幾份時，一次給多個編號。
5. 上下文超過水位時，舊對話會融合成一份滾動摘要。

![一回合的狀態機](doc/images/AI_agent_harness的狀態轉移.png)

![工具回傳管線](doc/images/AI_agent_harness的工具回傳管線.png)

每一步對應到哪個函式、狀態存在哪裡、要改某件事該看哪裡，見〈[架構說明](doc/架構說明.md)〉。

## 快速開始

**需要**：已在執行的 [Ollama](https://ollama.com/)（先 `ollama pull gemma4:e4b`）、Python 3.10+ 與 `pip install ollama`。附圖功能需要 `Pillow`；容器與 ROS2 技能需要宿主機的 `docker` CLI；調度系統技能需要 fih_rmf_system 的 web_console（:8020），離線時可用 `skills_system/scripts/mock_server.py` 代替。

```bash
python3 web_console.py      # Web：http://127.0.0.1:8765（只綁本機）
python3 Agent_Runner.py     # CLI
```

Web 左欄是對話，右欄是系統與工具回傳：每次工具回傳有兩張卡，一張是完整原文（附 `#編號`，可點開），一張是「AI 實際收到的內容」。輸入框打 `/` 會跳出指令與技能選單，旁邊有附圖（📷）與 Plan 模式（📝）按鈕。

| 常用指令 | 作用 |
|---|---|
| `/auto on` | 工具結果自動加入、連續執行到做完（Web 最多 25 輪） |
| `/hybrid on` | 每次問要不要加入，不論選哪個都繼續推論（預設的 manual 也會問，但選「不加入」就結束） |
| `/plan on` | 下一個任務先列步驟，核准後才執行 |
| `/skill <名稱>`、`/skills` | 手動載入某技能的規格／列出所有技能 |
| `/summarize off` | 大量回傳只給成功／失敗，不摘要 |
| `/make_skill <名稱>`、`/trajectory` | 把做對的步驟編成新技能／查看操作軌跡 |
| `/compress`、`/clear` | 手動壓縮上下文／清空對話 |
| `/menu`（Web） | 列出全部指令 |

| 常用設定（環境變數） | 預設 | 說明 |
|---|---|---|
| `WEB_CONSOLE_MODEL` | `gemma4:e4b` | Web 用的主模型（CLI 寫在 `main()`） |
| `WEB_CONSOLE_HOST`／`WEB_CONSOLE_PORT` | `127.0.0.1`／`8765` | Web 綁定位址 |
| `AGENT_NUM_CTX` | `32768` | context 上限；記憶體小的設備可調低 |
| `AGENT_SUMMARY_MODEL` | 同主模型 | 獨立 session 用的模型 |
| `RMF_WEB_CONSOLE_URL` | `http://localhost:8020` | 調度系統的位址 |
| `VISION_MODEL` | `gemma4:e4b` | 附圖分析用的模型 |

其他門檻與保留上限見〈[架構說明 §10](doc/架構說明.md#10-常數與環境變數)〉。

## 技能一覽

| 分類 | 技能 |
|---|---|
| 檔案系統 | `list_dir`、`search_text`、`find_file`、`change_dir`、`view_file` |
| 容器 | `docker_containers`、`docker_images`、`docker_est`、`docker_open`、`docker_runcmd` |
| ROS2（容器內） | `ROS2_topic_list`、`ROS2_topic_echo`、`ROS2_node_list`、`ROS2_node_info` |
| 調度系統 | `workpackage_send`、`workpackage_status`、`workpackage_cancel`、`overpending_cancel`、`semantic_map` |
| 多模態 | `stt_engine`、`image_inspect` |
| 工具結果存檔 | `result_recall`、`result_grep`、`result_view`、`result_list` |
| 記憶 | `modify_memory` |

索引在 [`skills_system/SKILLS.md`](skills_system/SKILLS.md)，每個技能的規格在 `skills_system/tools/<name>.md`；各技能的參數與重點見〈[設計筆記 §3](doc/設計筆記與已知限制.md#3-技能清單全部功能)〉。

## 目錄結構

```
AI_agent_harness/
├── Agent_Runner.py     # CLI 入口（幾行，實作在 agent_core/）
├── web_console.py      # Web 入口（幾行，實作在 web_app/）
├── agent_core/         # 核心：SkillAgent（由各 mixin 組成）、回覆協議、常數、CLI——唯一的核心邏輯來源
├── web_app/            # Web 介面（純標準庫）：狀態、回合流程、slash 指令、HTTP、static/index.html
├── AGENT.md            # system prompt 主體：角色、JSON 回覆格式、執行協議、安全原則、記憶協議
├── Memory.md           # 全域長期記憶（每輪常駐）
├── skills_system/
│   ├── SKILLS.md       # 技能索引（常駐 system prompt）
│   ├── tools/          # 各技能的規格文件（按需載入）
│   ├── scripts/        # 各技能實際執行的腳本與共用模組
│   └── memory/         # 綁定技能的經驗記憶
├── vision/             # 多模態影像 library（擷取、推論、前端框選）
├── subagent/           # 獨立的框選截圖 → 視覺推論小工具
├── doc/                # 架構說明、設計筆記、圖（.puml 與產生器）
└── logs/               # 執行時產生：工具回傳存檔、檢索清單、軌跡、摘要歸檔（.gitignore）
```

## 文件

- 〈[架構說明](doc/架構說明.md)〉：`agent_core/` 與 `web_app/` 的完整解析，含每個模組負責什麼（先從第 0 節「工具回傳什麼時候會被摘要」看起）
- 〈[設計筆記與已知限制](doc/設計筆記與已知限制.md)〉：各機制的理由與實測數據、完整技能清單、驗證方法、已知限制、未來方向
- 〈[長期願景](doc/長期願景.md)〉：AGV/AMR 車隊調度場景的構想（尚未實作）
- 圖的來源在 `doc/*.puml`，用 `python3 doc/流程圖產生器.py` 重新產生
