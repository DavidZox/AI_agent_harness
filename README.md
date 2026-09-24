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
- **`skills_system/memory/<name>.md`**（可選）：技能綁定的經驗記憶，由 `modify_memory --skill <name>` 寫入（見 1.5）。載入規格文件時 `_load_skill_doc()` 自動把條目附在規格後面的「# 經驗記憶」區塊，跟規格文件同一套按需載入，平常不佔上下文。
- **`skills_system/scripts/<name>_cmd.py`**：技能實際對應的可執行腳本。由 `/make_skill` 產生的「組合技能」腳本只是資料（步驟清單與參數），執行邏輯在共用的 `scripts/_composite.py`（見 1.8）。

**回覆協議（實體隔離）**：模型每一次回覆都是固定的 JSON 物件 `{"thought", "reply", "action"}`，由 `ask_ai()` 以 Ollama `format=AGENT_REPLY_SCHEMA` 強制結構（與壓縮摘要、`make_skill` 同一機制），`AGENT.md` 另附「解釋／選技能／執行」三個對比範例。`reply` 是給人看的文字，`action` 是給系統執行的指令：`null` 或 `{"command": 技能名稱｜腳本路徑, "args": 參數字串}`。`parse_agent_reply()` 解析後，CLI 與 Web 只顯示 `reply`（💭 thought 另外顯示、有 action 時附一行「▶ action」），`run_tool()` **只看 `action`**——`reply` 裡不論寫了什麼，包括解釋時抄出來的 `EXECUTE: ...` 範例、或把整份規格複述一遍，都不會被執行。回覆不是合法 JSON 時降級為「顯示原文、該輪不執行」，**不會退回用文字比對找指令**。另一種失敗是「空白回覆」：合法 JSON 但 `reply` 是空字串、`action` 是 `null`——實際案例是 thought 寫到「關鍵字是」就斷掉，因為模型接著想寫 `"scheduler"`，而英文雙引號在 JSON 字串裡就是「字串結束」，文法接著只允許 `"reply":`，模型被迫給出空字串與 `null`。`ask_ai()` 對這種回覆會帶著暫時的提醒（不進歷史）自動重試一次，UI 顯示「已自動重試一次」；`AGENT.md` 同時要求 thought 精簡、thought／reply 不用英文雙引號（引用改用「」或反引號）。另一個實測發現：改成 JSON 後，小模型載入規格文件之後容易停下來「解釋規格」或反問使用者要不要執行（`reply` 欄位本身就在邀請它聊天），兩階段完成率一度掉到 5/8；把 `run_tool()` 載入規格時回給模型的那句話改成明確的下一步指令（「這一輪只是載入規格、還沒有執行任何東西：下一輪請直接依規格繼續使用者原本的任務……不要解釋規格、不要詢問是否執行」），並在 `AGENT.md` 補上同樣的規則後回到 7/8，剩下的一次是模型把「列出 skills_system/tools 目錄」誤解成系統提示詞裡的技能索引，與協議無關。`args` 刻意是字串而不是物件：所有腳本都吃位置參數，規格文件裡 `EXECUTE: <路徑> <參數>` 的範例就對應 `command`＝路徑、`args`＝參數，十八份 `tools/*.md` 一份都不用改。改成這個協議的直接原因是舊版用文字比對 `EXECUTE:`：實測 `gemma4:e4b` 會在回覆裡複述含有 `EXECUTE:` 行的規格文件、或在解釋時寫出範例指令，都被當成真的指令執行（曾把整份規格當參數送進腳本、收到 65 個參數）。JSON 的單一 `action` 欄位也自然實現了「一次只做一步」，不再靠模型自律。

`run_tool()` 拿到 `action` 後依 `command` 自動分成兩種行為，**完全沒有「技能名稱 → 腳本檔名」的對照表**：

1. 若 `command` 剛好對應到 `tools/<name>.md` 存在（代表模型用的是 `SKILLS.md` 索引裡的技能名稱），系統直接把整份規格文件內容（若有 `memory/<name>.md`，連同該技能的經驗記憶）當作系統回傳注入上下文，**不執行任何腳本**。
2. 否則把 `command` 當成真正的腳本路徑（例如 `scripts/cd_cmd.py`）、`args` 當參數執行。

這代表模型**必須先讀過規格文件、拿到裡面寫的真實腳本路徑，才有辦法成功執行**——用技能名稱直接呼叫永遠只會拿到規格書，不會誤打誤撞執行成功。歷史訊息裡保存的是模型原始的 JSON（模型看得到自己上一輪的格式，維持一致），壓縮摘要與工具摘要的錨點文字則只取 `reply` 與 `[action]`，`thought` 不進摘要。使用者也可以**手動按需載入**：Web Console 在輸入框打「/」的選單裡點選技能（或輸入 `/skill <名稱>`，CLI 同名指令），`SkillAgent.manual_skill_block()` 產生的規格區塊（含該技能的經驗記憶，以 `[skill loaded]` 標記開頭）會附在下一則使用者訊息後面一起送出，AI 拿到規格就能直接依腳本路徑執行，省掉一輪「先載規格」；技能清單來自 `SkillAgent.list_skills()` 解析 `SKILLS.md`（名稱、描述、`##` 分類、是否有經驗記憶）。這個設計本身就是一種按需載入（progressive disclosure），且是靠架構天然達成，不依賴模型自律或額外的強制攔截邏輯（先前試過用獨立 `NEED_TOOL:` 指令、或後端攔截並強制注入規格的做法，都因為讓模型的「我要執行」跟系統實際回傳的東西對不上、破壞推理連貫性而放棄）。

**腳本回傳慣例與逾時機制**：所有技能腳本統一由 stdout 回傳結果，成功以 `[PASS]` 開頭、失敗（含逾時）一律以 `[ERROR]` 開頭並說明原因與建議動作——`_content_for_context()` 就是靠這個前綴判定成功/失敗，`SKILLS.md` 開頭也向模型說明了這個慣例。每支會呼叫外部程序或網路的腳本都有自己的逾時上限（各 `tools/<name>.md` 的「語法 / 參數規範」有寫）；容器相關腳本共用 `scripts/_docker_common.py`，在容器內以 coreutils `timeout` 包住指令，逾時會真正終止容器內的程序而不是只殺掉宿主機端的 `docker exec`，並把常見的 docker / ros2 錯誤翻成可行動的說明。`SkillAgent.run_tool` 另設 `TOOL_EXEC_TIMEOUT`（600 秒）作為最後防線，並在腳本以非零 exit code 結束時補上 `[ERROR]` 前綴、以及在腳本完全沒有輸出時給出明確訊息——否則空字串會被誤判成「沒有工具需要執行」。

### 1.3 上下文管理：雙水位線壓縮、滾動融合摘要與可選的 AI 摘要

**Token 計量方式（真實 token 尺度）**：Ollama 沒有 tokenize API（Python 套件與伺服器皆無），所以採「能精確就精確、不能就校準估算」：AI 回覆用每次 `ollama.chat()` 回報的 `eval_count`；整體上下文大小用回報的 `prompt_eval_count`（完整 prompt 的 token 數，含 chat 模板；prompt cache 命中時仍回報完整值，已實測），兩次呼叫之間新增的訊息以校準比估算增量（`SkillAgent.context_tokens()`）；使用者輸入與工具回傳沒有模型呼叫可依，用「字元數 ÷ `chars_per_token`」估算，`chars_per_token` 每次呼叫後以 `prompt_eval_count` 對整個 prompt 重新校準（`_record_usage()`，中文為主的內容實測約 1.8～1.9）。因此下表所有門檻與 `num_ctx` 同一尺度，可以直接比較。Web Console 標題列的 `ctx` 顯示目前上下文大小，前面有 `≈` 代表估算值。

