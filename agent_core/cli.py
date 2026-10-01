"""CLI 介面（python3 Agent_Runner.py）：互動迴圈、slash 指令、Plan 模式與 /make_skill 的終端機流程。"""
from .agent import SkillAgent
from .config import (
    NUM_CTX,
    PARALLEL_CAL_DEFAULT,
    SOFT_TOKEN_THRESHOLD,
    TOKEN_THRESHOLD,
    TOOL_RESULT_TOKEN_THRESHOLD,
    TOOL_RESULTS_DIRNAME,
    TOOL_SUMMARY_DEFAULT,
)
from .context_mode import CONTEXT_MODE_DESCRIPTIONS
from .protocol import attach_skill_docs, is_exempt_result, tool_result_message
from .turn import _append_discarded_tool_result, _content_for_context, after_turn_compression, context_kind, oversized_notice


def _run_plan_flow(agent, user_task=None):
    """/plan 模式（草稿與規則在 agent_core/plan.py）：user_task 有值時先讓規劃 session 產生第一版草稿；接著等使用者
    y 核准／n 取消／/plan 指令直接改／其他文字＝用說的改（AI 只提出要改哪幾條，harness 套用）。

    安全設計：規劃階段自始至終不會呼叫 agent.run_tool()，也不碰主對話——確認關卡是靠「這個函式根本不執行工具」保證的。
    回傳 True 代表使用者已核准（任務原文＋[PLAN_CONFIRMED] 已加入主對話），False 代表取消。
    """
    if user_task is not None:
        print("📝 規劃中（一次性的規劃 session，不進主對話）…")
        print(f"\n📝 計畫草稿:\n{'-'*30}\n{agent.plan_start_draft(user_task)}\n{'-'*30}")
    while True:
        outcome, msg = agent.plan_handle_input(input("\n📝 計畫> "))
        print(msg)
        if outcome == "approved":
            return True
        if outcome == "rejected":
            return False


def _context_content_for_cli(agent, result, tool_tokens, use_summary):
    """CLI 用：算出要餵給主對話的內容，並印出「主對話實際收到什麼」：完整原文只印一行提示（原文剛才已完整印過），
    任務導向摘要與成功／失敗判定整段印出（Web Console 以 🧠 卡片顯示同一份內容）。"""
    content = _content_for_context(result, tool_tokens, agent=agent, use_summary=use_summary)
    kind = context_kind(content, tool_tokens)
    if kind == "summary":
        print(f"\n🧠 獨立 session 任務導向摘要（主對話實際收到的內容）:\n{'-'*30}\n{content}\n{'-'*30}")
    elif kind == "truncated":
        print(f"🧠 主對話收到：原文頭尾（≈{agent.count_tokens(content)} tokens；中間省略的部分在存檔 #{agent.last_result_id}，AI 自己決定要不要回查）")
    elif kind == "reduced":
        print(f"\n🧠 主對話實際收到的內容（只有成功／失敗判定）:\n{'-'*30}\n{content}\n{'-'*30}")
    elif kind == "doc":
        print(f"🧠 主對話收到：完整規格文件（≈{tool_tokens} tokens，不受門檻限制）")
    else:
        print(f"🧠 主對話收到：完整原文（≈{tool_tokens} tokens，未縮減）")
    return content

def _run_make_skill_flow(agent, arg_text):
    """CLI 的 /make_skill：讀這段對話草擬流程技能 → 預覽 → y 核准／n 取消／其他文字＝修改意見重擬。
    與 Web 的 handle_skill_draft_response 用同一套 SkillAgent 狀態機。"""
    parts = arg_text.split()
    if not parts:
        print("用法：/make_skill <技能名稱> [範圍：省略＝上一個起點之後的對話；或軌跡編號 3-7、3,5,8；或 all]；"
              "/trajectory 可查編號")
        return
    name, spec = parts[0], (parts[1] if len(parts) > 1 else None)
    print(f"🧩 正在讀這段對話與工具回傳，草擬流程技能 {name}（模型 {agent.skill_model}）…")
    draft, err = agent.start_skill_draft(name, spec)
    if err:
        print(f"⚠️ {err}")
        return
    print(agent.skill_draft_preview(draft))
    while True:
        choice = input("\n核准並註冊(y) / 取消(n) / 直接輸入修改意見: ").strip()
        lower = choice.lower()
        if lower == 'y':
            ok, msg = agent.approve_skill_draft()
            print(msg)
            if ok:
                return
            continue
        if choice == "" or lower in ('n', 'no'):
            print(agent.cancel_skill_draft())
            return
        print("🧩 依修改意見重新草擬…")
        draft, err = agent.revise_skill_draft(choice)
        if err:
            print(f"⚠️ {err}")
            continue
        print(agent.skill_draft_preview(draft))


