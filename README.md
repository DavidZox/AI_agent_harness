# AI_agent_harness

一個以本地 Ollama 模型（預設 `gemma4:e4b`）為核心的 CLI / Web 機器人維運助理。特色是把「可用技能」拆成一份輕量索引 + 一批獨立的規格文件，讓模型在真正需要時才載入完整的使用說明，而不是把所有工具的細節一次塞進系統提示詞。

> 本文件說明目前的實作現況、已知限制，以及後續可能的修改方向，給接手或回頭維護這個專案的人快速建立整體圖像。

---

## 1. 核心概念

### 1.1 兩種操作介面，共用同一顆大腦

- **`Agent_Runner.py`**：終端機互動介面（`SkillAgent` 類別 + `main()` REPL 迴圈）。所有核心邏輯（對話狀態、技能載入、腳本執行、記憶壓縮、token 門檻）都定義在這裡，是整個專案的**唯一核心來源**。
- **`web_console.py`**：純標準庫（無 Flask / FastAPI 依賴）打造的網頁版介面，`import` `Agent_Runner` 裡的 `SkillAgent` 類別與共用函式（`_append_discarded_tool_result`、`_content_for_context`、`TOKEN_THRESHOLD`、`TOOL_RESULT_TOKEN_THRESHOLD`），把同一套推理迴圈改寫成「每次 HTTP 請求處理一小段、以 NDJSON 串流逐筆回傳事件」的形式：後端每完成一次推論或工具執行就立刻推一筆到瀏覽器，前端邊收邊渲染，不必等整回合結束；本身**不重新定義任何核心規則**。

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

**訊息數量的滑動視窗（`_truncate_memory`）**：跟上面兩個以 token 為單位的門檻是**互相獨立**的第三道機制，以「訊息則數」為單位。`SkillAgent(max_history=...)` 目前 CLI 與 Web Console 都設為 30；`ask_ai()` 每次呼叫前都會檢查，一旦 `self.messages`（不含開頭的 system prompt）超過 30 則，就直接執行 `self.messages = [self.messages[0]] + self.messages[-30:]`——**沒有摘要、沒有歸檔，超過的部分直接捨棄**。這跟 `compress_context_to_file()` 是兩套不同邏輯：後者是以 token 量觸發、會先摘要存檔才清空；前者純粹以則數為觸發條件，只要一次對話來回夠多（例如工具呼叫很密集的多步驟任務），就可能在還沒累積到 `TOKEN_THRESHOLD` 之前就先被這個機制悄悄丟掉最舊的訊息。這點目前是已知的設計缺口，見第 5.5 節。

### 1.4 三種執行模式

- **Manual（預設）**：每次工具執行完都要人工確認是否把結果加入上下文（y / n / stop）。
- **Hybrid**（`/hybrid on`）：一樣每次詢問，但不論加入或捨棄都會讓 AI 接續推論（CLI 用 `n` 分支後 `continue`；Web Console 對應 `apply_decision` 裡的 hybrid 分支）。
- **Auto**（`/auto on`）：工具結果自動帶入下一輪，完全不需人工確認，直到沒有工具需要執行為止。

### 1.5 記憶

- **長期記憶**（`Memory.md`）：只有使用者明確要求（「記住這件事」等）才會透過 `modify_memory` 技能寫入，格式固定為 `[問題種類] | [問題描述] | [解決方法或結論]`（規則見 `AGENT.md`）。`modify_memory_cmd.py` 內的檔案路徑是根據腳本自身位置往上推算出的絕對路徑，固定指向專案根目錄下的 `Memory.md`，**不受 `current_cwd` 影響**——早期版本用相對路徑，若 AI 當下的虛擬工作目錄（`current_cwd`，可被 `change_dir` 技能改變）剛好在別的專案，會把記憶寫到那個專案底下而不是這裡，已修正。
- **壓縮歷史**（`logs/summary_*.md`）：`compress_context_to_file()` 產生，`get_system_prompt()` 每次都會讀最近 5 份放進系統提示詞的「Recent Compressed History Summary」。
- **Sticky Objective**：使用者可設定一個最高優先任務，會持續出現在系統提示詞裡提醒模型，直到被清除。

### 1.6 Plan 模式：先規劃、經使用者核准才執行