定義在 `Agent_Runner.py`，CLI 與 Web Console 共用：

| 常數 | 目前值 | 作用 |
| :--- | :--- | :--- |
| `NUM_CTX` | 32768（環境變數 `AGENT_NUM_CTX`） | 一次請求給 Ollama 的 context 上限，主對話、壓縮摘要、工具摘要三種 session 共用。模型本身支援 131072。預設從 12288 提高：system prompt 約 3000～4000 tokens 在 12288 下佔三成，水位之間只剩幾百 tokens 給對話，實測兩分鐘內壓縮四次、每次 10～15 秒。gemma4:e4b 在 32768 下 `ollama ps` 顯示的佔用量與 12288 相同（3.4 GB），記憶體不是問題；較小的設備可設回 12288 或更低。 |
| `TOKEN_THRESHOLD`（硬水位） | `NUM_CTX × 75%`（預設 24576；比例可用 `AGENT_HARD_RATIO` 覆寫） | 呼叫模型前的最後防線：`ask_ai()` 送出前若上下文超過此值，一定**同步**壓縮（`ensure_context_budget()`），確保壓縮先於 Ollama 在 `num_ctx` 處的靜默截斷；若剛好有背景壓縮在跑，先等它完成、仍超標才自己壓，不會兩份摘要同時跑。這是唯一的硬水位檢查點——舊版在每輪工具執行後還有一處，且壓縮後 `continue` 會跳過「把工具結果加入上下文」那一步、直接再問一次 AI，AI 拿不到剛執行的結果而重複下同一個指令，已移除。 |
| `SOFT_TOKEN_THRESHOLD`（軟水位） | `NUM_CTX × 60%`（預設 19660；比例可用 `AGENT_SOFT_RATIO` 覆寫） | 只在**回合結束後**檢查（`after_turn_compression()`：CLI 印完最終回覆、Web Console 串流送出最終 chat 事件之後）。此時使用者正在讀答案、模型閒著，壓縮不會拉長任何一次回覆的等待時間，切點也剛好落在任務邊界，不會把一個任務切成兩半。`/parallel_cal on` 時改在背景執行緒做（見下方）。可壓的舊內容少於 `MIN_COMPRESS_TOKENS` 時不會宣告也不會動作。 |
| `MIN_COMPRESS_TOKENS` | `max(1000, NUM_CTX ÷ 20)`（預設 1638） | 軟水位的「值不值得」門檻：一次摘要要花一次模型呼叫，只為騰出幾百 tokens 不划算。system prompt 佔比高時「超過水位卻沒什麼可壓」是常態，舊版會在這種狀態下每回合都壓一次。硬水位不受此限。 |
| `KEEP_RECENT_TOKENS`（low watermark） | `NUM_CTX × 15%`（預設 4915） | 壓縮時保留最新這麼多 token 的**原文**：從最新往回累加、在訊息邊界切、至少保留最新一則、不拆開 assistant 的 `EXECUTE:` 與它的 `[tool result]`（`_split_for_compression()`）。其餘交給摘要。舊版固定「保留最新 2 則」，兩則可能只有 50 tokens 也可能 1500 tokens，銜接感不穩定。 |
| `SUMMARY_MAX_CHARS` / `SUMMARY_MAX_PREDICT` | 600 字 / 2000 tokens | 融合摘要的長度目標（寫進摘要 prompt；實測 gemma4:e4b 會超過目標約三成，實際落在 450～600 tokens）與輸出硬上限（`num_predict`，防止失控；被截斷時 JSON 會解析失敗退回原文，所以留得夠寬）。 |
| `SUMMARY_ARCHIVE_KEEP` | 30（環境變數 `AGENT_SUMMARY_KEEP`） | `logs/` 只保留最近 N 份 `summary_*.md` 與同名 `.json`，其餘在每次歸檔後自動刪除。 |
| `SUMMARY_MODEL` | `None`（環境變數 `AGENT_SUMMARY_MODEL`） | 壓縮摘要與工具摘要兩種獨立 session 共用的模型，預設與主模型相同；與主模型不同時，`/parallel_cal on` 的背景摘要才會真的與主對話平行。取捨見下方「摘要模型」。 |
| `TOOL_RESULT_TOKEN_THRESHOLD` | 500（約 1000 字元） | 單一工具回傳超過此 token 數時，**不會**把完整原始輸出塞進 AI 的上下文，預設改用 `_content_for_context()` 產生的精簡摘要（依內容是否以 `[ERROR]` 開頭，回報「成功」或「失敗」），避免一次大量的搜尋/列目錄結果打斷模型的推理節奏。完整內容仍會顯示給使用者（CLI 印出、或 Web Console 的「系統 / 工具回傳」面板並標記 ⚠️ 待確認）。**技能規格文件例外**：`run_tool` 載入規格文件的回傳（以 `SKILL_DOC_PREFIX` 開頭）一律完整進入上下文、不標 ⚠️——它是按需載入機制的核心，被精簡掉 AI 就拿不到腳本路徑；規格書本身以 400 tokens（約 800 字元）以內為原則。 |

Web Console 另外加了 `MAX_AUTO_ITERATIONS = 25`：Auto 模式下連續執行工具超過此輪數會強制中止本回合，避免模型陷入迴圈時把伺服器卡死（CLI 版本因為有人在終端機前，可以直接 Ctrl+C，暫無此限制）。

**`num_ctx` 與門檻的關係**：所有 `ollama.chat()` 呼叫都帶 `num_ctx=NUM_CTX`。若不指定，Ollama 會用內建預設值 4096，對話還沒到壓縮門檻就會在背後悄悄截斷最舊的內容並擠壓輸出空間，看起來就像模型的輸出被無故砍短。`TOKEN_THRESHOLD` 直接定義為 `NUM_CTX` 的 75%（`HARD_RATIO`），兩者同一尺度，壓縮一定先於截斷發生。歷史教訓：舊版 `TOKEN_THRESHOLD = 8000` 是以「字元÷4」計量，換成真實 token 約 17000，早已超過 `num_ctx` 12288，壓縮實際上永遠來不及觸發。

