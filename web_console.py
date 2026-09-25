"""
Web Console for SkillAgent

一個純標準庫（不需要額外安裝 flask / fastapi）的網頁版操作介面，
取代在終端機裡跑 Agent_Runner.py 的方式。

設計原則：
- 完全不修改 Agent_Runner.py，只 import 其中的 SkillAgent 類別與
  _append_discarded_tool_result 這個共用函式，重用既有邏輯。
- 畫面拆成兩塊：
    左邊「使用者 ↔ Agent 對話」：使用者輸入與 AI 的文字回應。
    右邊「系統 / 工具回傳」：AI 回覆 JSON 的 action 觸發的規格書載入或腳本執行結果、以及 💭 思考。
- CLI 版本原本用 /auto on、/compress 這類指令切換模式；這裡沿用同樣的
  指令字串，並新增 /menu 可以查詢目前支援哪些指令。
- 即時串流：後端每完成一次推論或工具執行，就立刻把該筆事件以 NDJSON
  （一行一個 JSON）寫回 HTTP 回應並 flush，前端邊收邊渲染，不必等整個
  回合（可能是多輪 ask_ai -> run_tool）跑完才一次看到全部結果。
  實作上把原本累積用的 events list 換成 EventStream 物件，append 即送出，
  所以 run_turn / apply_decision / handle_plan_response 等流程函式完全不用改。

執行方式：
    python3 web_console.py
    然後瀏覽器打開 http://127.0.0.1:8765
"""

import json
import os
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# TOKEN_THRESHOLD（整體上下文自動壓縮門檻）、TOOL_RESULT_TOKEN_THRESHOLD
# （單一工具回傳精簡門檻）與 _content_for_context 都是核心邏輯，定義在
# Agent_Runner.py 裡，CLI（main()）與這裡共用同一份，避免兩邊各自維護一份
# 而逐漸產生行為落差。
from Agent_Runner import (
    action_text,
    SkillAgent,
    _append_discarded_tool_result,
    _content_for_context,
    after_turn_compression,
    attach_skill_docs,
    NUM_CTX,
    TOKEN_THRESHOLD,
    SOFT_TOKEN_THRESHOLD,
    KEEP_RECENT_TOKENS,
    TOOL_RESULT_TOKEN_THRESHOLD,
    TOOL_SUMMARY_DEFAULT,
    context_kind,
    tool_result_message,
    MIN_COMPRESS_TOKENS,
    PARALLEL_CAL_DEFAULT,
    is_skill_doc_result,
    is_exempt_result,
)

# 📷 多模態影像：附圖走 vision library 的獨立視覺 sub-session（見 _run_vision_subsession），
# 主對話永遠是純文字，既有的壓縮／token 計算／滑動視窗都不需要知道影像的存在。
from vision import (
    DEFAULT_MODEL as VISION_MODEL,
    SUBSESSION_SYSTEM_PROMPT,
    VisionError,
    VisionSession,
    analyze as vision_analyze,
)
from vision.web import static_file as vision_static_file

# =========================================================
# Agent 狀態（單一使用者、單一 Agent 實例）
# =========================================================

agent = SkillAgent(model=os.environ.get("WEB_CONSOLE_MODEL", "gemma4:e4b"), max_history=None)  # 則數視窗停用，統一以 token 門檻壓縮
agent.reset_conversation()

state = {
    "auto_mode": False,
    "hybrid_mode": False,
    "tool_summary_mode": TOOL_SUMMARY_DEFAULT,  # 超過門檻的工具回傳交給獨立 session 做任務導向摘要（預設開）
    "plan_mode": False,
    "parallel_cal": PARALLEL_CAL_DEFAULT,  # 軟水位壓縮改在背景執行緒做（/parallel_cal on|off）
}
pending = {"result": None, "mode": None, "tokens": None}  # 等待使用者決策的工具結果（hybrid / manual 模式用）
plan_pending = {"active": False, "text": None}  # 等待使用者核准／修改意見的任務計畫（/plan 模式用）
vision_session = VisionSession()  # 📷 尚未送出的影像附件（框選截圖／上傳的檔案），送出新任務時一次消費
lock = threading.Lock()

# 📘 使用者從「/」選單（或 /skill <名稱>）手動載入、尚未送出的技能規格：name -> block。
# 跟 📷 附件同一種生命週期：送出下一個新任務時一次消費（slash 指令、計畫回應、工具決策不會）。
pending_skills = {}
pending_skills_lock = threading.Lock()

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

DEFAULT_SKILL_PROMPT = "我已手動載入上述技能的規格，請依規格用兩三句話說明它的用途與呼叫方式，然後等待我的指示。"


def load_pending_skill(name):
    """把技能規格加入待送清單。回傳 {name, doc, tokens, pending}；技能不存在時 raise ValueError。"""
    name = (name or "").strip()
    if name.endswith(".md"):
        name = name[:-3]
    block = agent.manual_skill_block(name) if name else None
    if block is None:
        raise ValueError(f"找不到技能 '{name}'，請用 /skills 或「/」選單查看可用名稱。" if name else "用法：/skill <技能名稱>")
    with pending_skills_lock:
        already = name in pending_skills
        pending_skills[name] = block
        pending = list(pending_skills)
    return {"name": name, "doc": block, "tokens": agent.count_tokens(block), "pending": pending, "already": already}


def remove_pending_skill(name):
    with pending_skills_lock:
        pending_skills.pop((name or "").strip(), None)
        return list(pending_skills)


def pending_skill_names():
    with pending_skills_lock:
        return list(pending_skills)


def take_all_pending_skills():
    """送出新任務時一次取走全部待送的技能規格（回傳 [(name, block), ...]）。"""
    with pending_skills_lock:
        items = list(pending_skills.items())
        pending_skills.clear()
    return items

MAX_AUTO_ITERATIONS = 25  # 安全防護：避免 auto 模式下模型無限迴圈卡住伺服器

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


# =========================================================
# 核心流程：重用 Agent_Runner.SkillAgent 的方法，改寫成
# 「每次呼叫處理一小段、把過程逐筆推給前端」的形式。
# 下面各函式的 events 參數實際上是 EventStream（見 HTTP Server 區塊），
# 只用到 .append()，每 append 一筆就立刻串流送到瀏覽器。
# =========================================================

def current_mode_label():
    if state["auto_mode"]:
        return "auto"
    if state["hybrid_mode"]:
        return "hybrid"
    return "manual"


def build_stats():
    return {
        "mode": current_mode_label(),
        "tool_summary_mode": state["tool_summary_mode"],
        "plan_mode": state["plan_mode"],
        "current_plan": agent.current_plan or None,
        "current_cwd": agent.current_cwd,
        "container_cwd": agent.container_cwd,
        "target_container": agent.target_container,
        "objective": agent.sticky_objective or None,
        "total_user_tokens": agent.total_user_tokens,
        "total_ai_tokens": agent.total_ai_tokens,
        "total_tool_tokens": agent.total_tool_tokens,
        "context_tokens": agent.context_tokens(),
        "context_exact": agent.last_prompt_tokens is not None,
        "token_threshold": TOKEN_THRESHOLD,
        "soft_threshold": SOFT_TOKEN_THRESHOLD,
        "keep_recent_tokens": KEEP_RECENT_TOKENS,
        "num_ctx": NUM_CTX,
        "chars_per_token": round(agent.chars_per_token, 2),
        "attachments": vision_session.count(),
        "pending_skills": pending_skill_names(),
        "vision_model": VISION_MODEL,
        "summary_model": agent.summary_model,
        "skill_model": agent.skill_model,
        "skill_draft": agent.pending_skill_draft["name"] if agent.pending_skill_draft else None,
        "parallel_cal": state["parallel_cal"],
        "compressing": agent.compression_in_progress(),
        "last_compression": agent.last_compression,
        # 背景壓縮完成／失敗的通知：每次組 stats 時取走（stats 會隨每個事件與 /api/status 送到前端，
        # 前端把它們渲染成系統訊息），同一則不會重複出現
        "notices": agent.pop_notices(),
    }


def _current_tool_action():
    """剛執行完的工具是哪個 action：run_turn 與 apply_decision 的時間點，最後一則 assistant 都還是下這個工具的那一輪。"""
    return (agent._last_assistant_step() or {}).get("action")


