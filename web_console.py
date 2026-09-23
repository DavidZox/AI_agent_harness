"""
Web Console for SkillAgent

一個純標準庫（不需要額外安裝 flask / fastapi）的網頁版操作介面，
取代在終端機裡跑 Agent_Runner.py 的方式。

設計原則：
- 完全不修改 Agent_Runner.py，只 import 其中的 SkillAgent 類別與
  _append_discarded_tool_result 這個共用函式，重用既有邏輯。
- 畫面拆成兩塊：
    左邊「使用者 ↔ Agent 對話」：使用者輸入與 AI 的文字回應。
    右邊「系統 / 工具回傳」：EXECUTE 指令觸發的規格書載入或腳本執行結果。
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
    SkillAgent,
    _append_discarded_tool_result,
    _content_for_context,
    after_turn_compression,
    NUM_CTX,
    TOKEN_THRESHOLD,
    SOFT_TOKEN_THRESHOLD,
    KEEP_RECENT_TOKENS,
    TOOL_RESULT_TOKEN_THRESHOLD,
    PARALLEL_CAL_DEFAULT,
    is_skill_doc_result,
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
    "tool_summary_mode": False,
    "plan_mode": False,
    "parallel_cal": PARALLEL_CAL_DEFAULT,  # 軟水位壓縮改在背景執行緒做（/parallel_cal on|off）
}
pending = {"result": None, "mode": None, "tokens": None}  # 等待使用者決策的工具結果（hybrid / manual 模式用）
plan_pending = {"active": False, "text": None}  # 等待使用者核准／修改意見的任務計畫（/plan 模式用）
vision_session = VisionSession()  # 📷 尚未送出的影像附件（框選截圖／上傳的檔案），送出新任務時一次消費
lock = threading.Lock()

MAX_AUTO_ITERATIONS = 25  # 安全防護：避免 auto 模式下模型無限迴圈卡住伺服器

MENU_TEXT = """可用指令：
/menu                  顯示本說明
/clear                  清空對話記憶，重新開始
/compress               手動壓縮並歸檔目前的歷史對話
/auto on / /auto off    切換 Auto Continue 模式（工具結果自動帶入下一輪，不需確認）
/hybrid on / /hybrid off 切換 Hybrid 模式（每次工具結果都詢問是否加入上下文）
/summarize on / /summarize off 切換工具回傳摘要模式（見下方說明，預設關閉）
/parallel_cal on / /parallel_cal off 切換平行壓縮（回合結束後的軟水位壓縮改在背景執行緒做，預設關閉）
/plan on / /plan off    切換 Plan 模式（新任務會先規劃步驟，經你核准後才會執行，預設關閉）
/plan done              手動清除目前已核准、正在執行中的計畫（見下方說明）
/objective set <內容>   設定 Sticky Objective（最高優先任務，會持續提醒 AI）
/objective show         查看目前的 Objective
/objective clear        清除 Objective

不切換 auto／hybrid 時，預設為「手動模式」：每次工具執行完都會等待你確認
是否要把結果加入上下文，畫面下方會出現決策按鈕。

開啟 /plan on 後，輸入新任務時 AI 不會馬上執行，而是先依 SKILLS.md 規劃出
步驟清單顯示出來，畫面下方會出現「✅ 核准並執行 / 🚫 取消任務」按鈕；也可以
直接在輸入框打字送出修改意見，AI 會依意見重新規劃，直到你核准或取消為止。
規劃階段完全不會呼叫任何工具，即使 AI 不小心在計畫裡夾帶了 EXECUTE 指令
也不會被執行——確認關卡是靠系統不執行工具保證的，不是單純提醒 AI 而已。

計畫一旦核准，會存進系統提示詞（跟 Sticky Objective 同一種做法），確保
即使後續執行很多輪工具、甚至觸發了自動壓縮，AI 都不會忘記這個計畫。
系統不會自動判斷「所有步驟都做完了」而清掉它，需要你在任務結束後手動
輸入 /plan done 清除，避免舊計畫殘留干擾之後的新任務。

