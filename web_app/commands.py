"""slash 指令：「/」選單的清單（SLASH_COMMANDS）、/menu 的說明文字、handle_slash_command。"""
from vision import DEFAULT_MODEL as VISION_MODEL
from agent_core.config import (
    KEEP_RECENT_TOKENS,
    MIN_COMPRESS_TOKENS,
    NUM_CTX,
    SOFT_TOKEN_THRESHOLD,
    TOKEN_THRESHOLD,
    TOOL_RESULT_TOKEN_THRESHOLD,
)
from .state import agent, load_pending_skill, state


# 「/」選單裡的功能開關與指令（技能清單另由 agent.list_skills() 提供）。args=True 代表選取後只填入、等使用者接參數。
SLASH_COMMANDS = [
    {"cmd": "/auto on", "desc": "Auto Continue：工具結果自動帶入下一輪，不需確認", "group": "模式開關"},
    {"cmd": "/auto off", "desc": "關閉 Auto Continue（回到手動確認）", "group": "模式開關"},
    {"cmd": "/hybrid on", "desc": "Hybrid：每次工具結果都詢問，捨棄後仍讓 AI 接續", "group": "模式開關"},
    {"cmd": "/hybrid off", "desc": "關閉 Hybrid", "group": "模式開關"},
    {"cmd": "/summarize on", "desc": "超過門檻的工具回傳由獨立 session 依任務擷取重點（預設）", "group": "模式開關"},
    {"cmd": "/summarize off", "desc": "改為只給成功／失敗判定（不多花一次模型呼叫）", "group": "模式開關"},
    {"cmd": "/plan on", "desc": "下一個新任務先規劃、經核准後執行（核准後自動退出）", "group": "模式開關"},
    {"cmd": "/plan off", "desc": "關閉 Plan 模式", "group": "模式開關"},
    {"cmd": "/parallel_cal on", "desc": "軟水位壓縮改在背景執行緒進行", "group": "模式開關"},
    {"cmd": "/parallel_cal off", "desc": "壓縮改回序列處理", "group": "模式開關"},
    {"cmd": "/skill ", "desc": "手動載入某技能的規格，隨下一則訊息送出（直接點下方技能清單更快）", "group": "動作與查詢", "args": True},
    {"cmd": "/skills", "desc": "列出所有可用技能", "group": "動作與查詢"},
    {"cmd": "/menu", "desc": "顯示完整指令說明", "group": "動作與查詢"},
    {"cmd": "/compress", "desc": "手動壓縮並歸檔目前的歷史對話", "group": "動作與查詢"},
    {"cmd": "/plan done", "desc": "提早清除目前已核准的計畫", "group": "動作與查詢"},
    {"cmd": "/make_skill ", "desc": "把這段做對的操作步驟編譯成新技能（後接技能名稱，可再接步驟範圍如 3-7）", "group": "動作與查詢", "args": True},
    {"cmd": "/trajectory", "desc": "列出本次 session 記錄到的腳本執行軌跡（步驟編號、成功／失敗）", "group": "動作與查詢"},
    {"cmd": "/objective set ", "desc": "設定 Sticky Objective（後面接內容）", "group": "動作與查詢", "args": True},
    {"cmd": "/objective show", "desc": "查看目前的 Objective", "group": "動作與查詢"},
    {"cmd": "/objective clear", "desc": "清除 Objective", "group": "動作與查詢"},
    {"cmd": "/clear", "desc": "清空對話記憶，重新開始（選取後需再按 Enter 才會送出）", "group": "動作與查詢"},
]