**滾動融合摘要（rolling summary）**：`compress_context_to_file()` 先用 `_split_for_compression()` 切出保留區以外的舊訊息，交給 `summarize_messages()`：摘要模型同時收到「上一份 `rolling_summary`」與「這次要併入的新片段」（渲染成 `[user]` / `[assistant]` 標頭的純文字，舊版直接塞 Python list 的 repr，夾帶 `{'role': ...}` 與 `\n` 轉義，浪費 token 又難讀），輸出一份更新後的完整摘要**取代**上一份——延續舊內容、依新片段更新進度、淘汰已解決或過時的細節，但使用者明確表達的偏好與限制一律保留。結構由 Ollama 的 `format=` JSON schema（`SUMMARY_SCHEMA`：overview / key_progress / results_and_errors / user_preferences / open_items）強制，再由 `_render_summary_markdown()` 排成固定五段的 Markdown，不靠模型自己遵守範本；模型仍沒給合法 JSON 時退回原文，總比丟掉整段歷史好。每次壓縮都會寫一個 `logs/summary_<時間>.md` 供稽核，但 `get_system_prompt()` 的「Recent Compressed History Summary」**只注入最新一份**（舊版載入最近 5 個檔案，prompt 成本是單份摘要的 5 倍，且第 6 次壓縮起最舊的會無聲消失）。啟動時從 `logs/` 最新一份載入，因此摘要會跨 session 延續；`/clear` 不會清掉它（與舊版行為一致），要完全重來請刪除 `logs/` 下的檔案。每份歸檔同時寫一個同名 `.json`（排版前的結構化資料、壓縮則數、token 統計），給日後的反思機制讀取；`logs/` 依 `SUMMARY_ARCHIVE_KEEP` 只保留最近 30 份，並已加入 `.gitignore`。摘要模型看到的對話會把以 `HARNESS_MARKERS`（`[tool result]`、`[vision result]`、`[skill loaded]`、`[PLAN_REQUEST]`…）開頭的 user 訊息標成 `[harness …]`，並被明確告知這些是框架插入的系統訊息，不要把其中的規則記成使用者偏好——舊版曾把 `build_plan_request()` 的「規劃時不要輸出 EXECUTE」記成使用者偏好。

**system prompt 的成本**：`AGENT.md` 約 1500、`SKILLS.md` 約 800、`Memory.md` 約 600、滾動摘要約 500 tokens，合計約 3400，每輪常駐；水位是對整體算的，所以 `NUM_CTX` 太小時對話可用的空間會被壓得很窄。`Memory.md` 已把三組講同一件事的條目各合併成一則、`SKILLS.md` 的說明段落已精簡。`AGENT.md` 曾試過把執行協議從約 1550 tokens 精簡到約 1000（重複的規則只留一次），但 A/B 實測（同一個「列出目前目錄」任務各跑多次）精簡版出現了猜腳本檔名、單一回覆塞多行 `EXECUTE:` 的失誤，原版 8 次全對，因此保留原版的詳細寫法——對 4B 級模型而言重複強調是有用的，省下的 450 tokens 在 32768 的上下文裡不值得換。另外補了一條「腳本路徑不可推測」規則，且 `run_tool` 找不到腳本時會回傳可行動的 `[ERROR]`（猜的檔名若包含某個技能名稱，直接提示先 `EXECUTE: <該技能>` 載入規格）。

**`/parallel_cal on|off`（背景壓縮，預設關閉）**：CLI 與 Web Console 同名指令，預設值可用 `AGENT_PARALLEL_CAL=1` 改成開啟。開啟後軟水位的壓縮改由 `start_background_compression()` 在背景執行緒進行：先快照要壓的訊息、呼叫摘要模型（期間不持有任何鎖）、完成後 `_apply_compression()` 以**物件身分**把那幾則從 `messages` 原地移除（不重綁 list、不靠索引），所以壓縮期間主對話新加入的訊息不會遺失；主對話送給 Ollama 的也是快照，校準比以真正送出的那份計算。完成或失敗的通知放進 `pop_notices()`：CLI 在下一次輸入後印出，Web Console 隨每次 stats 推送、且背景進行中前端每 4 秒輪詢一次 `/api/status`，標題列會顯示「🗜️ 背景壓縮中」。同一時間只允許一個背景壓縮；硬水位遇到背景進行中會先等它完成。**前提（實測）**：摘要要真的與主對話同時推論，需要 Ollama 為模型配置 2 個以上 slot，**或**摘要模型與主模型不同（不同模型是不同 runner 程序，天然平行）。本機 `OLLAMA_NUM_PARALLEL=2`，但 Ollama 0.34 的新引擎對多模態模型（gemma4）強制單 slot：`/api/ps` 的 `context_length` 只有一份 `num_ctx`，並發送出一長一短兩個請求時，單獨只要 0.1 秒的短請求要等 3 秒的長請求結束才回來。因此**同模型**開 `/parallel_cal on`，背景摘要期間你的下一次對話會在 Ollama 內排隊，等待只是搬到下一次呼叫（UI 上 done 仍立即送出、通知照常到達、不會遺失訊息）。搭配 `AGENT_SUMMARY_MODEL` 指定另一個模型才有真正的平行：實測 `gemma4:26b` 生成長文期間，`gemma4:e4b` 的短請求 0.2 秒回來、不受影響；代價是第二個模型的記憶體與算力分攤（每多一個 slot 也多一份 `num_ctx` 的 KV cache）。API 查不到 slot 數，程式無法自動判斷，算力弱的設備建議維持關閉（序列處理：回合結束後同步壓完再等待輸入，答案仍然是先送出的）。

**摘要模型（`AGENT_SUMMARY_MODEL`）**：預設與主模型相同。可指定同家族的小模型（例如 `gemma3:1b`）縮短摘要時間，但注意三件事：統一記憶體的機器（Jetson、GB10）上「CPU 卸載」省不到記憶體只省算力；多載一個模型可能把主模型擠出 Ollama，重載主模型的代價遠高於一次摘要；摘要會進入之後每一輪的 system prompt，小模型的錯誤會複利累積。有足夠記憶體再考慮。反過來說，這也是目前在多模態 gemma4 上讓 `/parallel_cal on` 真正平行的唯一方法（見上段）。

**工具回傳摘要模式（可選，預設關閉）**：CLI 用 `/summarize on|off`、Web Console 用同名指令切換（狀態各自存在 `main()` 的區域變數 / `state["tool_summary_mode"]`）。關閉時就是上表「成功/失敗」判定的預設行為；開啟後，超過 `TOOL_RESULT_TOKEN_THRESHOLD` 的內容會改由 `SkillAgent.summarize_tool_result()` 處理——做法比照 `compress_context_to_file()`：另外開一個獨立、乾淨的一次性 session（專屬 system/user prompt，不接觸主對話的 `self.messages`，模型同 `SUMMARY_MODEL`），對原始輸出做語意摘要，讓主 session 拿到的是「有意義的重點摘要」而不只是成功/失敗判定，摘要完即丟棄。若摘要 session 本身失敗（例如模型出錯），`_content_for_context()` 會自動 fallback 回成功/失敗判定，不會讓主推理流程中斷。Web Console 會把這次獨立摘要的結果額外用紫色卡片顯示在「系統 / 工具回傳」面板（標籤「🧠 AI 摘要（獨立 session）」）。這是本節提到 token 門檻機制的加強版，代價是超過門檻時會多一次 LLM 呼叫、增加延遲，因此設計成可自由開關。

**訊息則數的滑動視窗（`_truncate_memory`）已預設停用**：`SkillAgent(max_history=None)` 是 CLI 與 Web Console 的預設值，上下文大小只由上面的 token 門檻決定，舊內容一律「摘要歸檔」而不是「無聲丟棄」。`max_history` 保留為可選的保險絲（例如設 200），啟用時超過則數會直接砍掉最舊訊息、不摘要不歸檔，一般情況不建議開。

### 1.4 三種執行模式