def _emit_context_event(content, events, tool_tokens=None):
    """每次工具結果決定好要餵給主對話什麼之後，都推一個「AI 實際收到的內容」事件到右欄，緊接在完整原文卡片之後：
    summary（任務導向摘要）與 reduced（只有成功／失敗）整段顯示；raw／doc（完整原文、規格文件）只給一行提示，
    因為上一張卡片就是同一份內容。使用者永遠同時看得到完整原文與 AI 收到的版本。"""
    kind = context_kind(content, tool_tokens)
    n = agent.count_tokens(content)
    if kind in ("summary", "reduced"):
        events.append({"channel": "summary", "kind": kind, "text": content, "tokens": n})
    elif kind == "doc":
        events.append({"channel": "summary", "kind": kind, "text": f"完整規格文件（≈{n} tokens，不受門檻限制），與上方卡片相同。", "tokens": n})
    else:
        events.append({"channel": "summary", "kind": kind, "text": f"完整原文（≈{n} tokens，未縮減），與上方系統回傳卡片相同。", "tokens": n})


DEFAULT_VISION_PROMPT = "請描述這些影像的內容，並逐字列出可見的文字、數值、錯誤訊息與任何值得注意的異常。"


def _run_vision_subsession(message, images, events):
    """📷 附圖的獨立視覺 sub-session（跟 SkillAgent.summarize_tool_result 同一種模式）：
    影像 + 使用者訊息交給視覺模型得到文字，主對話只收到文字、不接觸影像 bytes，
    所以 compress_context_to_file / count_tokens / _truncate_memory 全都不用改。
    代價是主 Agent 看到的是描述而非原圖，追問時要重新附圖。
    回傳要放進主對話的完整使用者訊息內容（原文 + [vision result] 區塊）。"""
    n = len(images)
    events.append({"channel": "system", "text": f"🖼️ 視覺推論中（{n} 張影像，模型 {VISION_MODEL}）..."})
    try:
        result = vision_analyze(images, message, system_prompt=SUBSESSION_SYSTEM_PROMPT)
    except VisionError as e:
        result = f"[ERROR] 視覺分析失敗：{e}"
    tokens = agent.count_tokens(result)
    agent.total_tool_tokens += tokens
    # 不套用 TOOL_RESULT_TOKEN_THRESHOLD 的精簡：這段文字是影像唯一的表示，砍成成功／失敗
    # 就沒有資訊了。超過門檻只標 ⚠️ 提醒使用者留意長度（sub-session prompt 已要求精簡）。
    oversized = tokens > TOOL_RESULT_TOKEN_THRESHOLD
    events.append({"channel": "vision", "text": result, "tokens": tokens, "count": n, "oversized": oversized})
    if oversized:
        events.append({
            "channel": "system",
            "text": (
                f"⚠️ 視覺分析結果約 {tokens} tokens，超過門檻 {TOOL_RESULT_TOKEN_THRESHOLD}，已標記待人工確認；"
                f"因為它是影像唯一的文字表示，內容仍會完整交給主 Agent。"
            ),
        })
    # 這段接在使用者訊息尾端、以 user 角色送進主對話，模型容易把它當成「使用者寫的」而回覆
    # 「你提供的視覺分析」。這裡明確標示來源是系統的視覺模型；AGENT.md「Harness Messages」也有對應說明。
    return (
        f"{message}\n\n"
        f"[vision result]\n"
        f"【系統影像分析】使用者只提供了 {n} 張影像，沒有寫下面這段文字；以下由系統的視覺模型（{VISION_MODEL}）"
        f"針對上述訊息自動產生。你看不到原圖，請把它當作系統回傳的分析結果來回應或決定下一步，"
        f"回覆時稱「影像分析結果」，不要說成使用者提供的分析。\n{result}"
    )


def _emit_reply_meta(parsed, events):
    """回覆協議的附帶資訊：💭 思考推到右欄；不是合法 JSON 時先推一則降級警告（放在正文之前，最後一筆仍是正文）。"""
    if agent.last_reply_retry:
        events.append({"channel": "system", "text": agent.last_reply_retry})
    if not parsed["valid"]:
        events.append({"channel": "system", "text": "⚠️ 模型輸出不是合法的 JSON 回覆，已降級為純文字顯示，本輪不執行任何指令。"})
    if parsed["thought"]:
        events.append({"channel": "thought", "text": parsed["thought"]})


def _ask_and_present_plan(events):
    """呼叫一次 ask_ai() 取得計畫文字，推到 events 給前端顯示，並把
    plan_pending 標記為待核准。跟 CLI 的 _run_plan_flow 用同一套
    SkillAgent.build_plan_request / build_plan_revision_request，
    只是這裡拆成「單次 HTTP 請求處理一小段」的非同步形式。"""
    plan_raw = agent.ask_ai()
    agent.total_ai_tokens += agent.last_ai_tokens(plan_raw)
    if agent.auto_compressed:
        events.append({"channel": "system", "text": "📦 上下文超過門檻，呼叫前已自動壓縮並歸檔。"})
    agent.messages.append({'role': 'assistant', 'content': plan_raw})
    parsed = agent.parse_reply(plan_raw)
    _emit_reply_meta(parsed, events)
    plan_msg = parsed["reply"] or plan_raw  # 計畫文字在 reply；就算模型夾帶了 action，規劃階段也不會執行
    events.append({"channel": "plan", "text": plan_msg})
    plan_pending["active"] = True
    plan_pending["text"] = plan_msg


def start_plan_flow(user_task, events):
    """/plan 模式：把使用者任務包裝成規劃請求送出，取得第一版計畫。"""
    agent.messages.append({'role': 'user', 'content': agent.build_plan_request(user_task)})
    _ask_and_present_plan(events)


def handle_plan_response(text, events):
    """處理使用者對目前待核准計畫的回應（y／n／修改意見三選一，比照 CLI）。

    安全設計跟 CLI 版一致：這個函式從頭到尾不會呼叫 agent.run_tool()，
    確認關卡不依賴 AI 是否遵守「先別執行」的指示。

    回傳 "approved" / "rejected" / "revised"。
    """
    choice = text.strip()

    if choice.lower() == 'y':
        agent.confirm_plan(plan_pending["text"])  # 存 current_plan、加 [PLAN_CONFIRMED]、軌跡記起點（與 CLI 共用）
        plan_pending["active"] = False
        plan_pending["text"] = None
        # 核准即退出 Plan 模式：Plan 模式的意義是「下一個新任務先規劃」，規劃階段到此結束；
        # 這個任務接著依 current_plan 執行，下一個新任務會直接執行（要再規劃請重新 /plan on）。
        # 取消（n）或送修改意見則維持 Plan 模式，方便重新描述任務再規劃。
        if state["plan_mode"]:
            state["plan_mode"] = False
            events.append({"channel": "system", "text": (
                "📝 計畫已核准，已自動退出 Plan 模式：這個任務會依計畫執行；"
                "下一個新任務將直接執行（要再規劃請重新 /plan on）。"
            )})
        return "approved"

    if choice == "" or choice.lower() in ("n", "no"):
        agent.messages.append({
            'role': 'user',
            'content': "[PLAN_REJECTED]\n使用者取消了上述計畫，本次任務不會執行，請等待使用者的新指示。"
        })
        plan_pending["active"] = False
        plan_pending["text"] = None
        events.append({"channel": "system", "text": "🚫 已取消，本次任務不會執行。"})
        return "rejected"

    # 其餘輸入視為修改意見，重新規劃一次
    agent.messages.append({'role': 'user', 'content': agent.build_plan_revision_request(choice)})
    _ask_and_present_plan(events)
    return "revised"


def handle_skill_draft_response(text, events):
    """處理使用者對待決定技能草稿的回應（比照計畫核准）：y 核准並註冊、t 先重播驗證再註冊、
    n／空白取消、其他文字＝修改意見重擬。狀態在 agent.pending_skill_draft（與 CLI 共用）。
    回傳 "registered" / "cancelled" / "revised" / "failed"（失敗時草稿仍待決定）。"""
    choice = text.strip()
    lower = choice.lower()
    if lower in ("y", "t"):
        if lower == "t":
            events.append({"channel": "system", "text": "🧪 正在以軌跡中的原值重播草稿腳本…"})
        ok, msg = agent.approve_skill_draft(replay=(lower == "t"))
        events.append({"channel": "system", "text": msg})
        return "registered" if ok else "failed"
    if choice == "" or lower in ("n", "no"):
        events.append({"channel": "system", "text": agent.cancel_skill_draft()})
        return "cancelled"
    events.append({"channel": "system", "text": "🧩 依修改意見重新草擬技能…"})
    draft, err = agent.revise_skill_draft(choice)
    if err:
        events.append({"channel": "system", "text": f"⚠️ {err}（上一版草稿仍待決定）"})
        return "failed"
    events.append({"channel": "skilldraft", "name": draft["name"], "text": agent.skill_draft_preview(draft)})
    return "revised"