CLI 與 Web Console 都支援 `/plan on|off`（狀態各自是 `main()` 的區域變數 / `state["plan_mode"]`）。開啟後，輸入新任務不會直接進入執行迴圈，而是：

1. `SkillAgent.build_plan_request()` 把任務包裝成「請依 `SKILLS.md` 規劃步驟、這輪不要輸出 `EXECUTE:`」的請求，模型本來就看得到技能索引，不需要額外注入。
2. 模型回傳條列式步驟計畫，顯示給使用者，並詢問「y＝核准 / n＝取消 / 其他文字＝修改意見重新規劃」（CLI 用 `input()` 迴圈 `_run_plan_flow()`；Web Console 用 `plan_pending` 狀態拆成非同步的 `start_plan_flow()` / `handle_plan_response()`，畫面上是一條「📝 有計畫待你核准」提示列 + 核准／取消按鈕，也可以直接在輸入框打字送出修改意見）。
3. **安全設計**：規劃階段從頭到尾不會呼叫 `agent.run_tool()`——確認關卡是「這段程式碼路徑根本不執行工具」保證的，不是單純告訴模型「先別執行」。就算 `gemma4:e4b` 不聽話在計畫裡夾帶了 `EXECUTE:`，也不會被執行。
4. 核准後，計畫文字會存進 `self.current_plan`，並跟 Sticky Objective 用同一種模式：由 `_build_plan_context_prompt()` 注入 `get_system_prompt()`，**每次組系統提示詞都會重新塞入**，因此不會被 1.3 節提到的滑動視窗或壓縮摘要沖掉，整個多步驟任務執行期間都能持續提醒模型「依計畫逐步執行」。
5. 系統不會自動判斷「所有步驟都做完了」而清除計畫（對 `gemma4:e4b` 這種小模型的自我判斷能力不夠信任），需要使用者在任務結束後手動輸入 `/plan done` 清除；`/clear`（`reset_conversation()`）也會一併清空 `current_plan`，避免舊計畫殘留干擾下一個任務。

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

常用指令：`/clear`、`/compress`、`/auto on|off`、`/hybrid on|off`、`/summarize on|off`、`/plan on|off`、`/plan done`、`objective set|show|clear`、`exit`/`quit`。

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

畫面分成左右兩欄：左邊是「使用者 ↔ Agent 對話」，右邊是「系統 / 工具回傳」（規格文件載入內容、腳本執行結果、系統通知都會出現在這裡）。輸入 `/menu` 可查詢目前支援的所有指令。開啟 `/summarize on` 後，超過門檻的工具結果除了原始輸出，右欄還會多一張紫色的「🧠 AI 摘要（獨立 session）」卡片。開啟 `/plan on` 後，新任務會先在左欄顯示一張青綠色的「📝 計畫（待你確認）」卡片，畫面下方會出現核准／取消按鈕，詳見 1.6 節。

---

## 4. 已知限制 / 待處理問題

這些是目前程式碼裡「還沒解決、但看得到」的狀況，供後續維護時參考：

1. **`robot_ping` / `eval_speed` 目前無法執行**：兩者都依賴 `skills_system/skills/nav_core.py`，但這個目錄已經不存在於專案中，執行會拋出 `'NoneType' object is not callable`。要嘛補回 `nav_core.py`，要嘛移除這兩個技能與其規格文件。
2. **自我進化（Self-Evolution）機制已被移除**：`manager.py`、`manage_skill_cmd.py`、`manage_skill.md` 都已從專案中刪除，`AGENT.md` 也拿掉了對應章節。目前 `Agent_Runner.py` 的 `_parse_script_args()` 裡仍留著一行 `if "manage_skill" in script_name` 的特判邏輯，屬於死代碼，不影響功能但可以之後順手清掉。
3. **`workitem_est` 依賴外部調度服務**：對應的 `scripts/mock_server.py` 用 FastAPI + Uvicorn 實作，但目前環境（`common_env`）並未安裝這兩個套件，這支 mock server 本身也還無法啟動。
4. **`stt_engine` 綁死特定環境**：麥克風裝置名稱、Windows 路徑（`C:\temp`）、`ffmpeg.exe` 路徑都寫死在腳本裡，僅適用於作者自己的 WSL + Windows 錄音裝置設定。
5. **小型本地模型的工具呼叫可靠度**：實測過 `gemma4:e4b` 在需要判斷、選技能的情境下，偶爾會不輸出 `EXECUTE:` 指令、直接「腦補」一份假的執行結果（例如編造一份不存在的目錄列表）。這是模型能力限制，不是架構問題，但值得在後續設計中納入考量（例如偵測回應裡有沒有實際呼叫工具、要求時偵測到可疑輸出就要求重答）。1.6 節的 Plan 模式是針對這個限制的其中一種緩解方式——執行前的確認關卡是靠程式碼路徑保證的（規劃階段不呼叫 `run_tool()`），不依賴模型本身是否守規矩。
6. **CLI 與 Web Console 功能不完全對等**：CLI 的 `objective set` 是互動式多行輸入（輸入到 `objective end` 為止），Web Console 為了適應單次 HTTP 請求，簡化成單行的 `/objective set <內容>`。
7. **沒有自動化測試**：目前所有驗證都是開發過程中手動寫的一次性腳本（stub `ollama.chat`、模擬多輪對話），沒有留在專案裡形成正式的測試套件。

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