- **Manual（預設）**：每次工具執行完都要人工確認是否把結果加入上下文（y / n / stop）。
- **Hybrid**（`/hybrid on`）：一樣每次詢問，但不論加入或捨棄都會讓 AI 接續推論（CLI 用 `n` 分支後 `continue`；Web Console 對應 `apply_decision` 裡的 hybrid 分支）。
- **Auto**（`/auto on`）：工具結果自動帶入下一輪，完全不需人工確認，直到沒有工具需要執行為止。

### 1.5 記憶

- **長期記憶**：只有使用者明確要求（「記住這件事」等）才會透過 `modify_memory` 技能寫入，格式固定為 `[問題種類] | [問題描述] | [解決方法或結論]`（規則見 `AGENT.md`）。**兩種目標**：不加參數寫入全域 `Memory.md`，`load_long_term_memory()` 每輪把最近 30 行放進系統提示詞常駐；加 `--skill <技能名稱>` 則寫入 `skills_system/memory/<技能名稱>.md`，只在該技能規格文件被 `EXECUTE` 載入時，由 `_load_skill_doc()` 自動附在規格文件後面（「# 經驗記憶」區塊）一併進入上下文，平常不佔任何 token——跟規格文件同一套按需載入。判斷規則寫在 `AGENT.md` 的記憶寫入協議：關於某個技能的用法、參數、前置條件、曾發生的錯誤就綁技能；通用原則、技能之間的取捨、溝通風格、專案經驗寫全域。腳本會拒絕不存在的技能名稱與 `--skill modify_memory`（工具本身不是記憶主題，小模型常這樣誤填）、略過重複內容，全域寫入的內容若點名了單一技能會附上「下次可用 `--skill`」的提示（已寫入、不需重寫），單一技能條目超過 10 則或約 800 字時附上整併提醒。`modify_memory_cmd.py` 內的檔案路徑是根據腳本自身位置推算的絕對路徑，**不受 `current_cwd` 影響**——早期版本用相對路徑，若 AI 當下的虛擬工作目錄剛好在別的專案，會把記憶寫到那個專案底下，已修正。
- **壓縮歷史**（`logs/summary_*.md` + 同名 `.json`）：每次壓縮寫一組檔供稽核與日後反思，只保留最近 30 份（`SUMMARY_ARCHIVE_KEEP`），`get_system_prompt()` 只注入記憶體中最新一份融合摘要（`rolling_summary`，啟動時從最新的檔案載入），詳見 1.3 的「滾動融合摘要」。
- **Sticky Objective**：使用者可設定一個最高優先任務，會持續出現在系統提示詞裡提醒模型，直到被清除。

### 1.6 Plan 模式：先規劃、經使用者核准才執行

CLI 與 Web Console 都支援 `/plan on|off`（狀態各自是 `main()` 的區域變數 / `state["plan_mode"]`）。開啟後，**下一個**新任務不會直接進入執行迴圈，而是：

1. `SkillAgent.build_plan_request()` 把任務包裝成「請依 `SKILLS.md` 規劃步驟、這輪不要輸出 `EXECUTE:`」的請求（以 `[PLAN_REQUEST]` 標記開頭，修改意見為 `[PLAN_REVISION]`，讓主模型與摘要模型知道這是框架訊息而不是使用者說的話），模型本來就看得到技能索引，不需要額外注入。
2. 模型回傳條列式步驟計畫，顯示給使用者，並詢問「y＝核准 / n＝取消 / 其他文字＝修改意見重新規劃」（CLI 用 `input()` 迴圈 `_run_plan_flow()`；Web Console 用 `plan_pending` 狀態拆成非同步的 `start_plan_flow()` / `handle_plan_response()`，畫面上是一條「📝 有計畫待你核准」提示列 + 核准／取消按鈕，也可以直接在輸入框打字送出修改意見）。
3. **安全設計**：規劃階段從頭到尾不會呼叫 `agent.run_tool()`——確認關卡是「這段程式碼路徑根本不執行工具」保證的，不是單純告訴模型「先別執行」。就算 `gemma4:e4b` 不聽話在規劃回覆裡填了 `action`，也不會被執行；計畫文字取回覆的 `reply` 欄位。
4. 核准後，計畫文字會存進 `self.current_plan`，並跟 Sticky Objective 用同一種模式：由 `_build_plan_context_prompt()` 注入 `get_system_prompt()`，**每次組系統提示詞都會重新塞入**，因此不會被 1.3 節提到的滑動視窗或壓縮摘要沖掉，整個多步驟任務執行期間都能持續提醒模型「依計畫逐步執行」。**核准的同時自動退出 Plan 模式**（`plan_mode` 關閉）：Plan 模式的意義是「下一個新任務先規劃」，規劃階段到核准為止；取消（n）或送修改意見則維持 Plan 模式，方便重新描述任務再規劃。舊版核准後仍停留在 Plan 模式，之後每個新任務都會再被要求規劃一次，看起來像卡在這個模式，已修正。
5. **計畫的生命週期 = 核准後那個任務的執行期間**：期間所有工具決策（manual／hybrid 的 y/n）、auto 迴圈、自動壓縮都不會清掉它；使用者**送出下一個新任務**（打字送出的新訊息，不含 slash 指令、計畫回應、工具決策）時由 `clear_plan_for_new_task()`（Web）／`main()` 開頭（CLI）自動清除，並顯示「🧹 上一個已核准的計畫已隨新任務自動清除」。要提早清除可 `/plan done`；`/clear`（`reset_conversation()`）也會一併清空。系統仍然不靠模型自己判斷「所有步驟都做完了」（對 `gemma4:e4b` 這種小模型的自我判斷不夠信任），而是用「新任務送出」這個確定的事件當邊界。取捨：若 AI 在計畫中途停下來問你問題、你打字回答，這句回答也會被視為新任務而清掉計畫——AI 仍能從對話歷史看到計畫內容，只是少了系統提示詞的持續提醒；相較之下舊版要求手動 `/plan done`，實際使用時容易忘記，舊計畫會殘留在系統提示詞干擾之後的每個任務。

### 1.7 多模態影像：`vision/` library 與 📷 附圖

影像相關的邏輯集中在頂層套件 `vision/`，三個使用者共用同一份：Web Console 的 📷 附圖、獨立的 `subagent/screen_gemma4_web.py`（框選截圖 → 推論的單頁工具，現在只是 library 的薄殼）、以及 `image_inspect` 技能（讓 Agent 自己對檔案路徑做圖像推論）。

- `vision/capture.py`：伺服器端螢幕擷取，後端自動偵測：WSL → PowerShell（原 sniper 做法）、Linux X11 桌面 → Pillow `ImageGrab` + `xrandr` 列螢幕；都沒有時 `backend()` 回傳 None，前端改用瀏覧器的 `getDisplayMedia`（分享畫面）擷取，任何作業系統都能用，但頁面需以 `http://localhost` 或 https 開啟。裁切一律在瀏覽器端以原始解析度完成後上傳 PNG，底圖以等比例（letterbox）顯示，多螢幕拼接的寬桌面不會被拉變形。
- `vision/images.py`：任何來源（檔案、bytes、data URL、截圖裁切）統一成 `PIL.Image`。
- `vision/inference.py`：`analyze(images, prompt)` 呼叫多模態模型（預設 `gemma4:e4b`，可用 `VISION_MODEL` / `VISION_TIMEOUT` 環境變數調整），逾時與錯誤分類只在這裡維護一份，失敗一律拋 `VisionError`。
- `vision/session.py`：`VisionSession`，Web Console 與 sniper 共用的「螢幕選單 → 擷取 → 框選裁切 → 影像清單」工作階段。
- `vision/web/`：前端共用的框選覆蓋層與螢幕選單（`snip.js` / `snip.css`），兩個頁面都以 `/static/vision/...` 載入。

