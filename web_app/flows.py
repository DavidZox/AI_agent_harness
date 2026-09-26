"""回合流程：run_turn（ask_ai → run_tool 迴圈）、工具結果的決策、Plan 模式、技能草稿、附圖的視覺 sub-session、狀態列。"""
from vision import (
    analyze as vision_analyze,
    DEFAULT_MODEL as VISION_MODEL,
    SUBSESSION_SYSTEM_PROMPT,
    VisionError,
)
from agent_core.config import (
    KEEP_RECENT_TOKENS,
    NUM_CTX,
    SOFT_TOKEN_THRESHOLD,
    TOKEN_THRESHOLD,
    TOOL_RESULT_TOKEN_THRESHOLD,
)
from agent_core.protocol import action_text, is_exempt_result, tool_result_message
from agent_core.turn import _append_discarded_tool_result, _content_for_context, context_kind
from .state import agent, pending, pending_skill_names, plan_pending, state, vision_session


MAX_AUTO_ITERATIONS = 25  # 安全防護：避免 auto 模式下模型無限迴圈卡住伺服器


# =========================================================
# 核心流程：重用 agent_core.SkillAgent 的方法，改寫成
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