### 5.5 滑動視窗（`_truncate_memory`）改成「壓縮＋保留最新幾筆」，而不是直接丟棄

見 1.3 節：目前超過 `max_history`（30 則）就直接 `self.messages[-30:]` 硬砍，沒有摘要、沒有歸檔。這跟 `compress_context_to_file()` 的處理方式不一致，也代表如果一次任務工具呼叫特別密集（例如多步驟的 Plan 模式任務），有可能還沒累積到 `TOKEN_THRESHOLD` 就先被這個機制悄悄丟掉最舊的訊息，而且丟掉的內容完全沒有留下任何摘要痕跡（`compress_context_to_file()` 至少會存一份到 `logs/`）。比較好的做法是讓 `_truncate_memory` 觸發時比照 `compress_context_to_file()` 走一次壓縮流程——把要丟棄的最舊那批訊息摘要後存檔，只保留最新幾筆原始對話（不摘要，維持細節完整），而不是無聲無息地整批消失。理想上兩套「上下文太大時該怎麼辦」的邏輯（token 觸發、則數觸發）應該收斂成同一套壓縮機制，只是觸發條件不同。

### 5.6 grep（精準檢索）vs 獨立 session 摘要，該用哪個不應該只看 token 量

目前的邏輯很單純：工具回傳超過 `TOOL_RESULT_TOKEN_THRESHOLD` 就套用精簡摘要或（開啟 `/summarize on` 後的）獨立 session 摘要，兩者間要用哪一個純粹是使用者手動切換的全域開關，跟這次工具回傳的**內容特性**完全無關。但這兩種處理方式其實適合不同情境：像 `search_text` / `find_file` 這類搜尋型技能如果一次撈出大量結果，也許更好的處理是引導模型**用更精確的關鍵字重新查詢一次**（縮小範圍後重新 grep），而不是把一大包搜尋結果硬做語意摘要；但像是長篇日誌、格式不規則的雜訊內容，語意摘要可能才是必要且合理的。也就是說，「這次該重新查更精準」還是「該對現有結果做摘要」，兩者的取捨目前完全沒有依內容或技能類型做區分，純粹是 token 數一刀切。未來可以考慮依技能類型（甚至是規格文件裡的 metadata）決定 `_content_for_context()` 該採取哪種降維策略。

### 5.7 讓 `Memory.md` 的修正「內化」進規格書與 Plan 模式提示詞後即可刪除

目前 `Memory.md` 用來記錄模型曾經犯過的錯誤、使用者要求的行為修正（例如「查看檔案前要先確認」），確實有效避免重蹈覆轍，但 `load_long_term_memory()` 每一輪都會把最近 30 行塞進系統提示詞——這是一個只會越長越大、永遠佔用上下文的清單，跟 1.2 節「按需載入」的設計精神相反（技能規格書只在被用到時才載入，但 `Memory.md` 是不管用不用得到都全部常駐）。未來可以設計一個機制（可以是定期執行，也可以是手動觸發的一個新技能）：