**附圖如何進入主對話**：採「獨立視覺 sub-session」，而不是把影像直接塞進主對話的 messages。使用者送出帶影像的訊息時，後端先把影像 + 使用者訊息交給 `analyze()`（system prompt 要求它直接回答問題，並逐字抄錄影像中與問題相關的文字、數值、錯誤訊息），得到的文字以 `[vision result]` 區塊附在使用者訊息後面，再進入原本的 `run_turn()`（plan 模式則進入規劃）。主對話因此永遠是純文字，`compress_context_to_file`、`count_tokens`、`_truncate_memory` 都不需要知道影像的存在；代價是主 Agent 看到的是描述而非原圖，追問時需重新附圖。這段區塊以 user 角色進入對話，模型容易講成「你提供的視覺分析」；現在區塊開頭明確標示【系統影像分析】、說明使用者只提供了影像、並要求稱之為「影像分析結果」——這段稱呼指示**刻意只寫在區塊裡、不寫進 `AGENT.md`**：曾經把「影像分析結果」列進 `AGENT.md` 的 Harness Messages 當作稱呼之一，結果 `gemma4:e4b` 在完全沒有附圖的純文字對話裡，也把一般工具回傳講成「影像分析結果顯示…」。`AGENT.md` 現在對所有系統插入的訊息只給一個稱呼「系統回傳」，與影像有關的字眼只在真的附圖時才會出現在上下文。這跟 1.3 節的獨立摘要 session 是同一種設計。視覺分析結果不套用 `TOOL_RESULT_TOKEN_THRESHOLD` 的精簡（它是影像唯一的文字表示），sub-session 的 system prompt 要求一般控制在 300 字以內，超過門檻時右欄卡片會標 ⚠️ 待確認。影像只在送出一次新任務時消費，slash 指令與 plan 的核准／修改意見不會用掉附件。

**新增影像來源**：只要能產出 `PIL.Image` 或檔案路徑，呼叫 `vision.analyze([img], prompt)` 即可；要讓 Agent 自己使用，比照 `scripts/image_inspect_cmd.py` 寫一支腳本並登錄到 `SKILLS.md`。

### 1.8 自建技能：`/make_skill` 把做對的操作軌跡編譯成組合技能

這是「讓 Agent 自己做出技能」的第一階段。使用者不論是開 Plan 模式核准後逐步執行、還是不開 Plan 模式一步步引導，把一件事做對之後，輸入 `/make_skill <技能名稱>` 就能把這段正確路徑變成一個可重複使用的技能。兩種引導方式只決定「軌跡怎麼產生」，編譯這一步刻意**做成 harness 的功能，而不是交給 4B 模型自由發揮**，理由有三：技能清單裡沒有寫檔工具，模型今天根本無法產生規格、腳本、索引三個檔案；引導到做對為止通常對話很長、中途會壓縮，而摘要刻意不保留工具輸出原文，早期步驟的確切腳本名稱與參數只剩模型的印象（它連現有腳本名稱都會猜錯）；模型寫的 Python 不能未經審核就註冊進 `SKILLS.md`，等於讓它自己擴充自己的權限。所以分工是：**harness 記錄軌跡、挑步驟、驗證、用範本產生檔案、寫入索引；模型只填一份 JSON；使用者在預覽後核准**。

1. **操作軌跡**（`SkillAgent.trajectory`）：`run_tool` 每執行一支腳本就記一筆——指令、參數、當時的工作目錄與容器目錄、成功（`[PASS]`）或失敗（`[ERROR]`／逾時）、輸出開頭 300 字、當時的使用者任務敘述——同時追加到 `logs/trajectory.jsonl`。載入規格文件與「找不到腳本」的猜測不算步驟。軌跡存在對話 `messages` 之外，上下文壓縮不會沖掉。另有 `kind="boundary"` 的**起點**：`/clear`（`reset_conversation`）、計畫核准（`confirm_plan()`，CLI 與 Web 共用）、每次 `make_skill` 註冊完成。`/trajectory` 列出全部步驟與編號。
2. **挑步驟**：`/make_skill <名稱>` 預設取「上一個起點之後」的步驟（Plan 模式下就是這個計畫的執行期間）；也可以指定 `all`、`3-7`、`3,5,8`。`_group_trajectory()` 把每個成功步驟與它之前的失敗嘗試配成一組（同一個意圖下的修正歷程，供模型歸納注意事項），最後仍未修正的失敗另外列出、不會成為步驟。這批步驟所屬的已核准計畫（起點記錄裡帶著計畫文字）與引導過程中的使用者指令也一起交給模型當作意圖。
3. **模型填表**（`draft_skill_from_trajectory()`）：獨立的一次性 session、以 Ollama `format=MAKE_SKILL_SCHEMA` 結構化輸出，欄位是標題、索引描述、分類、用途、參數（之後會變動的值：容器名稱、路徑、關鍵字……，example 必須是軌跡中的原值）、每個成功步驟的 `include`／目的／參數化後的 args、成功判準、注意事項。模型由 `AGENT_SKILL_MODEL` 指定，預設同摘要模型（再退回主模型）；因為是離線一次性的工作，記憶體夠可以指定較大的模型換取更好的參數化判斷。
4. **驗證**（`_normalize_skill_draft()`）：模型的參數化必須能「代回 example 後與實際記錄到的參數逐字相同」，對不上的步驟退回原值並在預覽提醒；模型只宣告了參數、args 卻照抄原值時（實測 `gemma4:e4b` 的典型表現），系統依 example（就是記錄到的原值）自動代入佔位符——整個參數相同直接換，長度 ≥3 的值也允許以詞邊界為界的子字串替換（`docker` 不會換掉 `docker_runcmd` 裡的字），並在預覽註明；參數數量不同、漏填的步驟一樣退回原值；分類不在 `SKILLS.md` 就放「自建技能」（沒有這個段落會自動建立）；沒有被任何步驟用到的參數移除；模型把所有步驟都排除時改為全部納入。步驟順序永遠依時間，模型只能排除、不能重排或新增。
5. **範本產生三個檔案**（先寫到 `skills_system/drafts/<名稱>/`）：`tools/<名稱>.md` 是與其他技能相同的 OKF 段落（用途含步驟清單、語法、範例、回傳、異常＝注意事項）；`scripts/<名稱>_cmd.py` 只有 `NAME / PARAMS / STEPS` 三個資料常數，執行邏輯全在共用的 `scripts/_composite.py`：依序以 subprocess 執行既有技能的腳本、把命令列參數代入 `{參數}` 佔位符、任一步 `[ERROR]` 即停止並指出第幾步、`change_dir` 的 `[CWD_CHANGED]` 會帶到下一步且標記行保留在總輸出讓 harness 同步狀態、單步 300 秒／合計 570 秒（低於 harness 的 600）、中間步驟輸出截短；草稿位置的腳本會自己往上找到 `scripts/`，所以草稿可以直接重播。第三個是 `SKILLS.md` 的索引行。模型一行 Python 都不寫，逾時與錯誤翻譯直接繼承底層腳本與 `_docker_common.py`。
6. **核准流程**：CLI（`_run_make_skill_flow()`）與 Web（`handle_skill_draft_response()`）共用 `SkillAgent` 的狀態機 `start_skill_draft / revise_skill_draft / approve_skill_draft / cancel_skill_draft`（`pending_skill_draft`）。預覽（摘要、參數、步驟、排除的步驟、注意事項、提醒、完整規格文件）顯示後：`y` 核准並註冊；`t` 先**重播驗證**——用各參數的 example 值、以第一步當時的工作目錄實際跑一次草稿腳本，回 `[PASS]` 才註冊，失敗則保留草稿；`n`／空白取消（刪草稿、軌跡保留）；其他文字＝修改意見，帶著上一版 JSON 重擬。含會改變狀態的技能（`NON_READONLY_SKILLS`：`docker_est`、`workitem_est`、`modify_memory`、`change_dir`、`docker_open`）時預覽會提醒重播是真的執行。註冊＝寫入 `tools/`、`scripts/`、`SKILLS.md` 對應分類段落、稽核 JSON 進 `logs/`、刪草稿、軌跡記起點；`ask_ai()` 每次重組 system prompt，下一次呼叫就看得到新技能。Web Console 有草稿待決定時，輸入框的內容一律視為對草稿的回應（與計畫核准同一種做法），不消耗附件與待送的技能規格。
7. **引導建議**：做技能時優先用 Plan 模式（起點自動、修正對應到特定步驟、軌跡乾淨）；逐步探索的軌跡若很亂，用編號範圍或修改意見拿掉探索步驟，或乾脆用 Plan 模式乾淨重跑一次再做。修正時講到技能名稱與參數，模糊的修正只會多出幾筆失敗嘗試。之後會變的值（容器名稱、路徑）引導時用真實值，做技能時說明哪些要參數化。一個技能只做一件事。只有一步的做對經驗請用 `modify_memory --skill` 寫記憶，不必做技能。**第二階段**（需要新程式碼的能力，例如解析一個新指令的輸出：讓較大的模型草擬腳本、在 harness 裡用軌跡中的實際案例跑測試迭代、通過並經審核才註冊）尚未實作。