MENU_TEXT = """可用指令：
/menu                  顯示本說明
/clear                  清空對話記憶，重新開始
/compress               手動壓縮並歸檔目前的歷史對話
/auto on / /auto off    切換 Auto Continue 模式（工具結果自動帶入下一輪，不需確認）
/hybrid on / /hybrid off 切換 Hybrid 模式（每次工具結果都詢問是否加入上下文）
/summarize on / /summarize off 切換工具回傳的任務導向摘要（見下方說明，預設開啟）
/parallel_cal on / /parallel_cal off 切換平行壓縮（回合結束後的軟水位壓縮改在背景執行緒做，預設關閉）
/plan on / /plan off    開啟 Plan 模式：下一個新任務會先規劃步驟、經你核准後才執行；核准後自動退出（預設關閉）
/plan done              提早清除目前已核准的計畫（正常情況下會在你送出下一個新任務時自動清除）
/objective set <內容>   設定 Sticky Objective（最高優先任務，會持續提醒 AI）
/objective show         查看目前的 Objective
/objective clear        清除 Objective
/skills                 列出所有可用技能
/skill <技能名稱>        手動載入該技能的規格（含其經驗記憶），隨你下一則訊息一起送出
/trajectory             列出本次 session 記錄到的腳本執行軌跡（步驟編號、成功／失敗、所屬技能）
/make_skill <名稱> [範圍] 把做對的操作步驟編譯成新技能（範圍省略＝上一個起點之後；可用 3-7、3,5,8 或 all）

在輸入框打「/」會彈出選單：上半是功能開關與指令，下半是 SKILLS.md 裡的技能（依分類）。
↑↓ 移動、Enter／Tab 選取、Esc 關閉，也可以繼續打字過濾。選指令只會填入輸入框、要再按
Enter 才送出（避免誤點 /clear）；選技能等同 /skill <名稱>：規格立刻顯示在右欄、輸入框上方
出現 📘 chip，下一則訊息送出時附在後面，AI 就能直接依規格裡的腳本路徑執行，省掉一輪
「先載規格」。chip 可個別移除；slash 指令、計畫回應、工具決策不會消耗它。

標題列的 cwd 是 AI 目前的工作目錄（change_dir 切換）；container 是目前的**目標容器**：預設空白，AI 成功操作某個
容器後（docker_open 選定、docker_est 建立、或 docker_runcmd／ROS2_* 用了某個容器）自動帶入，之後容器技能可以
省略容器名稱，AI 也不會再問你要看哪個容器；要換容器直接跟 AI 說（它會用 docker_open 切換，或直接用新名稱操作）。
/clear 不會清掉它，跟 cwd 一樣。

不切換 auto／hybrid 時，預設為「手動模式」：每次工具執行完都會等待你確認
是否要把結果加入上下文，畫面下方會出現決策按鈕。

AI 的每一次回覆都是固定的 JSON（thought／reply／action），由 Ollama 的結構化輸出強制：左欄顯示 reply
（有 action 時附一行「▶ action」），右欄多一張灰色「💭 思考」卡片。系統只看 action 欄位決定要不要
載入規格或執行腳本，reply 裡不論寫了什麼指令文字都不會被執行；回覆不是合法 JSON 時降級為純文字顯示、
該輪不執行任何東西。

開啟 /plan on 後，輸入新任務時 AI 不會馬上執行，而是先依 SKILLS.md 規劃出
步驟清單顯示出來，畫面下方會出現「✅ 核准並執行 / 🚫 取消任務」按鈕；也可以
直接在輸入框打字送出修改意見，AI 會依意見重新規劃，直到你核准或取消為止。
規劃階段完全不會呼叫任何工具，即使 AI 不小心在計畫裡夾帶了 EXECUTE 指令
也不會被執行——確認關卡是靠系統不執行工具保證的，不是單純提醒 AI 而已。

按下核准後會自動退出 Plan 模式（取消或送修改意見則維持在 Plan 模式，方便重新
規劃）。核准的計畫會存進系統提示詞（跟 Sticky Objective 同一種做法），這個任務
執行期間，不論經過多少輪工具決策、甚至觸發自動壓縮，AI 都不會忘記它；等你送出
下一個新任務時會自動清除，不會殘留干擾新任務。若想提早清除可輸入 /plan done。

單一工具回傳若超過 {threshold} tokens，不論目前是什麼模式，都不會直接進主對話：
預設交給一個獨立、乾淨的 session 做「任務導向摘要」——它拿到完整原始輸出、
使用者的目標（Objective／這一輪任務的原始敘述／已核准的計畫）與這一步的目的
（AI 剛才的想法與執行的工具），只擷取跟任務有關的事實，名稱與數值照抄、不推測，
並明說原始輸出沒有涵蓋什麼；主對話收到的就是這份重點（右欄 🧠 卡片顯示同一份
內容）。所有工具回傳一律如此，包括 topic 擷取、狀態觀察、語義地圖這類分析型輸出
（腳本回傳完整資訊，重要與否由知道任務的獨立 session 判斷）；只有技能規格文件
例外、一律完整放行。/summarize off 改回只給成功／失敗判定（省一次模型呼叫，但 AI
拿不到內容）；摘要 session 失敗時也自動退回判定，不影響主流程。完整原始內容永遠
都會顯示在「系統 / 工具回傳」面板並標記 ⚠️ 待確認，需自行點「✅ 我已確認」。

輸入框旁的 📷 可以附加影像：「框選畫面」會擷取螢幕並進入全螢幕框選（可連續
框選多張，Esc 離開），「選擇檔案」可挑本機的圖片檔；縮圖會排在輸入框上方，
可個別移除。擷取方式自動判斷：伺服器在 WSL（PowerShell）或 Linux X11 桌面時
由伺服器端截圖（多螢幕會先讓你選）；否則改用瀏覽器的「分享畫面」功能由你挑
螢幕（頁面需以 http://localhost 或 https 開啟）。注意伺服器端截的是執行
web_console 那台機器的螢幕。送出訊息時，附加的影像先由獨立的視覺 sub-session（模型
{vision_model}）依你的訊息內容做分析，分析結果以文字連同你的訊息一起交給
主 Agent（右欄會多一張「🖼️ 視覺分析」卡片），主對話本身維持純文字，不影響
壓縮與 token 統計。視覺分析結果不套用工具回傳的精簡門檻（它是影像唯一的文字
表示），超過門檻時卡片會標 ⚠️ 待確認提醒你留意長度。影像只在送出一次新任務時使用，送出後即清空；slash 指令
與計畫核准／修改意見不會消耗附加的影像。只附圖不打字送出時，會用預設的
「請描述這些影像的內容」當作提示詞。

Token 計量：AI 回覆與整體上下文大小以 Ollama 回報的精確值為準（eval_count／
prompt_eval_count），使用者輸入與工具回傳以每次呼叫後校準的字元比估算，所有
數字都是真實 token 尺度。標題列的 ctx 會顯示目前大小（≈ 代表估算值）與兩道水位。

上下文壓縮採雙水位線：軟水位 {soft_threshold} tokens（num_ctx {num_ctx} 的 {soft_pct}%）只在
「回合結束後」檢查，此時答案已經送到你眼前、模型閒著，順手壓縮不會拉長任何一次回覆；
可壓的舊內容不到 {min_compress} tokens 時不會為了一點空間多花一次模型呼叫。
硬水位 {token_threshold}（{hard_pct}%）是呼叫模型前的最後防線，超過一定同步壓縮。壓縮時保留
最新約 {keep_recent} tokens 的原文（在訊息邊界切、不拆開指令與其結果），其餘與上一份
摘要融合成新的一份結構化摘要（總體情境／關鍵進度／執行結果與錯誤／使用者偏好／未完成
事項），存到 logs/ 並注入系統提示詞；系統提示詞只帶最新一份，不會越滾越長。
開啟 /parallel_cal on 後，軟水位壓縮改在背景執行緒進行，你可以馬上繼續對話，完成時右欄
會出現通知。但要注意：只有 Ollama 真的為模型配置多個 slot、或摘要模型（AGENT_SUMMARY_MODEL）
與主模型不同時，摘要才會與主對話同時推論；目前版本的 Ollama 對多模態模型（gemma4）強制
單 slot，同模型的背景摘要會讓你的下一次對話在 Ollama 內排隊，等待只是搬到下一次呼叫。
算力弱的設備建議維持關閉（序列處理）。

自建技能（/make_skill）：每次腳本執行（成功或失敗）都會記進「操作軌跡」，它存在對話 messages 之外，
上下文壓縮不會沖掉，/trajectory 可以查看。當你一步步引導 AI 把一件事做對之後，輸入
/make_skill <技能名稱> 會把「上一個起點（/clear、計畫核准、上一次 make_skill）之後」的成功步驟、
之前失敗的嘗試與核准過的計畫交給草擬模型（{skill_model}，可用 AGENT_SKILL_MODEL 換更大的模型）
填一份結構化草稿：標題、索引描述、分類、哪些值要變成參數、每步的目的、注意事項。模型不寫任何
程式：系統依範本產生規格文件與一支依序呼叫既有腳本的組合腳本（skills_system/drafts/<名稱>/），
並用實際記錄驗證模型的參數化（代回原值必須一致，否則退回原值並提醒）。左欄會顯示草稿預覽，下方
出現「核准並註冊／重播驗證後註冊／取消」按鈕，也可以直接打字送出修改意見重擬。核准後才會搬進
tools/ 與 scripts/ 並寫入 SKILLS.md，下一次呼叫 AI 就能用 EXECUTE: <名稱> 載入規格再執行。
「重播驗證」會用軌跡中的原值實際跑一次草稿腳本，含會改變狀態的步驟（docker_est、workpackage_send、
change_dir 等）時預覽會先提醒。只有一步的做對經驗請改用 modify_memory --skill 記憶，不必做技能。""".format(
    threshold=TOOL_RESULT_TOKEN_THRESHOLD, vision_model=VISION_MODEL,
    token_threshold=TOKEN_THRESHOLD, soft_threshold=SOFT_TOKEN_THRESHOLD,
    keep_recent=KEEP_RECENT_TOKENS, num_ctx=NUM_CTX, min_compress=MIN_COMPRESS_TOKENS,
    soft_pct=round(SOFT_TOKEN_THRESHOLD / NUM_CTX * 100), hard_pct=round(TOKEN_THRESHOLD / NUM_CTX * 100),
    skill_model=agent.skill_model,
)