def run_turn(events):
    """反覆執行 ask_ai -> run_tool，直到這一回合自然結束（沒有工具需要執行），
    或是需要使用者對工具結果做決策為止（hybrid / manual 模式）。

    回傳 True 代表目前正在等待使用者決策（awaiting_decision）。
    """
    for _ in range(MAX_AUTO_ITERATIONS):
        ai_msg = agent.ask_ai()
        ai_tokens = agent.last_ai_tokens(ai_msg)
        agent.total_ai_tokens += ai_tokens
        if agent.auto_compressed:
            events.append({"channel": "system", "text": "📦 上下文超過門檻，呼叫前已自動壓縮並歸檔。"})
        agent.messages.append({'role': 'assistant', 'content': ai_msg})
        parsed = agent.parse_reply(ai_msg)  # 回覆協議：左欄顯示 reply，執行只看 action，reply 裡的指令文字不會被執行
        _emit_reply_meta(parsed, events)
        events.append({
            "channel": "chat", "role": "assistant", "tokens": ai_tokens,
            "text": parsed["reply"] or ("（本輪沒有文字回覆）" if parsed["action"] else "（空白回覆）"),
            "action": action_text(parsed["action"]) if parsed["action"] else None,
        })

        result = agent.run_tool(parsed)
        tool_tokens = 0
        if result:
            tool_tokens = agent.count_tokens(result)
            agent.total_tool_tokens += tool_tokens
            # 規格文件載入不受門檻限制（_content_for_context 會完整放行），不標 ⚠️
            oversized = tool_tokens > TOOL_RESULT_TOKEN_THRESHOLD and not is_exempt_result(result, tool_tokens)
            events.append({
                "channel": "tool",
                "text": result,
                "tokens": tool_tokens,
                "oversized": oversized,
                "result_id": agent.last_result_id,      # 📄 存檔編號（規格載入時為 None）；卡片顯示並可開啟 /api/results/<id>
                "result_file": agent.last_result_file,
            })
            if oversized:
                events.append({
                    "channel": "system",
                    "text": (
                        f"⚠️ 此工具回傳約 {tool_tokens} tokens，超過門檻 "
                        f"{TOOL_RESULT_TOKEN_THRESHOLD}，已標記待人工確認；"
                        + ("加入上下文前將交由獨立 session 依目前任務擷取重點（🧠 卡片會顯示 AI 實際收到的內容）。"
                           if state["tool_summary_mode"] else
                           "AI 只會收到成功／失敗判定（/summarize on 可改為獨立 session 摘要）。")
                    ),
                })
        else:
            events.append({"channel": "system", "text": "✅ 無工具需要執行"})

        # 硬水位的壓縮檢查統一在 ask_ai() 呼叫前（ensure_context_budget，UI 會收到 📦 事件），
        # 軟水位在回合結束後（after_turn_compression，見 ConsoleHandler._finish_turn）。
        # 舊版這裡壓縮後 continue，會跳過下面「把工具結果加入上下文」的步驟直接再問一次 AI，
        # AI 拿不到剛執行的結果而重複下同一個指令；result 為 None 時也會多問一輪而不是結束回合。已移除。

        if not result:
            return False

        if state["auto_mode"]:
            content = _content_for_context(
                result, tool_tokens, agent=agent, use_summary=state["tool_summary_mode"]
            )
            _emit_context_event(content, events, tool_tokens)
            agent.messages.append({'role': 'user', 'content': tool_result_message(content, _current_tool_action())})
            events.append({"channel": "system", "text": "♻️ Auto Continue 中..."})
            continue

        # hybrid / manual 都需要暫停，等待使用者對這次工具結果做決策
        pending["result"] = result
        pending["mode"] = "hybrid" if state["hybrid_mode"] else "manual"
        pending["tokens"] = tool_tokens
        return True

    events.append({"channel": "system", "text": "⚠️ 已達安全上限（連續執行過多輪工具），本回合自動中止。"})
    return False


def apply_decision(action, events):
    """套用使用者對待處理工具結果的決策，回傳是否要繼續本回合的迴圈。"""
    result = pending["result"]
    mode = pending["mode"]
    tool_tokens = pending["tokens"] or 0
    pending["result"] = None
    pending["mode"] = None
    pending["tokens"] = None

    # 只有真的要把結果加入上下文時才計算 content——若是摘要模式，這會觸發
    # 一次獨立的 ollama 呼叫，使用者選擇「捨棄」時就不需要浪費這次呼叫。
    if mode == "hybrid":
        if action == "y":
            content = _content_for_context(
                result, tool_tokens, agent=agent, use_summary=state["tool_summary_mode"]
            )
            _emit_context_event(content, events, tool_tokens)
            agent.messages.append({'role': 'user', 'content': tool_result_message(content, _current_tool_action())})
        else:
            _append_discarded_tool_result(agent)
        return True  # hybrid 不論加入或捨棄，都會讓 AI 接續推論

    # manual 模式
    if action == "y":
        content = _content_for_context(
            result, tool_tokens, agent=agent, use_summary=state["tool_summary_mode"]
        )
        _emit_context_event(content, events, tool_tokens)
        agent.messages.append({'role': 'user', 'content': tool_result_message(content, _current_tool_action())})
        return True
    if action == "stop":
        return False
    _append_discarded_tool_result(agent)
    return False


def clear_plan_for_new_task(events):
    """新任務送出時，把上一個已核准的計畫從 system prompt 清掉。

    計畫的生命週期 = 核准後那個任務的執行期間：期間所有工具決策（manual／hybrid 的 y/n）、
    auto 迴圈、自動壓縮都不會清掉它；使用者再打字送出一句新訊息（非 slash 指令、非計畫回應、
    非工具決策）就視為新任務。舊版要求手動 /plan done，實際上容易忘記，舊計畫會殘留在
    system prompt 干擾之後的每個任務。要提早清除仍可用 /plan done。"""
    if agent.current_plan:
        agent.current_plan = None
        events.append({"channel": "system", "text": "🧹 上一個已核准的計畫已隨新任務自動清除（system prompt 不再要求依舊計畫執行）。"})


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