**實測（`gemma4:e4b` 同時當主模型與草擬模型）**：軌跡為「列出 `skills_system/tools` → 對 `/` 搜尋被安全邊界擋下 → 改在該目錄搜尋 `docker`」三步。草擬 7 秒；模型的標題、索引描述、分類、注意事項（「不要對根目錄搜尋」正確來自那次失敗）都可用，兩個參數（目錄、關鍵字）宣告正確、example 也是原值，但 args 全部照抄原值、沒放佔位符——第一版流程因此把參數判成未使用而移除，變成寫死 `docker` 的零參數技能；加上「依 example 自動代入」後兩個參數都正確進入步驟，預覽列出兩則「系統自動代入」提醒，重播驗證通過。註冊後開新 session：使用者點名技能時，模型兩輪完成「`EXECUTE: find_spec_keyword` 載規格 → `EXECUTE: scripts/find_spec_keyword_cmd.py skills_system/tools ros2`」並拿到 `[PASS]`；不點名只描述任務時，兩次實測各一次有從索引描述選到新技能，選到之後卻把整份規格當作工具結果複述、再把它貼在 `EXECUTE:` 後面（收到 65 個參數），組合腳本回了含用法的 `[ERROR]`——這是第 4 節第 5 點的模型幻覺問題，不是技能本身的問題。

---

## 2. 目錄結構

```
AI_agent_harness/
├── Agent_Runner.py          # 核心：SkillAgent 類別 + CLI REPL（唯一的核心邏輯來源）
├── web_console.py           # Web 版介面，重用 Agent_Runner 的邏輯，不重複定義規則
├── AGENT.md                 # 系統提示詞主體：角色設定、EXECUTE 協議、安全原則、記憶協議
├── Memory.md                # 全域長期記憶（modify_memory 不加 --skill 時寫入，每輪常駐）
├── logs/                    # 壓縮歸檔（.md + .json，只留最近 30 份）、操作軌跡 trajectory.jsonl、make_skill 稽核 JSON；已 .gitignore
├── skills_system/
│   ├── SKILLS.md             # 技能輕量索引（/make_skill 註冊的技能也寫在這裡）
│   ├── tools/<name>.md       # 各技能的 OKF 規格文件（按需載入）
│   ├── memory/<name>.md      # 技能綁定的經驗記憶（modify_memory --skill 寫入，載入規格時自動附上）
│   ├── drafts/<name>/        # /make_skill 尚未核准的技能草稿（暫時檔，已 .gitignore）
│   └── scripts/<name>_cmd.py # 各技能實際執行的腳本（容器類共用 _docker_common.py；組合技能共用 _composite.py）
├── vision/                   # 多模態影像 library：擷取 / 影像處理 / 推論 / 工作階段 / 前端框選資源（見 1.7）
├── subagent/                 # 獨立的框選截圖 → 視覺推論單頁工具（vision library 的薄殼）
└── doc/                      # 目前為空
```

---

## 3. 執行方式

### 3.1 前置需求

