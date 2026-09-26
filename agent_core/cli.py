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
from .protocol import attach_skill_docs, is_exempt_result, tool_result_message
from .turn import _append_discarded_tool_result, _content_for_context, after_turn_compression, context_kind


def _run_plan_flow(agent, user_task):
    """/plan 模式：先讓 AI 依 SKILLS.md 規劃步驟、印出來給使用者看，
    使用者核准後才讓 main() 的主迴圈開始真正執行（ask_ai -> run_tool）。

    安全設計：規劃階段自始至終不會呼叫 agent.run_tool()，就算模型不聽話
    在計畫裡夾帶了 EXECUTE: 指令也不會被執行——確認關卡是靠「這個函式
    根本不執行工具」保證的，不依賴模型是否遵守「先不要執行」的指示。

    回傳 True 代表使用者已核准，main() 可以繼續往下進入正常執行迴圈；
    回傳 False 代表使用者取消，本次任務到此為止，不會呼叫任何工具。
    """
    agent.messages.append({'role': 'user', 'content': agent.build_plan_request(user_task)})

    while True:
        plan_raw = agent.ask_ai()
        agent.total_ai_tokens += agent.last_ai_tokens(plan_raw)
        agent.messages.append({'role': 'assistant', 'content': plan_raw})
        parsed = agent.parse_reply(plan_raw)
        if agent.last_reply_retry:
            print(agent.last_reply_retry)
        plan_msg = parsed["reply"] or plan_raw  # 計畫文字在 reply；就算模型夾帶了 action，這裡也不會執行
        if parsed["thought"]:
            print(f"💭 {parsed['thought']}")
        print(f"\n📝 AI 規劃的任務計畫:\n{'-'*30}\n{plan_msg}\n{'-'*30}")

        choice = input("\n是否核准此計畫並開始執行？(y=核准 / n=取消 / 直接輸入修改意見=重新規劃): ").strip()

        if choice.lower() == 'y':
            agent.confirm_plan(plan_msg)
            return True

        if choice == "" or choice.lower() in ('n', 'no'):
            agent.messages.append({
                'role': 'user',
                'content': "[PLAN_REJECTED]\n使用者取消了上述計畫，本次任務不會執行，請等待使用者的新指示。"
            })
            print("🚫 已取消，本次任務不會執行。")
            return False

        # 其餘輸入視為修改意見，重新規劃一次
        agent.messages.append({'role': 'user', 'content': agent.build_plan_revision_request(choice)})


def _context_content_for_cli(agent, result, tool_tokens, use_summary):
    """CLI 用：算出要餵給主對話的內容，並印出「主對話實際收到什麼」：完整原文只印一行提示（原文剛才已完整印過），
    任務導向摘要與成功／失敗判定整段印出（Web Console 以 🧠 卡片顯示同一份內容）。"""
    content = _content_for_context(result, tool_tokens, agent=agent, use_summary=use_summary)
    kind = context_kind(content, tool_tokens)
    if kind == "summary":
        print(f"\n🧠 獨立 session 任務導向摘要（主對話實際收到的內容）:\n{'-'*30}\n{content}\n{'-'*30}")
    elif kind == "reduced":
        print(f"\n🧠 主對話實際收到的內容（只有成功／失敗判定）:\n{'-'*30}\n{content}\n{'-'*30}")
    elif kind == "doc":
        print(f"🧠 主對話收到：完整規格文件（≈{tool_tokens} tokens，不受門檻限制）")
    else:
        print(f"🧠 主對話收到：完整原文（≈{tool_tokens} tokens，未縮減）")
    return content

def _run_make_skill_flow(agent, arg_text):
    """CLI 的 /make_skill：草擬 → 預覽 → y 核准／t 重播驗證後核准／n 取消／其他文字＝修改意見重擬。
    與 Web 的 handle_skill_draft_response 用同一套 SkillAgent 狀態機。"""
    parts = arg_text.split()
    if not parts:
        print("用法：/make_skill <技能名稱> [步驟範圍，例如 3-7、3,5,8 或 all]；先用 /trajectory 查看已記錄的步驟")
        return
    name, spec = parts[0], (parts[1] if len(parts) > 1 else None)
    print(f"🧩 正在依操作軌跡草擬技能 {name}（模型 {agent.skill_model}）…")
    draft, err = agent.start_skill_draft(name, spec)
    if err:
        print(f"⚠️ {err}")
        return
    print(agent.skill_draft_preview(draft))
    while True:
        choice = input("\n核准並註冊(y) / 先重播驗證再註冊(t) / 取消(n) / 直接輸入修改意見: ").strip()
        lower = choice.lower()
        if lower in ('y', 't'):
            ok, msg = agent.approve_skill_draft(replay=(lower == 't'))
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
    print("="*50)

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
                    print(f"  {sk['name']:<20} {sk['description']}" + ("（含經驗記憶）" if sk["has_memory"] else ""))
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
                if agent.current_plan:
                    agent.current_plan = None
                    print("✅ 已提早清除目前的計畫（system prompt 不再提醒 AI 依計畫執行；平常會在下一個新任務送出時自動清除）")
                else:
                    print("ℹ️ 目前沒有進行中的計畫")
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

            # 新任務開始：上一個已核准的計畫到此結束，自動清除。計畫的生命週期 = 核准後那個任務的
            # 執行期間（期間的工具決策、auto 迴圈、自動壓縮都不會清掉它）；再打一句新訊息就是新任務。
            # 舊版要求手動 /plan done，容易忘記而讓舊計畫殘留在 system prompt 干擾之後的每個任務。
            if agent.current_plan:
                agent.current_plan = None
                print("🧹 上一個已核准的計畫已隨新任務自動清除（system prompt 不再要求依舊計畫執行）")

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
                # 核准即退出 Plan 模式：規劃階段結束，這個任務依 current_plan 執行，下一個新任務直接執行
                plan_mode = False
                print("📝 計畫已核准，已自動退出 Plan 模式（要再規劃下一個任務請重新 /plan on）")
            else:
                agent.messages.append({'role': 'user', 'content': content})

            while True:
                # --- 🧠 AI 推論 ---
                ai_msg = agent.ask_ai()
                ai_tokens = agent.last_ai_tokens(ai_msg)
                agent.total_ai_tokens += ai_tokens
                if agent.auto_compressed:
                    print("📦 上下文超過門檻，呼叫前已自動壓縮並歸檔。")
                parsed = agent.parse_reply(ai_msg)  # 回覆協議：顯示 reply／thought，執行只看 action
                if agent.last_reply_retry:
                    print(agent.last_reply_retry)
                print(f"\n🧠 AI:\n{'-'*30}\n{agent.format_reply_for_console(parsed)}\n{'-'*30}")
                print(f"📤 AI Tokens: {ai_tokens}")
                agent.messages.append({'role': 'assistant', 'content': ai_msg})

                # --- 🧰 TOOL 執行（只看 action 欄位，reply 裡的文字不會被執行）---
                result = agent.run_tool(parsed)
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
                        print(f"⚠️ 此工具回傳約 {tool_tokens} tokens，超過門檻 {TOOL_RESULT_TOKEN_THRESHOLD}，"
                              + ("加入上下文前將交由獨立 session 依目前任務擷取重點。" if tool_summary_mode
                                 else "加入上下文時只保留成功／失敗判定（/summarize on 可改為獨立 session 摘要）。"))
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

        except KeyboardInterrupt:
            print("\n👋 Bye")
            break
