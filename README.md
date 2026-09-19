# AI_agent_harness

一個以本地 Ollama 模型（預設 `gemma4:e4b`）為核心的 CLI / Web 機器人維運助理。特色是把「可用技能」拆成一份輕量索引 + 一批獨立的規格文件，讓模型在真正需要時才載入完整的使用說明，而不是把所有工具的細節一次塞進系統提示詞。

> 本文件說明目前的實作現況、已知限制，以及後續可能的修改方向，給接手或回頭維護這個專案的人快速建立整體圖像。

---

## 1. 核心概念

### 1.1 兩種操作介面，共用同一顆大腦

- **`Agent_Runner.py`**：終端機互動介面（`SkillAgent` 類別 + `main()` REPL 迴圈）。所有核心邏輯（對話狀態、技能載入、腳本執行、記憶壓縮、token 門檻）都定義在這裡，是整個專案的**唯一核心來源**。
- **`web_console.py`**：純標準庫（無 Flask / FastAPI 依賴）打造的網頁版介面，`import` `Agent_Runner` 裡的 `SkillAgent` 類別與共用函式（`_append_discarded_tool_result`、`_content_for_context`、`TOKEN_THRESHOLD`、`TOOL_RESULT_TOKEN_THRESHOLD`），把同一套推理迴圈改寫成「每次 HTTP 請求處理一小段、回傳事件列表」的形式，本身**不重新定義任何核心規則**。

兩者的差異只在「怎麼跟使用者互動」（終端機輸入 vs. 網頁請求／兩欄式面板），推理與工具執行邏輯完全一致。

### 1.2 技能系統：索引 + 按需載入的規格文件（OKF）

- **`skills_system/SKILLS.md`**：輕量索引，每個技能只列「名稱 + 一行描述」，永遠存在於系統提示詞裡（成本很低）。
- **`skills_system/tools/<name>.md`**：每個技能一份「Open Knowledge Format」規格文件，包含 `背景/運作原理`、`語法/參數規範`、`執行步驟`、`範例`、`異常處理` 等段落，只有在被用到時才會載入。
- **`skills_system/scripts/<name>_cmd.py`**：技能實際對應的可執行腳本。

執行機制（`SkillAgent.run_tool`）只認一種輸出格式 `EXECUTE: ...`，但依目標字串自動分成兩種行為，**完全沒有「技能名稱 → 腳本檔名」的對照表**：

1. 若目標字串剛好對應到 `tools/<name>.md` 存在（代表模型用的是 `SKILLS.md` 索引裡的技能名稱），系統直接把整份規格文件內容當作系統回傳注入上下文，**不執行任何腳本**。
2. 否則把目標字串當成真正的腳本路徑（例如 `scripts/cd_cmd.py`）執行。

這代表模型**必須先讀過規格文件、拿到裡面寫的真實腳本路徑，才有辦法成功執行**——用技能名稱直接呼叫永遠只會拿到規格書，不會誤打誤撞執行成功。這個設計本身就是一種按需載入（progressive disclosure），且是靠架構天然達成，不依賴模型自律或額外的強制攔截邏輯（先前試過用獨立 `NEED_TOOL:` 指令、或後端攔截並強制注入規格的做法，都因為讓模型的「我要執行」跟系統實際回傳的東西對不上、破壞推理連貫性而放棄）。

### 1.3 上下文管理：兩層 token 門檻與可選的 AI 摘要

定義在 `Agent_Runner.py`，CLI 與 Web Console 共用：

| 常數 | 目前值 | 作用 |
| :--- | :--- | :--- |
| `TOKEN_THRESHOLD` | 8000 | 整體對話上下文超過此 token 數，自動觸發 `compress_context_to_file()`：用 LLM 把舊對話摘要成結構化 Markdown 存到 `logs/`，只保留最近幾筆對話 + 摘要繼續。 |
| `TOOL_RESULT_TOKEN_THRESHOLD` | 250 | 單一工具回傳超過此 token 數時，**不會**把完整原始輸出塞進 AI 的上下文，預設改用 `_content_for_context()` 產生的精簡摘要（依內容是否以 `[ERROR]` 開頭，回報「成功」或「失敗」），避免一次大量的搜尋/列目錄結果打斷模型的推理節奏。完整內容仍會顯示給使用者（CLI 印出、或 Web Console 的「系統 / 工具回傳」面板並標記 ⚠️ 待確認）。 |

Web Console 另外加了 `MAX_AUTO_ITERATIONS = 25`：Auto 模式下連續執行工具超過此輪數會強制中止本回合，避免模型陷入迴圈時把伺服器卡死（CLI 版本因為有人在終端機前，可以直接 Ctrl+C，暫無此限制）。

**`num_ctx`**：`ask_ai()` 與 `compress_context_to_file()` 呼叫 `ollama.chat()` 時都明確帶入 `num_ctx=12288`。若不指定，Ollama 會用內建預設值 4096，遠小於模型（`gemma4:e4b`）實際支援的 131072，也小於 `TOKEN_THRESHOLD` 設計的 8000——代表對話還沒到我們自己設計的壓縮門檻，Ollama 就已經在背後悄悄截斷最舊的內容並擠壓輸出空間，看起來就像模型的輸出被無故砍短。拉高 `num_ctx` 是為了讓實際視窗跟應用層自己設計的門檻對齊。