- 已安裝並執行中的 [Ollama](https://ollama.com/)，且已 `ollama pull` 對應模型（預設 `gemma4:e4b`）。
- Python 3.10+ 與 `ollama` Python 套件（`pip install ollama`）。
- Web Console 使用純標準庫，不需要額外安裝任何套件。
- 多模態影像功能（Web Console 的 📷、`image_inspect` 技能、`subagent/screen_gemma4_web.py`）需要 `Pillow`（`pip install pillow`）。「框選畫面」的伺服器端截圖支援 WSL（PowerShell）與 Linux X11 桌面（需 Pillow 有 xcb 支援、有 `xrandr`）；其他環境（Wayland、macOS、瀏覽器在別台電腦）會改由瀏覽器的分享畫面功能擷取，此時頁面需以 `http://localhost` 或 https 開啟。伺服器端截的是執行 web_console 那台機器的螢幕。

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
| `AGENT_NUM_CTX` | `32768` | Ollama context 上限，各水位都是它的比例（1.3 節；CLI 亦適用） |
| `AGENT_HARD_RATIO` / `AGENT_SOFT_RATIO` | `0.75` / `0.60` | 硬／軟水位佔 `NUM_CTX` 的比例（1.3 節；CLI 亦適用） |
| `AGENT_SUMMARY_KEEP` | `30` | `logs/` 保留的摘要歸檔份數（1.3 節；CLI 亦適用） |
| `AGENT_SUMMARY_MODEL` | 同主模型 | 壓縮摘要／工具摘要用的模型（1.3 節；CLI 亦適用） |
| `AGENT_PARALLEL_CAL` | 未設定＝關閉 | `/parallel_cal` 的啟動預設值（1.3 節；CLI 亦適用） |
| `AGENT_SKILL_MODEL` | 同摘要模型 | `/make_skill` 草擬技能草稿用的模型（1.8 節；CLI 亦適用） |

畫面分成左右兩欄：左邊是「使用者 ↔ Agent 對話」，右邊是「系統 / 工具回傳」（規格文件載入內容、腳本執行結果、系統通知都會出現在這裡）。輸入 `/menu` 可查詢目前支援的所有指令。AI 每一輪的 💭 thought 會以灰色虛線卡片出現在右欄，左欄的回覆有 action 時附一行「▶ action: …」（1.2 節的回覆協議）。開啟 `/summarize on` 後，超過門檻的工具結果除了原始輸出，右欄還會多一張紫色的「🧠 AI 摘要（獨立 session）」卡片。開啟 `/plan on` 後，新任務會先在左欄顯示一張青綠色的「📝 計畫（待你確認）」卡片，畫面下方會出現核准／取消按鈕，詳見 1.6 節。輸入 `/make_skill <名稱>` 後左欄會出現紫色的「🧩 技能草稿」卡片（預覽含完整規格文件），下方出現「核准並註冊／重播驗證後註冊／取消草稿」按鈕，也可以直接打字送出修改意見，詳見 1.8 節；`/trajectory` 列出本次 session 的操作軌跡與步驟編號。輸入框旁的 📷 可以「框選畫面」或「選擇檔案」附加影像，送出後右欄會先出現藍色的「🖼️ 視覺分析（獨立 session）」卡片，再由主 Agent 依分析結果回應，詳見 1.7 節；視覺模型可用 `VISION_MODEL`、逾時可用 `VISION_TIMEOUT` 環境變數調整。在輸入框打「/」會彈出選單：上半是功能開關與指令（依「模式開關／動作與查詢」分組），下半是 `SKILLS.md` 的技能（依分類，有經驗記憶的標「含經驗記憶」），↑↓ 移動、Enter／Tab 選取、Esc 關閉，繼續打字可過濾。選指令只會填入輸入框、再按 Enter 才送出（避免誤點 `/clear`）；選技能等同 `/skill <名稱>`：規格立刻顯示在右欄（綠色虛線卡片）、輸入框上方出現 📘 chip，下一則訊息送出時附在後面（`[skill loaded]` 區塊），slash 指令、計畫回應、工具決策不會消耗它，chip 可個別移除；只載入技能不打字送出時用預設提示詞請 AI 簡述用法。後端端點：`GET /api/commands`（選單內容）、`POST /api/skill/load|remove|clear|status`。標題列的 `ctx` 顯示目前上下文大小與軟／硬水位（`≈` 代表估算值）；回合結束後觸發軟水位壓縮時右欄會出現「🗜️ 回合結束…壓縮中」與「📦 已將 N 則舊對話融合成摘要」兩則系統訊息，開啟 `/parallel_cal on` 後則是「已在背景開始壓縮」，完成時另有通知，詳見 1.3 節。

---

## 4. 已知限制 / 待處理問題

這些是目前程式碼裡「還沒解決、但看得到」的狀況，供後續維護時參考：

1. **`robot_ping` / `eval_speed` 目前無法執行**：兩者都依賴 `skills_system/skills/nav_core.py`，但這個目錄已經不存在於專案中，執行會拋出 `'NoneType' object is not callable`。要嘛補回 `nav_core.py`，要嘛移除這兩個技能與其規格文件。
2. **自我進化（Self-Evolution）機制已被移除**：`manager.py`、`manage_skill_cmd.py`、`manage_skill.md` 都已從專案中刪除，`AGENT.md` 也拿掉了對應章節。目前 `Agent_Runner.py` 的 `_parse_script_args()` 裡仍留著一行 `if "manage_skill" in script_name` 的特判邏輯，屬於死代碼，不影響功能但可以之後順手清掉。
3. **`workitem_est` 依賴外部調度服務**：對應的 `scripts/mock_server.py` 用 FastAPI + Uvicorn 實作，但目前環境（`common_env`）並未安裝這兩個套件，這支 mock server 本身也還無法啟動。
4. **`stt_engine` 綁死特定環境**：麥克風裝置名稱、Windows 路徑（`C:\temp`）、`ffmpeg.exe` 路徑都寫死在腳本裡，僅適用於作者自己的 WSL + Windows 錄音裝置設定。
5. **小型本地模型的工具呼叫可靠度**：實測過 `gemma4:e4b` 在需要判斷、選技能的情境下，偶爾會不輸出 `EXECUTE:` 指令、直接「腦補」一份假的執行結果（例如編造一份不存在的目錄列表）。這是模型能力限制，不是架構問題，但值得在後續設計中納入考量（例如偵測回應裡有沒有實際呼叫工具、要求時偵測到可疑輸出就要求重答）。1.6 節的 Plan 模式是針對這個限制的其中一種緩解方式——執行前的確認關卡是靠程式碼路徑保證的（規劃階段不呼叫 `run_tool()`），不依賴模型本身是否守規矩。1.2 節的 JSON 回覆協議則消除了另一類誤觸發：模型在 `reply` 裡複述規格或舉例指令時，舊版會被文字比對當成真的指令執行，現在只看 `action` 欄位；但它擋不住模型自己「腦補」一份工具結果——那仍是模型能力問題。
6. **CLI 與 Web Console 功能不完全對等**：CLI 的 `objective set` 是互動式多行輸入（輸入到 `objective end` 為止），Web Console 為了適應單次 HTTP 請求，簡化成單行的 `/objective set <內容>`。
7. **沒有自動化測試**：目前所有驗證都是開發過程中手動寫的一次性腳本（stub `ollama.chat`、模擬多輪對話），沒有留在專案裡形成正式的測試套件。
8. **使用者輸入與工具回傳的 token 數仍是估算值**：Ollama 沒有 tokenize API，只有 AI 回覆（`eval_count`）與整體上下文（`prompt_eval_count`）是精確的；其餘以每次呼叫後校準的字元比估算（見 1.3 節），對中文為主的內容誤差通常在一成以內，但夾雜大量程式碼或 URL 的工具輸出可能偏差較大。`count_tokens()` 保留了對 `ollama.tokenize()` 的偵測，日後套件提供時會自動改用精確值。
9. **背景壓縮是否真的平行取決於 Ollama 的 slot 配置**：`OLLAMA_NUM_PARALLEL=2` 不保證有 2 個 slot——實測 Ollama 0.34 新引擎對多模態模型（gemma4）強制單 slot，同模型的背景摘要會讓下一次主對話在 Ollama 內排隊；要真的平行需以 `AGENT_SUMMARY_MODEL` 指定另一個模型（已實測可行）。API 查不到 slot 數，程式無法自動判斷，只能靠文件提醒（1.3 節）。
10. **融合摘要的長度只能靠 prompt 約束**：`SUMMARY_MAX_CHARS` 是寫進提示詞的目標，實測 `gemma4:e4b` 會超過約三成；`SUMMARY_MAX_PREDICT` 只是防失控的硬上限，被截斷時 JSON 解析失敗會退回原文（結構消失但內容不丟）。
11. **小模型對 `modify_memory --skill` 的判斷不可靠**：新版記憶寫入協議要求模型自行判斷記憶該綁技能還是寫全域（1.5 節）。實測 `gemma4:e4b` 四個案例中兩個判斷錯誤：使用者點名 `view_file` 時仍寫成全域；通用的溝通風格規則卻綁到 `modify_memory` 自己。後者由腳本以 `[ERROR]` 拒絕並說明要改寫全域（實測模型收到錯誤後下一輪就改成全域寫入），前者則靠 `[PASS]` 訊息裡的提示與使用者自行更正。跟第 5 點一樣是模型能力限制，框架只能把最糟的錯誤（自綁、綁不存在的技能）擋成可恢復的錯誤。
12. **`/make_skill` 的草稿品質受模型限制**：哪些值該參數化、索引描述是否讓之後的模型選得到，都是草擬模型的判斷（1.8 節）。框架只保證「錯不會傳下去」：參數化代回原值對不上就退回原值、模型宣告了參數卻沒放佔位符就由系統依原值代入、分類不存在就放預設分類、漏填的步驟照原值納入，並在預覽把每一項修正都列成提醒；最終仍靠使用者在預覽時檢查與修改意見。想要更好的判斷可用 `AGENT_SKILL_MODEL` 指定較大的模型。
13. **JSON 回覆協議下的「空白回覆」**：`gemma4:e4b` 在 thought／reply 裡想引用某個詞時偶爾會用英文雙引號，在 JSON 字串裡它會提前結束字串，剩下的內容全部遺失，結果是合法 JSON 但 `reply` 空、`action` `null`（1.2 節）。框架的處理是自動重試一次並在提示詞裡禁用英文雙引號；合成情境八次未重現，屬機率性問題，重試後仍空白時會如實顯示「重試後仍為空白」。反過來 `args` 需要包住含空白參數時（`ros2_humble "ls -la /opt"`）四次實測都正確寫成 `\"`。
14. **組合技能的輸出容易超過工具回傳門檻**：多步驟的總輸出常超過 `TOOL_RESULT_TOKEN_THRESHOLD`，主模型預設只會收到精簡的成功／失敗判定（各步輸出仍完整顯示給使用者），需要重點時開 `/summarize on`。重播驗證只驗證「以原值重跑會不會 `[PASS]`」，不比對輸出內容，且會真的執行有副作用的步驟（預覽會提醒）。目前只支援「既有步驟的序列」這種組合技能，需要新程式碼的能力仍要人寫腳本（第二階段未實作）。

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

### 5.3 自我進化機制：第一階段已完成，第二階段待做

第一階段（見 1.8）已實作：`/make_skill` 把使用者引導 Agent 做對的操作軌跡編譯成「組合技能」，對齊現有 OKF 架構（同時產生 `tools/<name>.md`、資料型的 `scripts/<name>_cmd.py`、`SKILLS.md` 索引行，名稱衝突由 `validate_new_skill_name()` 擋下），模型只填 JSON、不寫程式，核准前可重播驗證。第二階段是「需要新程式碼的技能」：讓較大的模型草擬腳本到 `drafts/`，harness 以軌跡中的實際案例當測試 oracle 反覆執行、餵回錯誤讓它修正，通過且經使用者審核後才註冊；另外可考慮把 Plan 模式「所有步驟完成」時主動詢問「要做成技能嗎」，以及讓修正過程中的失敗嘗試自動寫成該技能的 `memory/<name>.md`。

### 5.4 補齊測試與可攜性

- 把目前開發過程中用來驗證行為的 stub 測試腳本，整理成正式的 `tests/` 目錄（用假的 `ollama.chat` 逐一驗證 `run_tool`、`_content_for_context`、`run_turn`、`apply_decision` 等關鍵函式的行為）。
- 把 `current_cwd`、模型名稱等寫死的預設值改成可透過環境變數或設定檔覆寫，方便在不同機器上部署。

### 5.5 ～已解決～ 滑動視窗、雙水位線與背景壓縮收斂成同一套機制

原本 `_truncate_memory` 以「訊息則數」（30 則）硬砍、`compress_context_to_file` 以 token 觸發，兩套邏輯並存且前者可能先於後者無聲丟棄內容。第一步：token 計量改為真實尺度（`prompt_eval_count` / `eval_count` + 校準估算，見 1.3），`TOKEN_THRESHOLD` 定義為 `NUM_CTX` 的 70%，則數視窗預設停用（`max_history=None`），只保留為可選保險絲。

第二步（雙水位線 + 滾動融合 + 背景壓縮，見 1.3）：壓縮從「卡在關鍵路徑上的單一門檻」改成兩道水位——軟水位在回合結束後、答案送出之後才動作，硬水位只留在 `ask_ai()` 呼叫前當保證；保留區從「最新 2 則」改為 token 預算且不拆開指令／結果；摘要從「每次獨立一份、system prompt 載最近 5 份」改為與上一份融合、只注入一份，並以 JSON schema 保證結構；`/parallel_cal on` 讓軟水位壓縮在背景執行緒進行（同一個多模態模型在目前的 Ollama 被強制單 slot，要真的平行需搭配不同的摘要模型，見 1.3）。序列模式的三種候選策略中未採用「閒置計時器觸發」（本質仍是背景執行，1 個 slot 的弱設備上等待只是被搬走，且 auto 模式沒有閒置）與「小模型／CPU 卸載當預設」（統一記憶體省不到記憶體、可能擠掉主模型、摘要錯誤會累積），後者保留為 `AGENT_SUMMARY_MODEL` 旋鈕。

### 5.6 grep（精準檢索）vs 獨立 session 摘要，該用哪個不應該只看 token 量

目前的邏輯很單純：工具回傳超過 `TOOL_RESULT_TOKEN_THRESHOLD` 就套用精簡摘要或（開啟 `/summarize on` 後的）獨立 session 摘要，兩者間要用哪一個純粹是使用者手動切換的全域開關，跟這次工具回傳的**內容特性**完全無關。但這兩種處理方式其實適合不同情境：像 `search_text` / `find_file` 這類搜尋型技能如果一次撈出大量結果，也許更好的處理是引導模型**用更精確的關鍵字重新查詢一次**（縮小範圍後重新 grep），而不是把一大包搜尋結果硬做語意摘要；但像是長篇日誌、格式不規則的雜訊內容，語意摘要可能才是必要且合理的。也就是說，「這次該重新查更精準」還是「該對現有結果做摘要」，兩者的取捨目前完全沒有依內容或技能類型做區分，純粹是 token 數一刀切。未來可以考慮依技能類型（甚至是規格文件裡的 metadata）決定 `_content_for_context()` 該採取哪種降維策略。

### 5.7 讓 `Memory.md` 的修正「內化」進規格書與 Plan 模式提示詞後即可刪除

> **～技能部分已實作～**：`modify_memory` 現在支援 `--skill <技能名稱>`，把記憶寫進 `skills_system/memory/<技能名稱>.md`，載入該技能規格時由 `_load_skill_doc()` 自動附在後面（見 1.5）。實作上沒有直接改寫 `tools/<name>.md`，而是用獨立的 memory 檔：手寫的規格書維持精簡（200 tokens 預算）且不會被機器附加的內容弄亂，條目可以個別檢視、編輯、刪除，對模型而言載入的結果仍是「規格 + 經驗」一份完整文字。原本 `Memory.md` 裡三則明顯屬於特定技能的條目（view_file 查看前先確認、view_file／search_text 前先 change_dir、stt_engine 缺 faster-whisper）已搬過去並自 `Memory.md` 刪除，其餘屬於通用原則的條目維持全域常駐。**尚未做**：下面提到的第二種目標（Plan 模式規劃原則內化進 `build_plan_request()`），以及定期把全域條目重新分類、整併的機制。後者的語料已經備好：`logs/summary_*.json` 裡的 `results_and_errors`／`user_preferences`／`open_items` 欄位，反思機制可以定期讀最近幾份，提議寫進全域或 `--skill` 綁定的記憶。


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