def handle_slash_command(message, events):
    """處理 /指令。回傳 True 代表已被當作指令處理，不需再送去給 AI。"""
    text = message.strip()
    lower = text.lower()

    if lower == "/menu":
        events.append({"channel": "system", "text": MENU_TEXT})
        return True
    if lower == "/clear":
        agent.reset_conversation()
        events.append({"channel": "system", "text": "🧹 記憶已清空。"})
        return True
    if lower == "/compress":
        if agent.compress_context_to_file():
            events.append({"channel": "system", "text": "🗜️ 歷史已手動壓縮並歸檔。"})
        else:
            events.append({"channel": "system", "text": "ℹ️ 目前沒有需要壓縮的舊對話。"})
        return True
    if lower == "/parallel_cal on":
        state["parallel_cal"] = True
        events.append({"channel": "system", "text": (
            "⚡ 已開啟平行壓縮：回合結束後超過軟水位時在背景執行緒壓縮，不擋下一次對話"
            "（要真的平行需 Ollama 給此模型多個 slot，或以 AGENT_SUMMARY_MODEL 指定不同的摘要模型；硬水位仍為同步）"
        )})
        return True
    if lower == "/parallel_cal off":
        state["parallel_cal"] = False
        events.append({"channel": "system", "text": "🔁 已關閉平行壓縮：改回序列處理，回合結束後超過軟水位時同步壓縮完才結束本回合"})
        return True
    if lower == "/auto on":
        state["auto_mode"] = True
        events.append({"channel": "system", "text": "🤖 已開啟 Auto Continue 模式"})
        return True
    if lower == "/auto off":
        state["auto_mode"] = False
        events.append({"channel": "system", "text": "🛑 已關閉 Auto Continue 模式"})
        return True
    if lower == "/hybrid on":
        state["hybrid_mode"] = True
        events.append({"channel": "system", "text": "🧬 已開啟 Hybrid 模式"})
        return True
    if lower == "/hybrid off":
        state["hybrid_mode"] = False
        events.append({"channel": "system", "text": "🧬 已關閉 Hybrid 模式"})
        return True
    if lower == "/summarize on":
        state["tool_summary_mode"] = True
        events.append({"channel": "system", "text": "🧠 已開啟工具回傳的任務導向摘要（預設）：超過門檻的結果由獨立 session 依使用者目標與這一步的目的擷取重點"})
        return True
    if lower == "/summarize off":
        state["tool_summary_mode"] = False
        events.append({"channel": "system", "text": "🧠 已關閉工具回傳的任務導向摘要：超過門檻的結果只給 AI 成功/失敗判定（不多花一次模型呼叫）"})
        return True
    if lower == "/plan on":
        state["plan_mode"] = True
        events.append({"channel": "system", "text": "📝 已開啟 Plan 模式（下一個新任務會先規劃步驟，經你核准後才執行；核准後自動退出）"})
        return True
    if lower == "/plan off":
        state["plan_mode"] = False
        events.append({"channel": "system", "text": "📝 已關閉 Plan 模式（恢復直接執行）"})
        return True
    if lower == "/plan done":
        if agent.current_plan:
            agent.current_plan = None
            events.append({"channel": "system", "text": "✅ 已提早清除目前的計畫（system prompt 不再提醒 AI 依計畫執行；平常會在下一個新任務送出時自動清除）"})
        else:
            events.append({"channel": "system", "text": "ℹ️ 目前沒有進行中的計畫"})
        return True
    if lower.startswith("/objective set "):
        agent.sticky_objective = text[len("/objective set "):].strip()
        events.append({"channel": "system", "text": "🎯 已設定 Objective"})
        return True
    if lower == "/objective show":
        events.append({"channel": "system", "text": f"🎯 Current: {agent.sticky_objective or 'None'}"})
        return True
    if lower == "/objective clear":
        agent.sticky_objective = ""
        events.append({"channel": "system", "text": "🧹 已清除 Sticky Objective"})
        return True
    if lower == "/trajectory":
        events.append({"channel": "system", "text": agent.format_trajectory()})
        return True
    if lower == "/make_skill" or lower.startswith("/make_skill "):
        parts = text[len("/make_skill"):].split()
        if not parts:
            events.append({"channel": "system", "text": (
                "用法：/make_skill <技能名稱> [步驟範圍，例如 3-7、3,5,8 或 all]；先用 /trajectory 查看已記錄的步驟。"
            )})
            return True
        name, spec = parts[0], (parts[1] if len(parts) > 1 else None)
        events.append({"channel": "system", "text": f"🧩 正在依操作軌跡草擬技能 {name}（模型 {agent.skill_model}）…"})
        draft, err = agent.start_skill_draft(name, spec)
        if err:
            events.append({"channel": "system", "text": f"⚠️ {err}"})
            return True
        events.append({"channel": "skilldraft", "name": draft["name"], "text": agent.skill_draft_preview(draft)})
        events.append({"channel": "system", "text": (
            "🧩 草稿已寫入 skills_system/drafts/，尚未註冊。下方按鈕：核准並註冊／重播驗證後註冊／取消；"
            "也可以直接在輸入框送出修改意見，系統會重擬草稿。"
        )})
        return True
    if lower == "/skills":
        lines, cat = ["📘 可用技能（/skill <名稱> 或「/」選單可手動載入規格）："], None
        for sk in agent.list_skills():
            if sk["category"] != cat:
                cat = sk["category"]; lines.append(f"\n【{cat}】")
            lines.append(f"  {sk['name']} — {sk['description']}" + ("（含經驗記憶）" if sk["has_memory"] else ""))
        events.append({"channel": "system", "text": "\n".join(lines)})
        return True
    if lower == "/skill" or lower.startswith("/skill "):
        try:
            info = load_pending_skill(text[len("/skill"):])
        except ValueError as e:
            events.append({"channel": "system", "text": f"⚠️ {e}"})
            return True
        if info["already"]:
            events.append({"channel": "system", "text": f"ℹ️ 技能 {info['name']} 的規格已在待送清單。"})
            return True
        events.append({
            "channel": "skillload", "name": info["name"], "tokens": info["tokens"],
            "text": info["doc"],
        })
        events.append({"channel": "system", "text": (
            f"📘 已手動載入技能 {info['name']} 的規格（≈{info['tokens']} tokens），會隨你下一則訊息一起送出；"
            f"輸入框上方的 chip 可移除。"
        )})
        return True

    return False