- 讀取 `Memory.md` 裡的條目，判斷每一條屬於「特定技能該有的行為修正」還是「Plan 模式規劃時該遵守的通用原則」。
- 屬於前者的，把修正內容改寫進對應的 `tools/<name>.md` 規格文件（例如加進「異常處理」或新增一條「注意事項」）；屬於後者的，整合進 `AGENT.md` 或 `SkillAgent.build_plan_request()` 的規則段落。
- 內化完成、確認新的規格文件/提示詞已經涵蓋該修正後，把 `Memory.md` 裡對應的那一行刪除。

這樣可以同時達成兩個目標：**按需載入**（修正只會在真的用到那個技能、或真的進入 Plan 模式時才出現在上下文裡，而不是每輪都常駐）與**釋放上下文空間**（`Memory.md` 不會無止盡增長，已經內化過的修正可以被清掉，不用重複佔用 token）。

---

## 6. 長期願景（概念性）：AGV/AMR 車隊調度場景延伸

> 以下內容由使用者提供，是一份完整的概念性系統規格文件，描述這個 harness 未來若要往「AGV/AMR 車隊調度決策輔助」場景擴展時的目標架構，**不是目前程式碼已經實作的東西**，也不會逐項對應到現有模組。之所以收錄在這裡，是因為文中的「多 Session 分割摘要（Map-Reduce Summarization）」機制，正是 5.5、5.6 節提到的上下文壓縮與大量工具回傳摘要優化方向的一個具體、更大規模的參照範例：現在的 `compress_context_to_file()` / `summarize_tool_result()` 都是單一 session 的摘要，而這份文件描述的是當語意資料量大到連單一摘要 session 都塞不下時，如何先分塊、平行摘要、再彙整（Map → Reduce）。以下保留原文結構，僅調整標題階層以嵌入本文件。

### 6.1 設計背景與安全邊界 (Safety & Control Boundaries)

在工業自動化與場域安全標準（如 **ISO 3691-4** 與 **VDA 5050**）規範下，底層路徑規劃與車輛控制指令必須具備高實時性與確定性。為避免 LLM 潛在的幻覺（Hallucination）引發實體碰撞或排程混亂，本系統將 AI Agent 定位為「資深調度顧問 / 情態感知輔助系統」。

**核心原則**

* **不直接介入控制**：AI Agent 不直接向底層 Planner 或 AGV 發送修改控制命令。
* **人機協同 (Human-in-the-Loop)**：採用純觀察/預警（Observation Only）與半自動審查模式，由 Agent 輸出情意分析與調度建議，再由 Fleet Admin（管理員）進行最終確認與執行。

### 6.2 系統架構與功能模組 (System Modules)

AI Agent 在調度架構中擔任「場域全知感知與語意轉譯器」，劃分為四大核心模組：

```
[使用者時間排程] ───┐
                   ├──► [Dynamic Context Ingestion]
[VLM 視覺現況]    ───┤      ( GeoJSON + SkillOKF )
                   │                 │
[海量拓撲語意描述] ──┘                 ▼
                       ┌───────────────────────────────┐
                       │  Multi-Session Summarizer     │
                       │ (分塊平行摘要 / Context 降維)  │
                       └───────────────┬───────────────┘
                                       │
                                       ▼
                       ┌───────────────────────────────┐
                       │        AI Agent Core          │
                       │   (綜合推理、評估與風險衝擊)   │
                       └───────────────┬───────────────┘
                                       │
                                       ▼
                       [Dashboard UI / Heartbeat Report]
                            (給予管理員處置建議)
```

#### 6.2.1 語意變化日誌與監控 (Semantic Event Logging)

* **功能**：持續交叉比對「使用者時間排程文字」與「VLM 視覺辨識現況」。
* **作用**：將非結構化的視覺影像與文字描述，轉化為結構化的場域歷史上下文數據。

#### 6.2.2 海量拓撲語意之多 Session 分割摘要機制 (Multi-Session Map-Reduce Summarization)