def main():
    agent = SkillAgent(
        model="gemma4:e4b",
        max_history=None,  # 則數視窗停用，統一以 token 門檻壓縮
    )
    agent.reset_conversation()
    auto_mode = False
    hybrid_mode = False  # 👈 新增狀態
    tool_summary_mode = TOOL_SUMMARY_DEFAULT  # 👈 工具回傳超過門檻時交給獨立 session 做任務導向摘要（預設開；/summarize off 改成只給成功／失敗）
    parallel_cal = PARALLEL_CAL_DEFAULT  # 👈 軟水位壓縮改在背景執行緒做（需 Ollama 有 ≥2 個 parallel slot 才真的平行）
    pending_skill_blocks = []  # 👈 /skill <名稱> 手動載入的技能規格，隨下一則新任務訊息一起送出
    plan_mode = False  # 👈 開啟後，下一個新任務先規劃、經使用者核准後才執行；核准即自動退出

    print("\n" + "="*50)
    print("V6 Robot Agent + Token Tracker 已啟動")
    print(f"上下文模式：{agent.context_mode}（/context_mode harness|claude_code 切換）；"
          f"執行前關卡：{'開' if agent.guard_enabled else '關'}（/guard on|off）")
    print("="*50)
    for notice in agent.pop_notices():   # 例如長期記憶超過載入上限
        print(notice)

    def execute_turn():
        """一個回合：反覆 ask_ai → 執行前確認 → run_tool → 依 auto／hybrid／manual 決定結果怎麼加入，直到模型不再下 action；
        最後做軟水位檢查。模式變數（auto_mode 等）由外層的 slash 指令改，這裡只讀。"""
        while True:
            # --- 🧠 AI 推論 ---
            ai_msg = agent.ask_ai()
            ai_tokens = agent.last_ai_tokens(ai_msg)
            agent.total_ai_tokens += ai_tokens
            if agent.auto_cleared:
                print(f"🧹 上下文超過門檻，呼叫前先把 {agent.auto_cleared['results']} 則舊的工具回傳清成存檔編號"
                      f"（騰出約 {agent.auto_cleared['tokens']} tokens，原文仍在存檔）。")
            if agent.auto_compressed:
                print("📦 上下文超過門檻，呼叫前已自動壓縮並歸檔。")
            parsed = agent.parse_reply(ai_msg)  # 回覆協議：顯示 reply／thought，執行只看 action
            if agent.last_reply_retry:
                print(agent.last_reply_retry)
            print(f"\n🧠 AI:\n{'-'*30}\n{agent.format_reply_for_console(parsed)}\n{'-'*30}")
            print(f"📤 AI Tokens: {ai_tokens}")
            agent.messages.append({'role': 'assistant', 'content': ai_msg})

            # --- 🛡️ 執行前關卡：會改變系統狀態的技能先問使用者（程式擋，不靠模型記得要問）---
            guard = agent.guard_check(parsed)
            if guard:
                print(f"\n{agent.guard_prompt_text(guard)}")
                if input("確定執行？(y/n): ").strip().lower() != 'y':
                    agent.messages.append({'role': 'user', 'content': tool_result_message(agent.guard_denied_text(guard), parsed["action"])})
                    print("🚫 已拒絕，這個動作沒有執行；請告訴 AI 要怎麼調整。")
                    break

            # --- 🧰 TOOL 執行（只看 action 欄位，reply 裡的文字不會被執行）---
            result = agent.run_tool(parsed, approved=bool(guard))
            for notice in agent.pop_notices():   # 計畫換下一步、連續失敗退出等
                print(notice)
            tool_tokens = 0
            if result:
                tool_tokens = agent.count_tokens(result)
                agent.total_tool_tokens += tool_tokens
                print(f"\n🚀 系統回傳:\n{'-'*30}\n{result}\n{'-'*30}")
                if agent.last_result_file:
                    print(f"📄 已存檔 #{agent.last_result_id}（logs/{TOOL_RESULTS_DIRNAME}/{agent.last_result_file}）；"
                          f"之後可用 result_grep {agent.last_result_id} <關鍵字> 回查")
                print(f"🧰 Tool Tokens: {tool_tokens}")
                if tool_tokens > TOOL_RESULT_TOKEN_THRESHOLD and not is_exempt_result(result, tool_tokens):
                    print(oversized_notice(agent, tool_tokens, tool_summary_mode))
            else:
                print("✅ 無工具需要執行")

            # --- 📊 TOKEN 統計顯示 ---
            print(f"\n📦 Context Tokens: {agent.context_tokens()} / 軟水位 {SOFT_TOKEN_THRESHOLD}"
                  f" / 硬水位 {TOKEN_THRESHOLD}（num_ctx {NUM_CTX}）")
            # 硬水位的壓縮檢查統一在 ask_ai() 呼叫前（ensure_context_budget），軟水位在回合結束後
            # （after_turn_compression）。舊版這裡壓縮後 continue，會跳過「把工具結果加入上下文」
            # 那一步，AI 拿不到剛執行的結果而重複下同一個指令，已移除。
            print(f"📊 Stats | User: {agent.total_user_tokens} | AI: {agent.total_ai_tokens} | Tool: {agent.total_tool_tokens}")

            # --- 模式判定流程 ---
            if not result:
                # /plan 執行中：剩下的都是不需技能的步驟就算完成；/plan_exec_guard on 時還有步驟沒做就自動提醒 AI 繼續
                nudge = agent.plan_after_reply(parsed)
                for notice in agent.pop_notices():
                    print(notice)
                if nudge:
                    print("📝 計畫還沒做完，自動提醒 AI 繼續下一步。")
                    agent.messages.append({'role': 'user', 'content': nudge})
                    continue
                break

            # 1. Auto Mode
            if auto_mode:
                agent.messages.append({'role': 'user', 'content': tool_result_message(_context_content_for_cli(agent, result, tool_tokens, tool_summary_mode), parsed["action"])})
                print("♻️ Auto Continue 中...")
                continue

            # 2. Hybrid Mode
            if hybrid_mode:
                choice = input("\n🤔 Hybrid Mode - 加入上下文？(y/n): ").lower()
                if choice == 'y':
                    agent.messages.append({'role': 'user', 'content': tool_result_message(_context_content_for_cli(agent, result, tool_tokens, tool_summary_mode), parsed["action"])})
                    continue
                else:
                    _append_discarded_tool_result(agent)
                    print("🚫 該結果已被略過 (已告知 Agent 執行結束)")
                    # 這裡不使用 break，讓 AI 根據這個「工具執行完畢」的資訊繼續推論
                    continue

            # 3. Manual Mode
            choice = input("\n是否將系統結果加入上下文？(y/n/stop): ").lower()
            if choice == 'y':
                agent.messages.append({'role': 'user', 'content': tool_result_message(_context_content_for_cli(agent, result, tool_tokens, tool_summary_mode), parsed["action"])})
            elif choice == 'stop':
                break
            else:
                _append_discarded_tool_result(agent)
                print("👀 已略過")
                break

        # --- 🗜️ 回合結束：軟水位檢查（答案已印出，這裡壓縮不影響回覆延遲） ---
        after_turn_compression(agent, parallel_cal, print)

    while True:
        try:
            user_msg = input("\n👤 使用者: ")

            # 背景壓縮（/parallel_cal on）完成或失敗的通知，在使用者下一次輸入後印出
            for notice in agent.pop_notices():
                print(notice)
            if agent.compression_in_progress():
                print("🗜️ 背景壓縮仍在進行中，超過硬水位時會先等它完成。")

            # --- 基礎指令 ---
            if user_msg.lower() in ['exit', 'quit']:
                break
            if user_msg.lower() == '/clear':
                agent.reset_conversation()
                print("🧹 記憶已清空。")
                continue

            # --- 新增：手動壓縮指令 ---
            if user_msg.lower() == '/compress':
                if agent.compress_context_to_file():
                    print("🗜️ 歷史已手動壓縮並歸檔。")
                else:
                    print("ℹ️ 目前沒有需要壓縮的舊對話。")
                continue
            
            # --- 模式切換指令 ---
            if user_msg.lower() == '/auto on':
                auto_mode = True
                print("🤖 已開啟 Auto Continue 模式")
                continue
            if user_msg.lower() == '/auto off':
                auto_mode = False
                print("🛑 已關閉 Auto Continue 模式")
                continue
            if user_msg.lower() == '/hybrid on':
                hybrid_mode = True
                print("🧬 已開啟 Hybrid Mode")
                continue
            if user_msg.lower() == '/hybrid off':
                hybrid_mode = False
                print("🧬 已關閉 Hybrid Mode")
                continue
            if user_msg.lower() == '/summarize on':
                tool_summary_mode = True
                print("🧠 已開啟工具回傳的任務導向摘要（預設）：超過門檻的結果由獨立 session 依使用者目標與這一步的目的擷取重點後再交給主對話")
                continue
            if user_msg.lower() == '/summarize off':
                tool_summary_mode = False
                print("🧠 已關閉工具回傳的任務導向摘要：超過門檻的結果只給主對話成功/失敗判定（不多花一次模型呼叫）")
                continue
            if user_msg.lower() == '/parallel_cal on':
                parallel_cal = True
                print("⚡ 已開啟平行壓縮：回合結束後超過軟水位時在背景執行緒壓縮，不擋下一次對話"
                      "（要真的平行需 Ollama 給此模型多個 slot，或以 AGENT_SUMMARY_MODEL 指定不同的摘要模型；硬水位仍為同步）")
                continue
            if user_msg.lower() == '/parallel_cal off':
                parallel_cal = False
                print("🔁 已關閉平行壓縮：改回序列處理，回合結束後超過軟水位時同步壓縮完再等待輸入")
                continue
            if user_msg.lower() == '/skills':
                cat = None
                for sk in agent.list_skills():
                    if sk["category"] != cat:
                        cat = sk["category"]; print(f"\n【{cat}】")
                    print(f"  {sk['name']:<20} {sk['description']}")
                print("\n輸入 /skill <名稱> 可手動載入規格，隨下一則訊息一起送出")
                continue
            if user_msg.lower().startswith('/skill ') or user_msg.lower() == '/skill':
                name = user_msg[len('/skill'):].strip()
                block = agent.manual_skill_block(name) if name else None
                if block is None:
                    print(f"⚠️ 找不到技能 '{name}'，輸入 /skills 查看可用名稱" if name else "用法：/skill <技能名稱>")
                    continue
                if any(b == block for b in pending_skill_blocks):
                    print(f"ℹ️ 技能 {name} 的規格已在待送清單")
                    continue
                pending_skill_blocks.append(block)
                print(f"\n📘 已載入技能 {name} 的規格（≈{agent.count_tokens(block)} tokens），會隨你下一則訊息一起送出：\n{'-'*30}\n{block}\n{'-'*30}")
                continue
            if user_msg.lower() == '/plan on':
                plan_mode = True
                print("📝 已開啟 Plan 模式（下一個新任務會先規劃步驟，經你核准後才執行；核准後自動退出）")
                continue
            if user_msg.lower() == '/plan off':
                plan_mode = False
                print("📝 已關閉 Plan 模式（恢復直接執行）")
                continue
            if user_msg.lower() == '/trajectory':
                print(agent.format_trajectory())
                continue
            if user_msg.lower() == '/make_skill' or user_msg.lower().startswith('/make_skill '):
                _run_make_skill_flow(agent, user_msg[len('/make_skill'):].strip())
                continue
            if user_msg.lower() == '/plan done':
                if agent.plan or agent.current_plan:
                    agent.plan_clear()
                    print("✅ 已清除目前的計畫與草稿")
                else:
                    print("ℹ️ 目前沒有進行中的計畫")
                continue
            if user_msg.lower() in ('/plan_exec_guard on', '/plan_exec_guard off'):
                agent.plan_exec_guard = user_msg.lower().endswith('on')
                print("📝 已開啟計畫執行前檢查：計畫執行中，不是目前這一步、又會改變狀態的技能不執行；AI 停下來會自動提醒它繼續；"
                      "連續失敗 3 次退出計畫" if agent.plan_exec_guard else "📝 已關閉計畫執行前檢查：計畫只顯示進度，沒有限制")
                continue
            # /plan add|insert|edit|del|move|show：使用者直接改草稿（不經過模型）；有草稿時進入核准流程
            handled, plan_msg = agent.plan_edit_command(user_msg)
            if handled:
                print(plan_msg)
                if agent.plan_status() == "draft" and _run_plan_flow(agent):
                    execute_turn()
                continue
            if user_msg.lower() == '/context_mode' or user_msg.lower().startswith('/context_mode '):
                arg = user_msg[len('/context_mode'):].strip()
                if not arg:
                    print(f"目前的上下文模式：{agent.context_mode}——{CONTEXT_MODE_DESCRIPTIONS[agent.context_mode]}"
                          f"\n可切換：/context_mode harness｜/context_mode claude_code")
                    continue
                try:
                    mode = agent.set_context_mode(arg)
                    print(f"🔀 已切換上下文模式：{mode}——{CONTEXT_MODE_DESCRIPTIONS[mode]}（之後的工具回傳才照新模式處理）")
                except ValueError as e:
                    print(f"⚠️ {e}")
                continue
            if user_msg.lower() in ('/guard on', '/guard off'):
                agent.guard_enabled = user_msg.lower() == '/guard on'
                print("🛡️ 已開啟執行前關卡：會改變系統狀態的技能執行前會先問你" if agent.guard_enabled else
                      "⚠️ 已關閉執行前關卡（只限這次執行）：派工單、取消任務、建容器、容器內非唯讀指令將不經確認直接執行")
                continue

            # --- 🎯 OBJECTIVE 設定 ---
            if user_msg.lower() == "objective set":
                print("\n🎯 進入任務錨點設定模式 (輸入 objective end 結束)")
                objective_lines = []
                while True:
                    line = input("OBJECTIVE >> ")
                    if line.lower() == "objective end": break
                    objective_lines.append(line)
                agent.sticky_objective = "\n".join(objective_lines).strip()
                continue
            if user_msg.lower() == "objective show":
                print(f"\n🎯 Current: {agent.sticky_objective or 'None'}")
                continue
            if user_msg.lower() == "objective clear":
                agent.sticky_objective = ""
                print("🧹 已清除 Sticky Objective")
                continue

            # --- 📥 USER TOKEN 計算 ---
            user_tokens = agent.count_tokens(user_msg)
            agent.total_user_tokens += user_tokens
            print(f"📥 User Tokens: {user_tokens}")

            # 新訊息：做完或退出的計畫清掉；執行中的保留（使用者可能在回答追問），/plan done 提早結束
            cleared_note = agent.end_plan_for_new_task()
            if cleared_note:
                print(cleared_note)

            # 記下使用者這句話（任務線：最近 3 句），獨立 session 摘要或 recall 時
            # 拿它當「使用者的目標」的一部分（見 _build_task_anchor_text）
            agent.set_current_task(user_msg)

            # 📘 手動載入的技能規格附在這則訊息後面一起送出（與 Web Console 一致）
            content = user_msg
            if pending_skill_blocks:
                content = attach_skill_docs(user_msg, pending_skill_blocks)
                agent.total_tool_tokens += sum(agent.count_tokens(b) for b in pending_skill_blocks)
                print(f"📘 隨訊息載入 {len(pending_skill_blocks)} 份技能規格")
                pending_skill_blocks.clear()

            # --- 📝 PLAN 模式：先規劃、經使用者核准才進入下面的執行迴圈 ---
            if plan_mode:
                if not _run_plan_flow(agent, content):
                    after_turn_compression(agent, parallel_cal, print)  # 取消也算回合結束
                    continue  # 使用者取消了計畫，維持 Plan 模式，回到最上層等待新的輸入
                # 核准即退出 Plan 模式：規劃階段結束，這個任務依計畫執行，下一個新任務直接執行
                plan_mode = False
                print("📝 計畫已核准，已自動退出 Plan 模式（要再規劃下一個任務請重新 /plan on）")
            else:
                agent.messages.append({'role': 'user', 'content': content})

            execute_turn()

        except KeyboardInterrupt:
            print("\n👋 Bye")
            break