**工具回傳摘要模式（可選，預設關閉）**：CLI 用 `/summarize on|off`、Web Console 用同名指令切換（狀態各自存在 `main()` 的區域變數 / `state["tool_summary_mode"]`）。關閉時就是上表「成功/失敗」判定的預設行為；開啟後，超過 `TOOL_RESULT_TOKEN_THRESHOLD` 的內容會改由 `SkillAgent.summarize_tool_result()` 處理——做法比照 `compress_context_to_file()`：另外開一個獨立、乾淨的一次性 session（專屬 system/user prompt，不接觸主對話的 `self.messages`），對原始輸出做語意摘要，讓主 session 拿到的是「有意義的重點摘要」而不只是成功/失敗判定，摘要完即丟棄。若摘要 session 本身失敗（例如模型出錯），`_content_for_context()` 會自動 fallback 回成功/失敗判定，不會讓主推理流程中斷。Web Console 會把這次獨立摘要的結果額外用紫色卡片顯示在「系統 / 工具回傳」面板（標籤「🧠 AI 摘要（獨立 session）」）。這是本節提到 token 門檻機制的加強版，代價是超過門檻時會多一次 LLM 呼叫、增加延遲，因此設計成可自由開關。

### 1.4 三種執行模式

- **Manual（預設）**：每次工具執行完都要人工確認是否把結果加入上下文（y / n / stop）。
- **Hybrid**（`/hybrid on`）：一樣每次詢問，但不論加入或捨棄都會讓 AI 接續推論（CLI 用 `n` 分支後 `continue`；Web Console 對應 `apply_decision` 裡的 hybrid 分支）。
- **Auto**（`/auto on`）：工具結果自動帶入下一輪，完全不需人工確認，直到沒有工具需要執行為止。

### 1.5 記憶

- **長期記憶**（`Memory.md`）：只有使用者明確要求（「記住這件事」等）才會透過 `modify_memory` 技能寫入，格式固定為 `[問題種類] | [問題描述] | [解決方法或結論]`（規則見 `AGENT.md`）。`modify_memory_cmd.py` 內的檔案路徑是根據腳本自身位置往上推算出的絕對路徑，固定指向專案根目錄下的 `Memory.md`，**不受 `current_cwd` 影響**——早期版本用相對路徑，若 AI 當下的虛擬工作目錄（`current_cwd`，可被 `change_dir` 技能改變）剛好在別的專案，會把記憶寫到那個專案底下而不是這裡，已修正。
- **壓縮歷史**（`logs/summary_*.md`）：`compress_context_to_file()` 產生，`get_system_prompt()` 每次都會讀最近 5 份放進系統提示詞的「Recent Compressed History Summary」。
- **Sticky Objective**：使用者可設定一個最高優先任務，會持續出現在系統提示詞裡提醒模型，直到被清除。

---

## 2. 目錄結構

```
AI_agent_harness/
├── Agent_Runner.py          # 核心：SkillAgent 類別 + CLI REPL（唯一的核心邏輯來源）
├── web_console.py           # Web 版介面，重用 Agent_Runner 的邏輯，不重複定義規則
├── AGENT.md                 # 系統提示詞主體：角色設定、EXECUTE 協議、安全原則、記憶協議
├── Memory.md                # 長期記憶（AI 透過 modify_memory 技能寫入）
├── logs/                    # 自動壓縮產生的歷史對話摘要
├── skills_system/
│   ├── SKILLS.md             # 技能輕量索引
│   ├── tools/<name>.md       # 各技能的 OKF 規格文件（按需載入）
│   └── scripts/<name>_cmd.py # 各技能實際執行的腳本
├── subagent/                 # 獨立的螢幕截圖 + Gemma4 視覺分析腳本（與主流程無關聯）
└── doc/                      # 目前為空
```

---

## 3. 執行方式

### 3.1 前置需求