* **功能**：當特定節點、邊或區域包含大量歷史維護日誌、複雜 Skill 步驟或高頻率的文字描述時，避免 LLM 因 Token 爆量（Context Window Exceeded）或資訊遺忘而降低推理能力。
* **處理機制（Map-Reduce Pipeline）**：
  1. **Chunking & Routing (分塊)**：將龐大的 GeoJSON 語意檔依據「區域（Zone）」或「節點子集（Node Clusters）」拆分為多個獨立的 Context Session。
  2. **Parallel Map Sessions (平行 Session 摘要)**：啟動多個獨立 Prompt Session 對各自負責的拓撲區域進行「特徵提取與語意降維」，各別產出區域級 Key Risk Factor（關鍵風險因子摘要）。
  3. **Reduce Session (整合總結)**：Agent 主 Thread 收集所有區域 Session 的精簡摘要，搭配當前受影響的局部拓撲，執行最終的車隊總體衝擊評估。

#### 6.2.3 衝擊範圍預估 (Impact Prediction)

* **功能**：當拓撲屬性變更（如路段阻塞、限速）或 VLM 偵測到異態時進行定量推算。
* **預測指標**：
  * **潛在受影響車輛**：列出行經該拓撲區段的 AGV 清單與預計抵達時間 (ETA)。
  * **車隊延遲時間**：計算維持現有排程不變情況下的預計總延遲時間。

#### 6.2.4 建議式心跳匯報 (Suggestive Heartbeat Report)

* **功能**：定期生成包含自然語言摘要與可執行建議的巡檢報告。
* **內容**：包含風險評級、影響評估以及優先級排序的建議處置方案（Recommendations），供管理員於管理面板一鍵採納或調整。

### 6.3 系統輸入與輸出規範 (Data Schema)

#### 6.3.1 Prompt 推理邏輯步驟 (Reasoning Pipeline)

1. **多 Session 預處理解析 (Pre-processing)**：若單一節點/邊的 Context 超過設定 Token 門檻，先調用 Multi-Session 分塊摘要機制，產出精簡版語意特徵矩陣。
2. **情境驗證 (Validation)**：比對「預排時間文字」與「VLM 即時影像」，確認是否有異常偏差或加劇狀況。
3. **影響推算 (Impact Calculation)**：評估若「維持現有排程」，車隊遭遇阻礙的風險與潛在壅塞時間。
4. **方案生成 (Recommendation Generation)**：依據拓撲規範與 SkillOKF 指引，產出供管理員參考的建議處置方案。
5. **報告摘要 (Heartbeat Synthesis)**：生成自然語言格式的心跳巡檢報告。

#### 6.3.2 結構化輸出 JSON Schema

```json
{
  "decision_summary": {
    "status_level": "NORMAL | WARNING | CRITICAL",
    "event_type": "STRING",
    "semantic_evaluation": "預排文字、海量語意摘要與 VLM 比對之綜合評估結論"
  },
  "impact_analysis": {
    "affected_topology": ["GeoJSON_Edge_or_Node_ID"],
    "potentially_impacted_agvs": [
      {
        "agv_id": "STRING",
        "eta_to_event_sec": "NUMBER",
        "risk_description": "風險與阻礙情境描述"
      }
    ],
    "estimated_fleet_delay_sec": "NUMBER"
  },
  "suggested_recommendations": [
    {
      "recommendation_id": "STRING",
      "target_agv": "STRING",
      "suggested_action": "建議管理員採取的具體操作",
      "priority": "LOW | MEDIUM | HIGH"
    }
  ],
  "heartbeat_report": "給予現場管理員閱讀的自然語言巡檢報告摘要"
}
```

### 6.4 工程落地優勢 (Engineering Advantages)

| 評估維度 | 系統優勢與價值 |
| --- | --- |
| **零系統安全風險** | Agent 僅做 UI 上的建議與報告輸出，其輸出不會干擾 FMS 核心調度邏輯（如 A* / Dijkstra 算法）與實體 AGV/AMR 的車載控制器。 |
| **海量文字高擴充性** | 引進多 Session 分割摘要機制，大幅提升對大規模場域（如數千個拓撲節點）或極繁瑣歷史註解文字的消化能力，有效控制 Token 成本並預防 LLM 幻覺。 |
| **低維護與整合成本** | 不需要為 LLM 封裝複雜且高度敏感的 FMS 底層 API 控制權，前端僅需實作資訊卡片渲染與「採納建議」之按鈕事件。 |
| **數據累積與模型迭代** | 系統運作期間可記錄「Agent 建議方案」與「管理員實際處置」之差異，做為日後 Prompt 工程優化或 Model Fine-tuning 的 RLHF 數據庫。 |