# =========================================================
# 前端頁面（純 HTML / CSS / JS，無外部依賴、可離線使用）
# =========================================================

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<title>SkillAgent Web Console</title>
<link rel="stylesheet" href="/static/vision/snip.css">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, "Segoe UI", "PingFang TC", "Microsoft JhengHei", sans-serif;
    background: #1e1f22; color: #e3e3e3; height: 100vh; display: flex; flex-direction: column;
  }
  header {
    padding: 10px 16px; background: #2b2d31; border-bottom: 1px solid #3a3c40;
    display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px;
  }
  header h1 { font-size: 16px; margin: 0; }
  #status { font-size: 12px; color: #9aa0a6; display: flex; gap: 14px; flex-wrap: wrap; }
  main { flex: 1; display: flex; min-height: 0; }
  .panel { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .panel + .panel { border-left: 1px solid #3a3c40; }
  .panel h2 {
    font-size: 13px; margin: 0; padding: 8px 12px; background: #26282c;
    border-bottom: 1px solid #3a3c40; color: #c7c9cc;
  }
  .log { flex: 1; overflow-y: auto; padding: 10px 12px; font-size: 13px; }
  .entry { margin-bottom: 10px; padding: 8px 10px; border-radius: 6px; white-space: pre-wrap; word-break: break-word; }
  .entry.user { background: #2c3e50; }
  .entry.assistant { background: #2b2d31; border: 1px solid #3a3c40; }
  .entry.tool { background: #1f2a24; border: 1px solid #2f4a3a; font-family: "Cascadia Code", Consolas, monospace; }
  .entry.system { background: #2a2620; border: 1px solid #4a4030; color: #d8c9a3; font-style: italic; }
  .entry.thought { background: #23262b; border: 1px dashed #4a4c50; color: #a7acb3; font-size: 12px; }
  .entry.thought .tag { color: #8f96a0; opacity: 1; }
  .entry.summary { background: #241f33; border: 1px solid #5a4a8f; color: #cfc3f0; }
  .entry.summary .tag { color: #b39ddb; opacity: 1; }
  .entry.summary.reduced { background: #332a1f; border-color: #8f6a4a; color: #f0dcc3; }
  .entry.summary.reduced .tag { color: #dbb59d; }
  .entry.summary.raw, .entry.summary.doc { background: transparent; border: 1px dashed #5a4a8f; color: #9d94b5; padding: 4px 8px; font-size: 12px; }
  .entry.summary.raw .tag, .entry.summary.doc .tag { color: #9d94b5; }
  .entry .result-link { display: block; margin-top: 4px; font-size: 12px; color: #8ab4f8; text-decoration: none; }
  .entry .result-link:hover { text-decoration: underline; }
  .entry.plan { background: #16302c; border: 1px solid #2f6f5e; }
  .entry.plan .tag { color: #4fd8ba; opacity: 1; }
  .entry.skilldraft { background: #26203a; border: 1px solid #6d5aa8; font-family: "Cascadia Code", Consolas, monospace; font-size: 12px; }
  .entry.skilldraft .tag { color: #c4b0ff; opacity: 1; font-family: inherit; }
  .entry.tool.oversized { border: 1px solid #b0873f; box-shadow: 0 0 0 1px #b0873f inset; }
  .entry.tool.oversized .tag { color: #e6b95c; opacity: 1; }
  .entry.tool.oversized.reviewed { border-color: #2f4a3a; box-shadow: none; opacity: 0.75; }
  .entry .tag { font-size: 10px; text-transform: uppercase; opacity: 0.6; margin-bottom: 4px; }
  .entry .ack-btn {
    display: inline-block; margin-top: 6px; padding: 4px 10px; font-size: 11px;
    background: #4a4c50; border-radius: 4px; cursor: pointer;
  }
  footer { border-top: 1px solid #3a3c40; padding: 10px 12px; background: #2b2d31; }
  #decision-bar, #plan-bar, #skill-draft-bar { display: none; margin-bottom: 8px; gap: 8px; align-items: center; font-size: 13px; flex-wrap: wrap; }
  #decision-bar.show, #plan-bar.show, #skill-draft-bar.show { display: flex; }
  #decision-bar button, #plan-bar button, #skill-draft-bar button { cursor: pointer; }
  .input-row { display: flex; gap: 8px; }
  textarea#msg {
    flex: 1; resize: none; height: 54px; background: #1e1f22; color: #e3e3e3;
    border: 1px solid #3a3c40; border-radius: 6px; padding: 8px; font-size: 13px; font-family: inherit;
  }
  button { background: #3a6df0; color: white; border: none; border-radius: 6px; padding: 8px 16px; font-size: 13px; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  button.secondary { background: #4a4c50; }
  button.danger { background: #b0473f; }

  /* ===== 📷 影像附件 ===== */
  .entry.vision { background: #1f2b33; border: 1px solid #3d6b80; color: #cfe6f0; }
  .entry.vision .tag { color: #7fc8e8; opacity: 1; }
  .entry.vision.oversized { border-color: #b0873f; box-shadow: 0 0 0 1px #b0873f inset; }
  .entry.vision.oversized .tag { color: #e6b95c; }
  .entry.vision.oversized.reviewed { border-color: #3d6b80; box-shadow: none; opacity: 0.75; }
  .entry.user .attach-note { font-size: 11px; color: #9fc3e6; margin-top: 4px; }
  #attach-strip { display: none; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; align-items: center; }
  #attach-strip.show { display: flex; }
  #attach-hint { font-size: 12px; color: #9aa0a6; }
  .attach-item { position: relative; }
  .attach-item img { width: 72px; height: 54px; object-fit: cover; border-radius: 4px; border: 1px solid #3a3c40; display: block; }
  .attach-item .rm {
    position: absolute; top: -6px; right: -6px; width: 18px; height: 18px; border-radius: 50%;
    background: #b0473f; color: white; font-size: 11px; line-height: 18px; text-align: center; cursor: pointer;
  }
  .input-row { position: relative; }
  #attach-btn { white-space: nowrap; }
  #attach-menu {
    display: none; position: absolute; bottom: 62px; left: 0; z-index: 100;
    background: #26282c; border: 1px solid #3a3c40; border-radius: 8px; padding: 6px;
    flex-direction: column; gap: 4px;
  }
  #attach-menu.show { display: flex; }
  #attach-menu button { background: #34363b; text-align: left; cursor: pointer; }
  #attach-menu button:hover { background: #3a6df0; }

  /* ===== 「/」選單：功能開關／指令 + 技能（手動按需載入規格）===== */
  #slash-menu {
    display: none; position: absolute; bottom: 62px; left: 0; right: 0; z-index: 110;
    max-height: 46vh; overflow-y: auto; background: #26282c; border: 1px solid #3a3c40;
    border-radius: 8px; padding: 6px; font-size: 13px;
  }
  #slash-menu.show { display: block; }
  .slash-section { padding: 6px 8px 2px; font-size: 11px; color: #c7c9cc; letter-spacing: .04em; }
  .slash-section + .slash-section, .slash-group + .slash-section, .slash-item + .slash-section {
    border-top: 1px solid #3a3c40; margin-top: 6px; padding-top: 8px;
  }
  .slash-group { padding: 4px 10px 0; font-size: 11px; color: #7f8a96; }
  .slash-item { display: flex; gap: 10px; align-items: baseline; padding: 5px 10px; border-radius: 6px; cursor: pointer; }
  .slash-item:hover, .slash-item.active { background: #3a6df0; color: white; }
  .slash-item .name { font-family: "Cascadia Code", Consolas, monospace; white-space: nowrap; }
  .slash-item .desc { color: #b8bcc2; font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .slash-item:hover .desc, .slash-item.active .desc { color: #e8eefc; }
  .slash-item .badge { font-size: 10px; background: #4a4c50; border-radius: 4px; padding: 1px 5px; color: #d8c9a3; white-space: nowrap; }
  .slash-empty { padding: 8px 10px; color: #9aa0a6; }
  #skill-strip { display: none; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; align-items: center; }
  #skill-strip.show { display: flex; }
  #skill-hint { font-size: 12px; color: #9aa0a6; }
  .skill-chip {
    background: #1f2a24; border: 1px solid #2f4a3a; color: #bfe3cf; border-radius: 12px; padding: 2px 8px;
    font-size: 12px; display: inline-flex; gap: 6px; align-items: center;
  }
  .skill-chip .rm { cursor: pointer; color: #e6b95c; }
  .entry.skillload { background: #1f2a24; border: 1px dashed #4fa37a; font-family: "Cascadia Code", Consolas, monospace; }
  .entry.skillload .tag { color: #7fd8a8; opacity: 1; }
</style>
</head>
<body>

<header>
  <h1>🤖 SkillAgent Web Console</h1>
  <div id="status">
    <span id="stat-mode">mode: manual</span>
    <span id="stat-cwd">cwd: -</span>
    <span id="stat-container" title="目前的目標容器：預設空白；AI 成功操作某個容器後自動帶入，之後容器技能可省略名稱。要換容器直接跟 AI 說。">container: -</span>
    <span id="stat-tokens">tokens: -</span>
    <span id="stat-attach"></span>
  </div>
</header>

<main>
  <section class="panel">
    <h2>💬 使用者 ↔ Agent 對話</h2>
    <div class="log" id="chat-log"></div>
  </section>
  <section class="panel">
    <h2>🛠️ 系統 / 工具回傳</h2>
    <div class="log" id="tool-log"></div>
  </section>
</main>

<footer>
  <div id="decision-bar">
    <span>⏸️ 有工具結果待決策：</span>
    <button onclick="sendDecision('y')">✅ 加入上下文</button>
    <button class="secondary" id="decision-n">🚫 捨棄</button>
    <button class="danger" id="decision-stop" onclick="sendDecision('stop')">⏹️ 停止本回合</button>
  </div>
  <div id="plan-bar">
    <span>📝 有計畫待你核准（也可以直接在下方輸入修改意見送出，AI 會重新規劃）：</span>
    <button onclick="sendPlanDecision('y')">✅ 核准並執行</button>
    <button class="danger" onclick="sendPlanDecision('n')">🚫 取消任務</button>
  </div>
  <div id="skill-draft-bar">
    <span>🧩 有技能草稿待你決定（也可以直接在下方輸入修改意見送出，系統會重擬）：</span>
    <button onclick="sendSkillDraftDecision('y')">✅ 核准並註冊</button>
    <button class="secondary" onclick="sendSkillDraftDecision('t')">🧪 重播驗證後註冊</button>
    <button class="danger" onclick="sendSkillDraftDecision('n')">🚫 取消草稿</button>
  </div>
  <div id="attach-strip"><span id="attach-hint">📎 已附加影像（送出時一起分析）：</span></div>
  <div id="skill-strip"><span id="skill-hint">📘 已載入技能規格（隨下一則訊息一起送出）：</span></div>
  <div class="input-row">
    <div id="slash-menu"></div>
    <div id="attach-menu">
      <button onclick="attachFromScreen()">🖥️ 框選畫面</button>
      <button onclick="attachFromFile()">📁 選擇檔案</button>
    </div>
    <button class="secondary" id="attach-btn" title="附加影像：框選畫面或選擇檔案" onclick="toggleAttachMenu()">📷</button>
    <textarea id="msg" placeholder="輸入訊息；打「/」選擇功能開關或手動載入技能規格...（Enter 送出，Shift+Enter 換行；📷 可附加影像）"></textarea>
    <button id="send-btn" onclick="sendMessage()">送出</button>
    <input type="file" id="file-input" accept="image/*" multiple style="display:none">
  </div>
</footer>

<script src="/static/vision/snip.js"></script>
<script>
const chatLog = document.getElementById('chat-log');
const toolLog = document.getElementById('tool-log');
const msgBox = document.getElementById('msg');
const sendBtn = document.getElementById('send-btn');
const decisionBar = document.getElementById('decision-bar');
const planBar = document.getElementById('plan-bar');
const skillDraftBar = document.getElementById('skill-draft-bar');
const attachStrip = document.getElementById('attach-strip');
const attachMenu = document.getElementById('attach-menu');
const attachBtn = document.getElementById('attach-btn');
const fileInput = document.getElementById('file-input');

function renderEntry(container, cls, tag, text, oversized) {
  const div = document.createElement('div');
  div.className = 'entry ' + cls + (oversized ? ' oversized' : '');
  if (tag) {
    const tagEl = document.createElement('div');
    tagEl.className = 'tag';
    tagEl.textContent = oversized ? tag + ' ⚠️ 待確認' : tag;
    div.appendChild(tagEl);
  }
  const textEl = document.createElement('div');
  textEl.textContent = text;
  div.appendChild(textEl);
  if (oversized) {
    const ackBtn = document.createElement('div');
    ackBtn.className = 'ack-btn';
    ackBtn.textContent = '✅ 我已確認';
    ackBtn.onclick = () => {
      div.classList.add('reviewed');
      ackBtn.remove();
    };
    div.appendChild(ackBtn);
  }
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function attachResultLink(container, id) {
  // 📄 工具結果存檔：卡片下方加一個連結開啟原文，使用者才知道對話裡的「存檔 #16」是什麼
  const entry = container.lastElementChild;
  if (!entry) return;
  const a = document.createElement('a');
  a.className = 'result-link';
  a.href = '/api/results/' + id;
  a.target = '_blank';
  a.textContent = `開啟結果檔 #${id}（AI 可用 result_grep ${id} <關鍵字> 回查）`;
  entry.appendChild(a);
}

function renderEvents(events) {
  events.forEach(ev => {
    if (ev.channel === 'chat') {
      let note = (ev.role === 'user' && ev.attachments) ? `\n📎 附加了 ${ev.attachments} 張影像` : '';
      if (ev.role === 'user' && ev.skills && ev.skills.length) note += `\n📘 附加了技能規格：${ev.skills.join(', ')}`;
      if (ev.role === 'assistant' && ev.action) note += `\n▶ action: ${ev.action}`;
      renderEntry(chatLog, ev.role, ev.role === 'user' ? '你' : 'AI', ev.text + note);
    } else if (ev.channel === 'thought') {
      renderEntry(toolLog, 'thought', '💭 思考', ev.text);
    } else if (ev.channel === 'skillload') {
      renderEntry(toolLog, 'skillload', `📘 手動載入技能規格：${ev.name}（≈${ev.tokens} tokens，隨下一則訊息送出）`, ev.text);
    } else if (ev.channel === 'vision') {
      renderEntry(toolLog, 'vision', `🖼️ 視覺分析（獨立 session，${ev.count} 張影像）`, ev.text, ev.oversized);
    } else if (ev.channel === 'tool') {
      renderEntry(toolLog, 'tool', '系統回傳' + (ev.result_id ? ` · 📄 已存檔 #${ev.result_id}` : ''), ev.text, ev.oversized);
      if (ev.result_id) attachResultLink(toolLog, ev.result_id);
    } else if (ev.channel === 'summary') {
      // 每次工具回傳都會有這張：AI 實際收到的內容（緊接在完整原文卡片之後）
      const kind = ev.kind || 'summary';
      const labels = {
        summary: '🧠 AI 實際收到的內容：任務導向摘要（獨立 session）',
        reduced: '🧠 AI 實際收到的內容：只有成功／失敗判定（/summarize off 或摘要失敗）',
        raw: '🧠 AI 實際收到的內容：完整原文',
        doc: '🧠 AI 實際收到的內容：完整規格文件',
      };
      renderEntry(toolLog, 'summary ' + kind, labels[kind] || labels.summary, ev.text);
    } else if (ev.channel === 'plan') {
      renderEntry(chatLog, 'plan', '📝 計畫（待你確認）', ev.text);
    } else if (ev.channel === 'skilldraft') {
      renderEntry(chatLog, 'skilldraft', `🧩 技能草稿 ${ev.name}（待你決定：核准／重播驗證／取消，或送出修改意見）`, ev.text);
    } else if (ev.channel === 'system') {
      renderEntry(toolLog, 'system', '系統', ev.text);
    }
  });
}

let compressPoll = null;  // /parallel_cal on 背景壓縮進行中時，定期輪詢狀態以接收完成通知

function updateStatus(stats) {
  document.getElementById('stat-mode').textContent =
    'mode: ' + stats.mode + (stats.parallel_cal ? ' · parallel_cal' : '');
  document.getElementById('stat-cwd').textContent = 'cwd: ' + stats.current_cwd;
  document.getElementById('stat-container').textContent = 'container: ' + (stats.target_container || '-');
  const tokensEl = document.getElementById('stat-tokens');
  tokensEl.textContent =
    `tokens: user ${stats.total_user_tokens} / ai ${stats.total_ai_tokens} / tool ${stats.total_tool_tokens}` +
    ` · ctx ${stats.context_exact ? '' : '≈'}${stats.context_tokens} (軟 ${stats.soft_threshold} / 硬 ${stats.token_threshold})` +
    (stats.compressing ? ' · 🗜️ 背景壓縮中' : '');
  tokensEl.title = `num_ctx ${stats.num_ctx}；軟水位（回合結束後壓縮）${stats.soft_threshold}；` +
    `硬水位（呼叫前必壓）${stats.token_threshold}；壓縮時保留最新約 ${stats.keep_recent_tokens} tokens 原文；` +
    `摘要模型 ${stats.summary_model}`;
  // 背景壓縮完成／失敗的通知（後端在組 stats 時取走，不會重複）
  (stats.notices || []).forEach(t => renderEntry(toolLog, 'system', '系統', t));
  // 背景壓縮進行中：每 4 秒輪詢一次狀態，結束時最後一次輪詢會帶回完成通知
  if (stats.compressing && !compressPoll) {
    compressPoll = setInterval(() => {
      fetch('/api/status').then(r => r.json()).then(updateStatus).catch(() => {});
    }, 4000);
  } else if (!stats.compressing && compressPoll) {
    clearInterval(compressPoll);
    compressPoll = null;
  }
  // 伺服器端附件已被消費（送出新任務）或清空時，同步清掉輸入框上方的縮圖
  if (stats.attachments === 0) clearAttachStrip();
  // 📘 待送的技能規格以伺服器狀態為準
  if (stats.pending_skills) syncSkillStrip(stats.pending_skills);
}

function setBusy(busy) {
  sendBtn.disabled = busy;
  msgBox.disabled = busy;
  attachBtn.disabled = busy;
}

function showDecisionBar(pendingMode) {
  decisionBar.classList.add('show');
  const stopBtn = document.getElementById('decision-stop');
  const nBtn = document.getElementById('decision-n');
  nBtn.onclick = () => sendDecision('n');
  // manual 模式才有獨立的「停止」選項；hybrid 模式捨棄後仍會繼續推論
  stopBtn.style.display = (pendingMode === 'manual') ? 'inline-block' : 'none';
}

function hideDecisionBar() {
  decisionBar.classList.remove('show');
}

function showPlanBar() {
  planBar.classList.add('show');
}

function hidePlanBar() {
  planBar.classList.remove('show');
}

function showSkillDraftBar() {
  skillDraftBar.classList.add('show');
}

function hideSkillDraftBar() {
  skillDraftBar.classList.remove('show');
}

function handleStreamMessage(msg) {
  if (msg.type === 'event') {
    renderEvents([msg.event]);
  } else if (msg.type === 'stats') {
    updateStatus(msg.stats);
  } else if (msg.type === 'error') {
    renderEntry(toolLog, 'system', '錯誤', msg.error);
  }
}

// 後端以 NDJSON（一行一個 JSON）串流回傳：每完成一次推論或工具執行就推一行，
// 這裡邊收邊解析、立刻渲染，不用等整回合結束。最後一行 type === 'done'
// 帶有 awaiting_decision / awaiting_plan / pending_mode / stats 等收尾資訊。
async function streamPost(url, body) {
  const res = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {})
  });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let done = null;
  const consume = (line) => {
    line = line.trim();
    if (!line) return;
    let msg;
    try { msg = JSON.parse(line); } catch (e) { return; }
    if (msg.type === 'done') done = msg; else handleStreamMessage(msg);
  };
  while (true) {
    const { value, done: finished } = await reader.read();
    if (finished) break;
    buffer += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buffer.indexOf('\n')) >= 0) {
      consume(buffer.slice(0, idx));
      buffer = buffer.slice(idx + 1);
    }
  }
  buffer += decoder.decode();
  consume(buffer);
  return done || {};
}

function applyDone(data) {
  if (data.stats) updateStatus(data.stats);
  if (data.awaiting_decision) showDecisionBar(data.pending_mode);
  if (data.awaiting_plan) showPlanBar();
  if (data.awaiting_skill_draft) showSkillDraftBar();
}

async function sendMessage() {
  const text = msgBox.value.trim();
  const hasPending = skillStrip.querySelectorAll('.skill-chip').length > 0 || attachStrip.querySelectorAll('.attach-item').length > 0;
  if (!text && !hasPending) return;   // 只有 chip／附件沒有文字時，伺服器會用預設提示詞
  hideSlashMenu();
  setBusy(true);
  hideDecisionBar();
  hidePlanBar();
  hideSkillDraftBar();
  msgBox.value = '';
  try {
    applyDone(await streamPost('/api/send', { message: text }));
  } catch (e) {
    renderEntry(toolLog, 'system', '錯誤', '與伺服器的連線中斷：' + e);
  } finally {
    setBusy(false);
    msgBox.focus();
  }
}

async function sendDecision(action) {
  setBusy(true);
  hideDecisionBar();
  try {
    applyDone(await streamPost('/api/decision', { action }));
  } catch (e) {
    renderEntry(toolLog, 'system', '錯誤', '與伺服器的連線中斷：' + e);
  } finally {
    setBusy(false);
    msgBox.focus();
  }
}

async function sendPlanDecision(action) {
  setBusy(true);
  hidePlanBar();
  try {
    applyDone(await streamPost('/api/send', { message: action }));
  } catch (e) {
    renderEntry(toolLog, 'system', '錯誤', '與伺服器的連線中斷：' + e);
  } finally {
    setBusy(false);
    msgBox.focus();
  }
}

// 🧩 技能草稿的決定跟計畫一樣走 /api/send：有草稿待決定時，輸入框的內容一律視為對草稿的回應
async function sendSkillDraftDecision(action) {
  setBusy(true);
  hideSkillDraftBar();
  try {
    applyDone(await streamPost('/api/send', { message: action }));
  } catch (e) {
    renderEntry(toolLog, 'system', '錯誤', '與伺服器的連線中斷：' + e);
  } finally {
    setBusy(false);
    msgBox.focus();
  }
}

// ===== 📷 影像附件：框選畫面／選擇檔案。附件本體存在伺服器端的 VisionSession，
// 這裡只顯示縮圖並記住 id 以便移除；送出新任務時伺服器一次消費全部附件。 =====
function toggleAttachMenu() { attachMenu.classList.toggle('show'); }
document.addEventListener('click', (e) => {
  if (!attachMenu.contains(e.target) && e.target !== attachBtn) attachMenu.classList.remove('show');
});

function syncAttachStrip() {
  const n = attachStrip.querySelectorAll('.attach-item').length;
  attachStrip.classList.toggle('show', n > 0);
  attachBtn.textContent = n > 0 ? `📷 ${n}` : '📷';
}
function clearAttachStrip() {
  attachStrip.querySelectorAll('.attach-item').forEach(el => el.remove());
  syncAttachStrip();
}
function addAttachment(item) {
  const wrap = document.createElement('div');
  wrap.className = 'attach-item'; wrap.dataset.id = item.id;
  const img = document.createElement('img');
  img.src = item.thumbnail; img.title = `${item.width}x${item.height}`;
  const rm = document.createElement('div');
  rm.className = 'rm'; rm.textContent = '✕'; rm.title = '移除';
  rm.onclick = async () => {
    await VisionSnip.postJSON('/api/vision/remove', { id: item.id });
    wrap.remove();
    syncAttachStrip();
  };
  wrap.appendChild(img); wrap.appendChild(rm);
  attachStrip.appendChild(wrap);
  syncAttachStrip();
}
function attachFromScreen() { attachMenu.classList.remove('show'); VisionSnip.capture(); }
function attachFromFile() { attachMenu.classList.remove('show'); fileInput.value = ''; fileInput.click(); }
fileInput.addEventListener('change', async () => {
  for (const file of Array.from(fileInput.files || [])) {
    try {
      const dataUrl = await new Promise((resolve, reject) => {
        const r = new FileReader(); r.onload = () => resolve(r.result); r.onerror = () => reject(r.error); r.readAsDataURL(file);
      });
      const data = await VisionSnip.postJSON('/api/vision/upload', { name: file.name, data: dataUrl });
      if (data.error) { renderEntry(toolLog, 'system', '錯誤', `附加 ${file.name} 失敗：${data.error}`); continue; }
      addAttachment(data);
    } catch (e) {
      renderEntry(toolLog, 'system', '錯誤', `讀取 ${file.name} 失敗：${e}`);
    }
  }
});
document.body.insertAdjacentHTML('beforeend', VisionSnip.markup());
VisionSnip.init({
  apiBase: '/api/vision',
  onAdded: addAttachment,
  onStatus: (t) => { document.getElementById('stat-attach').textContent = '📷 ' + t; },
  onError: (t) => renderEntry(toolLog, 'system', '錯誤', t),
});

// ===== 「/」選單：功能開關／指令 + 技能（選技能 = 手動按需載入規格，隨下一則訊息送出）=====
const slashMenu = document.getElementById('slash-menu');
const skillStrip = document.getElementById('skill-strip');
let catalog = { commands: [], skills: [] };
let slashItems = [];   // 目前顯示的可選項目（扁平，供 ↑↓ 移動）
let slashActive = -1;
fetch('/api/commands').then(r => r.json()).then(d => { catalog = d; }).catch(() => {});

function slashQuery() {
  const v = msgBox.value;
  if (!v.startsWith('/') || v.includes('\n')) return null;
  return v.slice(1).toLowerCase();
}
function updateSlashMenu() {
  const q = slashQuery();
  if (q === null) { hideSlashMenu(); return; }
  const cmds = catalog.commands.filter(c => c.cmd.toLowerCase().includes(q) || (c.desc || '').toLowerCase().includes(q));
  const skills = catalog.skills.filter(s => s.name.toLowerCase().includes(q) || (s.description || '').toLowerCase().includes(q) || (s.category || '').toLowerCase().includes(q));
  slashItems = []; slashMenu.innerHTML = '';
  const addSection = (title) => { const h = document.createElement('div'); h.className = 'slash-section'; h.textContent = title; slashMenu.appendChild(h); };
  const addGroup = (title) => { const g = document.createElement('div'); g.className = 'slash-group'; g.textContent = title; slashMenu.appendChild(g); };
  const addItem = (item, nameText, descText, badge) => {
    const el = document.createElement('div'); el.className = 'slash-item'; el.dataset.index = slashItems.length;
    const n = document.createElement('span'); n.className = 'name'; n.textContent = nameText; el.appendChild(n);
    if (badge) { const b = document.createElement('span'); b.className = 'badge'; b.textContent = badge; el.appendChild(b); }
    const d = document.createElement('span'); d.className = 'desc'; d.textContent = descText || ''; el.appendChild(d);
    el.onmousedown = (e) => { e.preventDefault(); selectSlashItem(item); };  // mousedown：避免 textarea 先失焦
    el.onmouseenter = () => setSlashActive(Number(el.dataset.index));
    slashMenu.appendChild(el); slashItems.push({ item, el });
  };
  if (cmds.length) {
    addSection('⚙️ 功能開關與指令');
    let last = null;
    cmds.forEach(c => { if (c.group !== last) { addGroup(c.group); last = c.group; } addItem({ type: 'command', ...c }, c.cmd, c.desc); });
  }
  if (skills.length) {
    addSection('📘 技能（選取 = 手動載入規格，隨下一則訊息送出）');
    let last = null;
    skills.forEach(s => {
      if (s.category !== last) { addGroup(s.category); last = s.category; }
      addItem({ type: 'skill', ...s }, '/skill ' + s.name, s.description, s.has_memory ? '含經驗記憶' : null);
    });
  }
  if (!slashItems.length) { const e = document.createElement('div'); e.className = 'slash-empty'; e.textContent = '沒有符合的指令或技能'; slashMenu.appendChild(e); }
  slashMenu.classList.add('show');
  setSlashActive(slashItems.length ? 0 : -1);
}
function setSlashActive(i) {
  slashActive = i;
  slashItems.forEach((it, k) => it.el.classList.toggle('active', k === i));
  if (i >= 0) slashItems[i].el.scrollIntoView({ block: 'nearest' });
}
function hideSlashMenu() { slashMenu.classList.remove('show'); slashMenu.innerHTML = ''; slashItems = []; slashActive = -1; }
function slashMenuOpen() { return slashMenu.classList.contains('show'); }
async function selectSlashItem(item) {
  hideSlashMenu();
  if (item.type === 'command') {
    // 只填入、不送出：/clear 這類指令誤點會清掉整段對話，按 Enter 才送
    msgBox.value = item.cmd + (item.args ? '' : '');
    msgBox.focus();
    msgBox.setSelectionRange(msgBox.value.length, msgBox.value.length);
    return;
  }
  msgBox.value = '';
  await loadSkill(item.name);
  msgBox.focus();
}
async function loadSkill(name) {
  try {
    const data = await VisionSnip.postJSON('/api/skill/load', { name });
    if (data.error) { renderEntry(toolLog, 'system', '錯誤', data.error); return; }
    if (data.already) { renderEntry(toolLog, 'system', '系統', `ℹ️ 技能 ${name} 的規格已在待送清單。`); return; }
    renderEntry(toolLog, 'skillload', `📘 手動載入技能規格：${data.name}（≈${data.tokens} tokens，隨下一則訊息送出）`, data.doc);
    syncSkillStrip(data.pending);
  } catch (e) {
    renderEntry(toolLog, 'system', '錯誤', '載入技能規格失敗：' + e);
  }
}
function syncSkillStrip(names) {
  skillStrip.querySelectorAll('.skill-chip').forEach(el => el.remove());
  (names || []).forEach(name => {
    const chip = document.createElement('span'); chip.className = 'skill-chip'; chip.dataset.name = name;
    const t = document.createElement('span'); t.textContent = '📘 ' + name; chip.appendChild(t);
    const rm = document.createElement('span'); rm.className = 'rm'; rm.textContent = '✕'; rm.title = '移除（不會隨下一則訊息送出）';
    rm.onclick = async () => { const d = await VisionSnip.postJSON('/api/skill/remove', { name }); syncSkillStrip(d.pending); };
    chip.appendChild(rm); skillStrip.appendChild(chip);
  });
  skillStrip.classList.toggle('show', (names || []).length > 0);
}
msgBox.addEventListener('input', updateSlashMenu);
document.addEventListener('click', (e) => {
  if (!slashMenu.contains(e.target) && e.target !== msgBox) hideSlashMenu();
});

msgBox.addEventListener('keydown', (e) => {
  if (slashMenuOpen()) {
    if (e.key === 'Escape') { e.preventDefault(); hideSlashMenu(); return; }
    if (slashItems.length) {
      if (e.key === 'ArrowDown') { e.preventDefault(); setSlashActive((slashActive + 1) % slashItems.length); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); setSlashActive((slashActive - 1 + slashItems.length) % slashItems.length); return; }
      if (e.key === 'Tab') { e.preventDefault(); if (slashActive >= 0) selectSlashItem(slashItems[slashActive].item); return; }
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        const active = slashActive >= 0 ? slashItems[slashActive].item : null;
        // 已經把指令完整打出來（例如 /auto on）就直接送出，不必再選一次
        if (active && active.type === 'command' && msgBox.value.trim() === active.cmd.trim()) { hideSlashMenu(); sendMessage(); return; }
        if (active) selectSlashItem(active);
        return;
      }
    }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

// 初始載入時抓一次目前狀態
fetch('/api/status').then(r => r.json()).then(updateStatus);
</script>

</body>
</html>
"""


# =========================================================
# HTTP Server（純標準庫，無外部依賴）
# =========================================================

class EventStream:
    """取代原本累積用的 events list：介面同樣只有 append()，但每 append
    一筆就立刻以 NDJSON（一行一個 JSON）寫回 HTTP 回應並 flush，讓瀏覽器
    在每次推論／工具執行完成的當下就看到結果，而不是等整回合結束。

    每筆事件後面會順帶推一筆最新 stats，讓 header 的 token 計數也即時更新。

    若使用者中途關掉分頁導致寫入失敗，不往外拋例外，只標記 client_gone
    並靜默略過之後的寫入——讓 run_turn 等流程照原本的方式跑完，Agent 的
    狀態（messages、pending、token 計數）才不會停在半途，跟改成串流前
    「請求一送出，後端一定跑完整回合」的行為一致。
    """

    def __init__(self, handler):
        self._handler = handler
        self.client_gone = False

    def _write(self, payload):
        if self.client_gone:
            return
        try:
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            self._handler.wfile.write(line.encode("utf-8"))
            self._handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.client_gone = True

    def append(self, event):
        self._write({"type": "event", "event": event})
        self._write({"type": "stats", "stats": build_stats()})

    def error(self, text):
        self._write({"type": "error", "error": text})

    def done(self, **fields):
        self._write({"type": "done", "stats": build_stats(), **fields})


class ConsoleHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 安靜一點，避免洗版終端機

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw or b"{}")

    def _begin_stream(self):
        """送出串流回應的 header 並回傳 EventStream。不帶 Content-Length，
        以 HTTP/1.0 的「連線關閉」作為回應結尾，瀏覽器的 fetch 會邊收邊給。"""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        return EventStream(self)

    def _finish(self, stream, awaiting_decision):
        """推出最後一行 done：帶前端收尾要用的旗標與最新 stats。"""
        stream.done(
            awaiting_decision=awaiting_decision,
            awaiting_plan=plan_pending["active"],
            awaiting_skill_draft=agent.pending_skill_draft is not None,
            pending_mode=pending["mode"] if awaiting_decision else None,
        )

    def _finish_turn(self, stream, awaiting_decision):
        """回合真正結束（不在等決策、也沒有計畫待核准）時的收尾：先做軟水位檢查再送 done。
        AI 的最終回覆事件早已串流到前端，這裡的壓縮不影響使用者看到答案的時間；
        /parallel_cal on 時壓縮在背景執行緒進行，done 會立刻送出。"""
        if not awaiting_decision and not plan_pending["active"]:
            after_turn_compression(
                agent, state["parallel_cal"],
                lambda text: stream.append({"channel": "system", "text": text}),
            )
        self._finish(stream, awaiting_decision)

    def do_GET(self):
        if self.path == "/":
            body = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            self._send_json(build_stats())
            return
        if self.path == "/api/commands":
            # 「/」選單的內容：功能開關／指令 + SKILLS.md 技能清單（含分類、是否有經驗記憶）
            self._send_json({"commands": SLASH_COMMANDS, "skills": agent.list_skills()})
            return
        if self.path.startswith("/api/results/"):
            # 📄 工具結果存檔原文：/api/results/<編號|latest|index|檔名>（純文字；讓使用者點卡片上的編號就能看整份原文）
            from urllib.parse import unquote
            path = agent.result_file_path(unquote(self.path[len("/api/results/"):]))
            if not path:
                self.send_error(404, "no such tool result")
                return
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        static = vision_static_file(self.path)
        if static:
            body, content_type = static
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        if self.path.startswith("/api/vision/"):
            self._handle_vision(self.path[len("/api/vision/"):])
            return
        if self.path.startswith("/api/skill/"):
            self._handle_skill(self.path[len("/api/skill/"):])
            return
        if self.path not in ("/api/send", "/api/decision"):
            self.send_error(404)
            return
        data = self._read_json()
        with lock:
            stream = self._begin_stream()
            try:
                if self.path == "/api/send":
                    self._handle_send(data, stream)
                else:
                    self._handle_decision(data, stream)
            except Exception as e:
                # 串流 header 已送出，無法再改 HTTP 狀態碼，改以 error 訊息
                # 告知前端；同時把 traceback 印在伺服器端方便除錯。
                traceback.print_exc()
                stream.error(f"伺服器處理時發生錯誤：{e}")
                self._finish(stream, bool(pending["mode"]))

    def _handle_skill(self, action):
        """📘 手動載入技能規格的 JSON 端點（不串流、不進 agent 的 lock，狀態在 pending_skills 自己的 lock）。"""
        data = self._read_json()
        try:
            if action == "load":
                self._send_json(load_pending_skill(data.get("name")))
            elif action == "remove":
                self._send_json({"pending": remove_pending_skill(data.get("name"))})
            elif action == "clear":
                take_all_pending_skills()
                self._send_json({"pending": []})
            elif action == "status":
                self._send_json({"pending": pending_skill_names()})
            else:
                self.send_error(404)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": f"伺服器處理技能規格時發生錯誤：{e}"}, status=500)

    def _handle_vision(self, action):
        """📷 影像附件的一般 JSON 端點（不串流）。附件狀態在 vision_session 自己的 lock 裡，
        不進 agent 的 lock：擷取畫面（PowerShell）可能要幾秒，不該卡住其他請求。"""
        data = self._read_json()
        try:
            if action == "screens":
                payload = vision_session.screens()
            elif action == "capture":
                payload = vision_session.capture(data.get("screen_index"))
            elif action == "crop":
                payload = vision_session.crop(
                    data.get("x1"), data.get("y1"), data.get("x2"), data.get("y2"),
                    data.get("preview_width"), data.get("preview_height"),
                )
            elif action == "upload":
                payload = vision_session.add_data_url(data.get("data"), data.get("name") or "上傳的影像")
            elif action == "remove":
                payload = vision_session.remove(data.get("id"))
            elif action == "clear":
                payload = vision_session.clear()
            elif action == "status":
                payload = vision_session.status()
            else:
                self.send_error(404)
                return
        except VisionError as e:
            self._send_json({"error": str(e)}, status=400)
            return
        except Exception as e:
            traceback.print_exc()
            self._send_json({"error": f"伺服器處理影像時發生錯誤：{e}"}, status=500)
            return
        self._send_json(payload)

    def _handle_send(self, data, stream):
        message = (data.get("message") or "").strip()

        if pending["mode"]:
            stream.error("尚有待決策的工具結果，請先回應決策再繼續。")
            self._finish(stream, True)
            return

        # 有計畫待核准時，輸入框的內容一律視為對計畫的回應（y／n／修改意見），
        # 不當作新指令或 slash command 處理——跟 CLI 版 _run_plan_flow 的
        # input() 迴圈行為一致。
        if plan_pending["active"]:
            outcome = handle_plan_response(message, stream)
            awaiting_decision = False
            if outcome == "approved":
                awaiting_decision = run_turn(stream)
            if outcome == "revised":
                self._finish(stream, False)  # 仍在規劃中，不算回合結束
            else:
                self._finish_turn(stream, awaiting_decision)  # 核准後跑完、或取消，都是回合結束
            return

        # 有技能草稿待決定時，輸入框的內容一律視為對草稿的回應（y／t／n／修改意見），
        # 與計畫核准同一種處理方式；這裡不呼叫模型的主對話，也不消耗附件與待送技能規格。
        if agent.pending_skill_draft:
            handle_skill_draft_response(message, stream)
            self._finish(stream, False)
            return

        if not message:
            if vision_session.count() == 0 and not pending_skill_names():
                self._finish(stream, False)
                return
            # 只附圖／只載入技能而不打字：用預設提示詞
            message = DEFAULT_VISION_PROMPT if vision_session.count() else DEFAULT_SKILL_PROMPT

        if handle_slash_command(message, stream):
            self._finish(stream, False)  # slash 指令不消耗附件
            return

        # 📷📘 只有「新任務」才消費附件與手動載入的技能規格（slash 指令與計畫核准／修改意見不會）
        attachments = vision_session.take_all()
        skill_items = take_all_pending_skills()

        user_tokens = agent.count_tokens(message)
        agent.total_user_tokens += user_tokens
        stream.append({
            "channel": "chat", "role": "user", "text": message,
            "tokens": user_tokens, "attachments": len(attachments),
            "skills": [name for name, _ in skill_items],
        })

        # 新任務開始：上一個已核准的計畫到此結束（見 clear_plan_for_new_task）
        clear_plan_for_new_task(stream)

        # 記錄這一輪任務最原始的使用者敘述，跟 CLI 版（Agent_Runner.main）
        # 行為一致，供獨立摘要 session 在沒有 objective／plan 可用時，
        # 當作「原始問題」聚焦摘要內容（見 SkillAgent._build_task_anchor_text）
        agent.set_current_task(message)

        # 有附加影像：先跑獨立視覺 sub-session，把分析結果以文字併入這次的使用者訊息，
        # 之後不論是 plan 模式還是直接執行，主 Agent 拿到的都是「原文 + 影像分析」的純文字
        content = _run_vision_subsession(message, attachments, stream) if attachments else message

        # 📘 手動載入的技能規格附在這則訊息後面（AI 可直接依規格裡的腳本路徑執行，省掉一輪「先載規格」）
        if skill_items:
            blocks = [block for _, block in skill_items]
            skill_tokens = sum(agent.count_tokens(b) for b in blocks)
            agent.total_tool_tokens += skill_tokens
            content = attach_skill_docs(content, blocks)
            stream.append({"channel": "system", "text": (
                f"📘 隨訊息載入技能規格：{', '.join(name for name, _ in skill_items)}（≈{skill_tokens} tokens）"
            )})

        if state["plan_mode"]:
            start_plan_flow(content, stream)
            self._finish(stream, False)
            return

        agent.messages.append({'role': 'user', 'content': content})
        self._finish_turn(stream, run_turn(stream))

    def _handle_decision(self, data, stream):
        action = (data.get("action") or "").strip().lower()

        if not pending["mode"]:
            stream.error("目前沒有待決策的工具結果。")
            self._finish(stream, False)
            return

        should_continue = apply_decision(action, stream)
        awaiting = run_turn(stream) if should_continue else False
        self._finish_turn(stream, awaiting)


def main():
    host = os.environ.get("WEB_CONSOLE_HOST", "127.0.0.1")
    port = int(os.environ.get("WEB_CONSOLE_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), ConsoleHandler)

    print("\n" + "=" * 50)
    print(f"🌐 SkillAgent Web Console 已啟動: http://{host}:{port}")
    print("按 Ctrl+C 停止伺服器。")
    print("=" * 50)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Bye")
        server.shutdown()


if __name__ == "__main__":
    main()