- 已安裝並執行中的 [Ollama](https://ollama.com/)，且已 `ollama pull` 對應模型（預設 `gemma4:e4b`）。
- Python 3.10+ 與 `ollama` Python 套件（`pip install ollama`）。
- Web Console 使用純標準庫，不需要額外安裝任何套件。

### 3.2 CLI

```bash
python3 Agent_Runner.py
```

常用指令：`/clear`、`/compress`、`/auto on|off`、`/hybrid on|off`、`/summarize on|off`、`objective set|show|clear`、`exit`/`quit`。

### 3.3 Web Console

```bash
python3 web_console.py
```

預設監聽 `http://127.0.0.1:8765`（只綁本機）。可用環境變數調整：

| 環境變數 | 預設值 | 說明 |
| :--- | :--- | :--- |
| `WEB_CONSOLE_MODEL` | `gemma4:e4b` | 使用的 Ollama 模型 |
| `WEB_CONSOLE_HOST` | `127.0.0.1` | 綁定位址，設 `0.0.0.0` 可開放區網存取 |
| `WEB_CONSOLE_PORT` | `8765` | 監聽埠 |

畫面分成左右兩欄：左邊是「使用者 ↔ Agent 對話」，右邊是「系統 / 工具回傳」（規格文件載入內容、腳本執行結果、系統通知都會出現在這裡）。輸入 `/menu` 可查詢目前支援的所有指令。開啟 `/summarize on` 後，超過門檻的工具結果除了原始輸出，右欄還會多一張紫色的「🧠 AI 摘要（獨立 session）」卡片。

---

## 4. 已知限制 / 待處理問題

這些是目前程式碼裡「還沒解決、但看得到」的狀況，供後續維護時參考：

1. **`robot_ping` / `eval_speed` 目前無法執行**：兩者都依賴 `skills_system/skills/nav_core.py`，但這個目錄已經不存在於專案中，執行會拋出 `'NoneType' object is not callable`。要嘛補回 `nav_core.py`，要嘛移除這兩個技能與其規格文件。
2. **自我進化（Self-Evolution）機制已被移除**：`manager.py`、`manage_skill_cmd.py`、`manage_skill.md` 都已從專案中刪除，`AGENT.md` 也拿掉了對應章節。目前 `Agent_Runner.py` 的 `_parse_script_args()` 裡仍留著一行 `if "manage_skill" in script_name` 的特判邏輯，屬於死代碼，不影響功能但可以之後順手清掉。
3. **`workitem_est` 依賴外部調度服務**：對應的 `scripts/mock_server.py` 用 FastAPI + Uvicorn 實作，但目前環境（`common_env`）並未安裝這兩個套件，這支 mock server 本身也還無法啟動。
4. **`stt_engine` 綁死特定環境**：麥克風裝置名稱、Windows 路徑（`C:\temp`）、`ffmpeg.exe` 路徑都寫死在腳本裡，僅適用於作者自己的 WSL + Windows 錄音裝置設定。
5. **`current_cwd` 預設值寫死為 `/home/david`**：`SkillAgent.__init__` 裡硬編碼，換一台機器或給別人使用時需要手動調整或改成動態偵測。
6. **小型本地模型的工具呼叫可靠度**：實測過 `gemma4:e4b` 在需要判斷、選技能的情境下，偶爾會不輸出 `EXECUTE:` 指令、直接「腦補」一份假的執行結果（例如編造一份不存在的目錄列表）。這是模型能力限制，不是架構問題，但值得在後續設計中納入考量（例如偵測回應裡有沒有實際呼叫工具、要求時偵測到可疑輸出就要求重答）。
7. **CLI 與 Web Console 功能不完全對等**：CLI 的 `objective set` 是互動式多行輸入（輸入到 `objective end` 為止），Web Console 為了適應單次 HTTP 請求，簡化成單行的 `/objective set <內容>`。
8. **沒有自動化測試**：目前所有驗證都是開發過程中手動寫的一次性腳本（stub `ollama.chat`、模擬多輪對話），沒有留在專案裡形成正式的測試套件。

---

## 5. 未來修改方向

依討論優先順序排列。原本排在這裡的「用真正的摘要取代死板的成功/失敗訊息」方向已經實作完成（見 1.3 的「工具回傳摘要模式」），故從清單移除。

### 5.1（低成本）讓 AI 自己把檢索範圍縮小

現有的 `search_text`（`grep_cmd.py`）、`find_file`、`list_dir` 已經是「精準檢索」的雛形，但工具回傳預設處理方式仍是「超過門檻就整包捨棄、換成死板的成功/失敗摘要」（除非另外開啟 1.3 提到的摘要模式），屬於治標。更根本的做法是在對應的 `tools/*.md` 規格文件與 `AGENT.md` 的搜尋約束裡，明確要求模型：

- 搜尋前先縮小路徑範圍（已有雛形：禁止對 `/` 遞迴搜尋）。
- 提供關鍵字時盡量具體、必要時分批查詢，而不是一次撈一大片再交給系統事後裁切。

這幾乎零成本（只是改規格文件文字），但效果依賴模型是否確實遵守。

### 5.2 修復已知限制

依第 4 節列出的項目逐項處理，特別是 `robot_ping` / `eval_speed` 的 `nav_core.py` 缺失，以及 `current_cwd` 的硬編碼路徑，這兩項會直接影響任何非原作者環境下的可用性。

### 5.3 重新設計自我進化機制

如果之後想恢復「AI 自己新增技能」的能力，建議一開始就對齊現有的 OKF 架構：新技能建立時同時產生 `tools/<name>.md`（而不是像舊版那樣產生內容陽春的骨架文件），並確保新技能名稱不會跟既有的 `tools/*.md` 檔名衝突。

### 5.4 補齊測試與可攜性

- 把目前開發過程中用來驗證行為的 stub 測試腳本，整理成正式的 `tests/` 目錄（用假的 `ollama.chat` 逐一驗證 `run_tool`、`_content_for_context`、`run_turn`、`apply_decision` 等關鍵函式的行為）。
- 把 `current_cwd`、模型名稱等寫死的預設值改成可透過環境變數或設定檔覆寫，方便在不同機器上部署。
