"""
迴歸測試：技能生命週期清除（evict_idle_tools()、/clear、/skill_ttl）。

驗證範圍：
- 剛載入的技能規格書會被正確追蹤（tool_spec_messages / tool_load_turn）。
- 連續閒置（未被 EXECUTE）達門檻的技能規格書會被精準清除（訊息物件本身、
  loaded_tools/loaded_scripts/追蹤字典皆同步移除）。
- 有實際被 EXECUTE 使用過的技能，閒置輪數重新從「最後使用」起算。
- 門檻設得很大時，久久沒用的技能仍受保護不被清除。
- /clear（reset_conversation）會清空所有技能生命週期追蹤狀態並歸零 turn_counter。
- /skill_ttl 指令解析。
- ask_ai() 會遞增 turn_counter，且內部自動呼叫 evict_idle_tools()。
"""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from Agent_Runner import SkillAgent

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def load_tool(agent, need_tool_marker):
    """
    複製 main() 主迴圈裡「收到 NEED_TOOL 後」的動作：check_need_tool() 本身
    只負責回傳規格書內容並登記 loaded_tools/loaded_scripts/tool_load_turn，
    真正把規格書內容包成訊息、附加進 agent.messages、並登記進
    tool_spec_messages（供 evict_idle_tools() 之後精準移除）是 main() 迴圈
    自己做的事，不是 check_need_tool() 的職責。測試若要驗證完整的載入生命
    週期，必須連同這一步一起模擬，否則 tool_spec_messages 永遠是空的。
    """
    tool_doc, fresh_name = agent.check_need_tool(need_tool_marker)
    if fresh_name:
        spec_message = {"role": "user", "content": f"[tool spec]\n{tool_doc}"}
        agent.messages.append(spec_message)
        agent.tool_spec_messages[fresh_name] = spec_message
    return tool_doc, fresh_name


# --- Agent 1：idle_eviction_turns=3，測試「從未執行過」的技能被清除 ---
agent = SkillAgent(model="gemma4:e4b", max_history=30, tool_idle_eviction_turns=3)
agent.reset_conversation()

tool_doc, fresh_name = load_tool(agent, "NEED_TOOL: change_dir")
check("freshly loaded skill tracked in tool_spec_messages", "change_dir" in agent.tool_spec_messages)
check("freshly loaded skill's load turn recorded", "change_dir" in agent.tool_load_turn)
spec_message = agent.tool_spec_messages["change_dir"]
check("spec message actually present in agent.messages", spec_message in agent.messages)

agent.turn_counter += 1
evicted = agent.evict_idle_tools()
check("not yet idle enough -> nothing evicted", evicted == [])
check("change_dir still loaded", "change_dir" in agent.loaded_tools)
check("spec message still in messages", spec_message in agent.messages)

agent.turn_counter += 2  # 累計閒置 3 輪，達到門檻
evicted = agent.evict_idle_tools()
check("idle skill (never executed) gets evicted", evicted == ["change_dir"])
check("removed from loaded_tools", "change_dir" not in agent.loaded_tools)
check("removed from loaded_scripts", "cd_cmd.py" not in agent.loaded_scripts)
check("removed from tool_spec_messages tracking", "change_dir" not in agent.tool_spec_messages)
check("the actual spec message object is gone from agent.messages", spec_message not in agent.messages)

# --- 有實際使用過的技能（view_file -> cat_cmd.py）：閒置輪數從「最後使用」起算 ---
tool_doc2, fresh_name2 = load_tool(agent, "NEED_TOOL: view_file")
check("view_file freshly loaded", fresh_name2 == "view_file")
agent.run_tool("EXECUTE: cat_cmd.py Agent_Runner.py")  # tool_last_used_turn 記為目前 turn_counter

agent.turn_counter += 2  # 使用後閒置 2 輪，未達門檻 3
evicted = agent.evict_idle_tools()
check("actively-used skill survives eviction (2 turns idle < threshold 3)", "view_file" not in evicted)

agent.turn_counter += 3  # 再閒置下去，終於超過門檻
evicted = agent.evict_idle_tools()
check("same skill finally evicted once truly idle past threshold", "view_file" in evicted)

# --- 門檻極大時，久久沒用的技能仍受保護 ---
agent2 = SkillAgent(model="gemma4:e4b", max_history=30, tool_idle_eviction_turns=1000)
agent2.reset_conversation()
load_tool(agent2, "NEED_TOOL: change_dir")
agent2.turn_counter += 50
evicted2 = agent2.evict_idle_tools()
check("large idle threshold protects a barely-used skill from eviction", evicted2 == [])

# --- /clear 清空所有技能生命週期追蹤狀態 ---
agent3 = SkillAgent(model="gemma4:e4b", max_history=30)
agent3.reset_conversation()
load_tool(agent3, "NEED_TOOL: change_dir")
agent3.run_tool("EXECUTE: cd_cmd.py /tmp")
check(
    "sanity: agent3 has loaded state before /clear",
    len(agent3.loaded_tools) > 0 and len(agent3.tool_spec_messages) > 0,
)
agent3.reset_conversation()
check("/clear wipes loaded_tools", len(agent3.loaded_tools) == 0)
check("/clear wipes loaded_scripts", len(agent3.loaded_scripts) == 0)
check("/clear wipes tool_spec_messages", len(agent3.tool_spec_messages) == 0)
check("/clear wipes tool_load_turn", len(agent3.tool_load_turn) == 0)
check("/clear wipes tool_last_used_turn", len(agent3.tool_last_used_turn) == 0)
check("/clear resets turn_counter to 0", agent3.turn_counter == 0)


# --- /skill_ttl 指令解析 ---
def parse_skill_ttl(user_msg, agent):
    remainder = user_msg[len("/skill_ttl"):].strip()
    if not remainder:
        return ("show", agent.tool_idle_eviction_turns)
    try:
        agent.tool_idle_eviction_turns = int(remainder)
        return ("set", agent.tool_idle_eviction_turns)
    except ValueError:
        return ("error", None)


r = parse_skill_ttl("/skill_ttl", agent3)
check("/skill_ttl with no args shows current value", r == ("show", agent3.tool_idle_eviction_turns))
r = parse_skill_ttl("/skill_ttl 10", agent3)
check("/skill_ttl 10 sets threshold to 10", r == ("set", 10) and agent3.tool_idle_eviction_turns == 10)
r = parse_skill_ttl("/skill_ttl notanumber", agent3)
check("/skill_ttl notanumber reports error, doesn't crash", r == ("error", None))

# --- ask_ai() 遞增 turn_counter，且內部自動呼叫 evict_idle_tools() ---
agent4 = SkillAgent(model="gemma4:e4b", max_history=30, tool_idle_eviction_turns=2)
agent4.reset_conversation()
load_tool(agent4, "NEED_TOOL: robot_ping")

agent4.messages.append({"role": "user", "content": "just checking in, no tool needed"})
before_turn = agent4.turn_counter
agent4.ask_ai()
check("ask_ai() increments turn_counter", agent4.turn_counter == before_turn + 1)
check("robot_ping survives after 1 idle turn (threshold 2)", "robot_ping" in agent4.loaded_tools)

agent4.messages.append({"role": "user", "content": "still just checking in"})
agent4.ask_ai()
check(
    "robot_ping auto-evicted by ask_ai() -> evict_idle_tools() after enough idle turns",
    "robot_ping" not in agent4.loaded_tools,
)

print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}"))
sys.exit(1 if failures else 0)