單一工具回傳若超過 {threshold} tokens，不論目前是什麼模式，預設 AI 只會收到
精簡的成功／失敗摘要（避免大量原始輸出干擾推理）。開啟 /summarize on 後，
超過門檻的結果會改由一個獨立、乾淨的 session 做語意摘要（不會混進主對話
的上下文），主 session 拿到的會是摘要後的重點而不只是成功/失敗；若摘要
session 失敗會自動退回原本的成功/失敗摘要，不影響主流程。這個獨立 session
會拿到「使用者原始問題敘述」當聚焦依據（優先用 Objective，其次是目前核准
中的 plan 執行到哪一步，都沒有就用這一輪任務原始輸入的文字），避免摘要
時因為不知道重點是什麼而漏掉關鍵資訊。完整原始內容永遠都會顯示在
「系統 / 工具回傳」面板並標記 ⚠️ 待確認，需自行點「✅ 我已確認」。

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

上下文壓縮採雙水位線：軟水位 {soft_threshold} tokens（num_ctx {num_ctx} 的 50%）只在
「回合結束後」檢查，此時答案已經送到你眼前、模型閒著，順手壓縮不會拉長任何一次回覆；
硬水位 {token_threshold}（70%）是呼叫模型前的最後防線，超過一定同步壓縮。壓縮時保留
最新約 {keep_recent} tokens 的原文（在訊息邊界切、不拆開指令與其結果），其餘與上一份
摘要融合成新的一份結構化摘要（總體情境／關鍵進度／執行結果與錯誤／使用者偏好／未完成
事項），存到 logs/ 並注入系統提示詞；系統提示詞只帶最新一份，不會越滾越長。
開啟 /parallel_cal on 後，軟水位壓縮改在背景執行緒進行，你可以馬上繼續對話，完成時右欄
會出現通知。但要注意：只有 Ollama 真的為模型配置多個 slot、或摘要模型（AGENT_SUMMARY_MODEL）
與主模型不同時，摘要才會與主對話同時推論；目前版本的 Ollama 對多模態模型（gemma4）強制
單 slot，同模型的背景摘要會讓你的下一次對話在 Ollama 內排隊，等待只是搬到下一次呼叫。
算力弱的設備建議維持關閉（序列處理）。""".format(
    threshold=TOOL_RESULT_TOKEN_THRESHOLD, vision_model=VISION_MODEL,
    token_threshold=TOKEN_THRESHOLD, soft_threshold=SOFT_TOKEN_THRESHOLD,
    keep_recent=KEEP_RECENT_TOKENS, num_ctx=NUM_CTX,
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
        "vision_model": VISION_MODEL,
        "summary_model": agent.summary_model,
        "parallel_cal": state["parallel_cal"],
        "compressing": agent.compression_in_progress(),
        "last_compression": agent.last_compression,
        # 背景壓縮完成／失敗的通知：每次組 stats 時取走（stats 會隨每個事件與 /api/status 送到前端，
        # 前端把它們渲染成系統訊息），同一則不會重複出現
        "notices": agent.pop_notices(),
    }


# summarize_tool_result() 產生的內容固定以這個標籤開頭，用來判斷
# _content_for_context() 這次回傳的是不是「AI 摘要」版本。
_SUMMARY_TAG = "[tool result - AI 摘要]"


def _emit_summary_event(content, events):
    """若這次餵給 AI 的內容是獨立摘要 session 產生的，額外推一個事件到
    前端的「系統 / 工具回傳」面板，讓使用者也能看到摘要結果，
    而不是只能從主對話推測 AI 收到了什麼。"""
    if content.startswith(_SUMMARY_TAG):
        events.append({"channel": "summary", "text": content})


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
    return (
        f"{message}\n\n"
        f"[vision result]\n"
        f"（使用者附上了 {n} 張影像；以下是獨立視覺模型針對上述訊息對影像的分析結果。"
        f"你看不到原圖，請以這份分析為依據回應或決定下一步。）\n{result}"
    )


def _ask_and_present_plan(events):
    """呼叫一次 ask_ai() 取得計畫文字，推到 events 給前端顯示，並把
    plan_pending 標記為待核准。跟 CLI 的 _run_plan_flow 用同一套
    SkillAgent.build_plan_request / build_plan_revision_request，
    只是這裡拆成「單次 HTTP 請求處理一小段」的非同步形式。"""
    plan_msg = agent.ask_ai()
    agent.total_ai_tokens += agent.last_ai_tokens(plan_msg)
    if agent.auto_compressed:
        events.append({"channel": "system", "text": "📦 上下文超過門檻，呼叫前已自動壓縮並歸檔。"})
    agent.messages.append({'role': 'assistant', 'content': plan_msg})
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
        agent.current_plan = plan_pending["text"]
        agent.messages.append({
            'role': 'user',
            'content': "[PLAN_CONFIRMED]\n使用者已核准上述計畫，現在開始依計畫執行第一個步驟。"
        })
        plan_pending["active"] = False
        plan_pending["text"] = None
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
        events.append({"channel": "chat", "role": "assistant", "text": ai_msg, "tokens": ai_tokens})

        result = agent.run_tool(ai_msg)
        tool_tokens = 0
        if result:
            tool_tokens = agent.count_tokens(result)
            agent.total_tool_tokens += tool_tokens
            # 規格文件載入不受門檻限制（_content_for_context 會完整放行），不標 ⚠️
            oversized = tool_tokens > TOOL_RESULT_TOKEN_THRESHOLD and not is_skill_doc_result(result)
            events.append({
                "channel": "tool",
                "text": result,
                "tokens": tool_tokens,
                "oversized": oversized,
            })
            if oversized:
                events.append({
                    "channel": "system",
                    "text": (
                        f"⚠️ 此工具回傳約 {tool_tokens} tokens，超過門檻 "
                        f"{TOOL_RESULT_TOKEN_THRESHOLD}，已標記待人工確認；"
                        f"AI 只會收到精簡的成功／失敗摘要。"
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
            _emit_summary_event(content, events)
            agent.messages.append({'role': 'user', 'content': f"[tool result]\n{content}"})
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
            _emit_summary_event(content, events)
            agent.messages.append({'role': 'user', 'content': f"[tool result]\n{content}"})
        else:
            _append_discarded_tool_result(agent)
        return True  # hybrid 不論加入或捨棄，都會讓 AI 接續推論

    # manual 模式
    if action == "y":
        content = _content_for_context(
            result, tool_tokens, agent=agent, use_summary=state["tool_summary_mode"]
        )
        _emit_summary_event(content, events)
        agent.messages.append({'role': 'user', 'content': f"【系統執行結果】:\n{content}"})
        return True
    if action == "stop":
        return False
    _append_discarded_tool_result(agent)
    return False


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
        events.append({"channel": "system", "text": "🧠 已開啟工具回傳摘要模式（超過門檻的結果會由獨立 session 摘要）"})
        return True
    if lower == "/summarize off":
        state["tool_summary_mode"] = False
        events.append({"channel": "system", "text": "🧠 已關閉工具回傳摘要模式（改回精簡成功/失敗判定）"})
        return True
    if lower == "/plan on":
        state["plan_mode"] = True
        events.append({"channel": "system", "text": "📝 已開啟 Plan 模式（新任務會先規劃步驟，經你核准後才會執行）"})
        return True
    if lower == "/plan off":
        state["plan_mode"] = False
        events.append({"channel": "system", "text": "📝 已關閉 Plan 模式（恢復直接執行）"})
        return True
    if lower == "/plan done":
        if agent.current_plan:
            agent.current_plan = None
            events.append({"channel": "system", "text": "✅ 已清除目前進行中的計畫（system prompt 不再提醒 AI 依計畫執行）"})
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
  .entry.summary { background: #241f33; border: 1px solid #5a4a8f; color: #cfc3f0; }
  .entry.summary .tag { color: #b39ddb; opacity: 1; }
  .entry.plan { background: #16302c; border: 1px solid #2f6f5e; }
  .entry.plan .tag { color: #4fd8ba; opacity: 1; }
  .entry.tool.oversized { border: 1px solid #b0873f; box-shadow: 0 0 0 1px #b0873f inset; }
  .entry.tool.oversized .tag { color: #e6b95c; opacity: 1; }
  .entry.tool.oversized.reviewed { border-color: #2f4a3a; box-shadow: none; opacity: 0.75; }
  .entry .tag { font-size: 10px; text-transform: uppercase; opacity: 0.6; margin-bottom: 4px; }
  .entry .ack-btn {
    display: inline-block; margin-top: 6px; padding: 4px 10px; font-size: 11px;
    background: #4a4c50; border-radius: 4px; cursor: pointer;
  }
  footer { border-top: 1px solid #3a3c40; padding: 10px 12px; background: #2b2d31; }
  #decision-bar, #plan-bar { display: none; margin-bottom: 8px; gap: 8px; align-items: center; font-size: 13px; flex-wrap: wrap; }
  #decision-bar.show, #plan-bar.show { display: flex; }
  #decision-bar button, #plan-bar button { cursor: pointer; }
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
</style>
</head>
<body>

<header>
  <h1>🤖 SkillAgent Web Console</h1>
  <div id="status">
    <span id="stat-mode">mode: manual</span>
    <span id="stat-cwd">cwd: -</span>
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
  <div id="attach-strip"><span id="attach-hint">📎 已附加影像（送出時一起分析）：</span></div>
  <div class="input-row">
    <div id="attach-menu">
      <button onclick="attachFromScreen()">🖥️ 框選畫面</button>
      <button onclick="attachFromFile()">📁 選擇檔案</button>
    </div>
    <button class="secondary" id="attach-btn" title="附加影像：框選畫面或選擇檔案" onclick="toggleAttachMenu()">📷</button>
    <textarea id="msg" placeholder="輸入訊息，或用 /menu 查詢可用指令...（Enter 送出，Shift+Enter 換行；📷 可附加影像）"></textarea>
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

function renderEvents(events) {
  events.forEach(ev => {
    if (ev.channel === 'chat') {
      const note = (ev.role === 'user' && ev.attachments) ? `\n📎 附加了 ${ev.attachments} 張影像` : '';
      renderEntry(chatLog, ev.role, ev.role === 'user' ? '你' : 'AI', ev.text + note);
    } else if (ev.channel === 'vision') {
      renderEntry(toolLog, 'vision', `🖼️ 視覺分析（獨立 session，${ev.count} 張影像）`, ev.text, ev.oversized);
    } else if (ev.channel === 'tool') {
      renderEntry(toolLog, 'tool', '系統回傳', ev.text, ev.oversized);
    } else if (ev.channel === 'summary') {
      renderEntry(toolLog, 'summary', '🧠 AI 摘要（獨立 session）', ev.text);
    } else if (ev.channel === 'plan') {
      renderEntry(chatLog, 'plan', '📝 計畫（待你確認）', ev.text);
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
}

async function sendMessage() {
  const text = msgBox.value.trim();
  if (!text) return;
  setBusy(true);
  hideDecisionBar();
  hidePlanBar();
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

msgBox.addEventListener('keydown', (e) => {
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

        if not message:
            if vision_session.count() == 0:
                self._finish(stream, False)
                return
            message = DEFAULT_VISION_PROMPT  # 只附圖不打字：用預設提示詞

        if handle_slash_command(message, stream):
            self._finish(stream, False)  # slash 指令不消耗附件
            return

        # 📷 只有「新任務」才消費附件（slash 指令與計畫核准／修改意見不會）
        attachments = vision_session.take_all()

        user_tokens = agent.count_tokens(message)
        agent.total_user_tokens += user_tokens
        stream.append({
            "channel": "chat", "role": "user", "text": message,
            "tokens": user_tokens, "attachments": len(attachments),
        })

        # 記錄這一輪任務最原始的使用者敘述，跟 CLI 版（Agent_Runner.main）
        # 行為一致，供獨立摘要 session 在沒有 objective／plan 可用時，
        # 當作「原始問題」聚焦摘要內容（見 SkillAgent._build_task_anchor_text）
        agent.current_task = message

        # 有附加影像：先跑獨立視覺 sub-session，把分析結果以文字併入這次的使用者訊息，
        # 之後不論是 plan 模式還是直接執行，主 Agent 拿到的都是「原文 + 影像分析」的純文字
        content = _run_vision_subsession(message, attachments, stream) if attachments else message

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
