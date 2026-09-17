"""
迴歸測試：OKF 隨需載入重構（NEED_TOOL / EXECUTE / 3 檔記憶 / 壓縮清除追蹤狀態）。

驗證範圍：
- system prompt 組裝內容（INDEX.md、LOADED_TOOL_SPECS_THIS_SESSION 狀態列、
  三檔長期記憶、manage_skill 索引列）。
- check_need_tool()：首次載入、快取命中、找不到的技能、路徑穿越／shell
  injection 形狀的名稱防護、沒有 NEED_TOOL 標記時回傳 None。
- run_tool()：未經 NEED_TOOL 載入仍可執行（soft-allow）並帶 [INFO] 提醒；
  已載入的技能執行時不帶提醒；cd_cmd.py 確實透過 [CWD_CHANGED] 同步狀態。
- compress_context_to_file() 會連帶清空所有技能載入追蹤狀態。
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


agent = SkillAgent(model="gemma4:e4b", max_history=30)
agent.reset_conversation()

# --- system prompt 內容 ---
sp = agent.get_system_prompt()
check("system prompt references INDEX.md section", "Available Skills (INDEX.md)" in sp)
check("system prompt shows LOADED_TOOL_SPECS_THIS_SESSION status line", "LOADED_TOOL_SPECS_THIS_SESSION" in sp)
check("system prompt contains 3-way memory section headers", "語意記憶" in sp and "情節記憶" in sp and "程序記憶" in sp)
check("system prompt contains manage_skill in INDEX", "manage_skill" in sp)

# --- NEED_TOOL：首次載入（change_dir 是「顯示名稱 != 腳本檔名」的橋接案例）---
tool_doc, fresh_name = agent.check_need_tool("NEED_TOOL: change_dir")
check("NEED_TOOL change_dir returns [PASS]", tool_doc is not None and tool_doc.startswith("[PASS]"))
check("NEED_TOOL change_dir content includes frontmatter", "type: Tool" in tool_doc)
check("loaded_tools now contains change_dir", "change_dir" in agent.loaded_tools)
check("loaded_scripts bridges to cd_cmd.py (name != script filename case)", "cd_cmd.py" in agent.loaded_scripts)
check("fresh load reports the skill name for message-tracking", fresh_name == "change_dir")

# --- 重複 NEED_TOOL：快取命中 ---
tool_doc2, fresh_name2 = agent.check_need_tool("NEED_TOOL: change_dir")
check("repeat NEED_TOOL returns cheap [INFO] already-loaded", tool_doc2 is not None and tool_doc2.startswith("[INFO]"))
check("cache-hit does NOT report a freshly-loaded name", fresh_name2 is None)

# --- 找不到的技能 ---
tool_doc3, fresh_name3 = agent.check_need_tool("NEED_TOOL: totally_unknown_skill_xyz")
check("unknown skill returns [ERROR]", tool_doc3 is not None and tool_doc3.startswith("[ERROR]"))
check("unknown skill does not report a freshly-loaded name", fresh_name3 is None)

# --- 路徑穿越／shell injection 形狀的名稱防護 ---
tool_doc4, _ = agent.check_need_tool("NEED_TOOL: ../../../etc/passwd")
check("path traversal name never resolves outside tools_dir", tool_doc4 is not None and "[ERROR]" in tool_doc4)
tool_doc5, _ = agent.check_need_tool("NEED_TOOL: rm -rf /")
check("shell-injection-shaped name rejected as illegal", tool_doc5 is not None and "不合法" in tool_doc5)

# --- 沒有 NEED_TOOL 標記 ---
tool_doc6, _ = agent.check_need_tool("just a normal reply, no marker here")
check("no NEED_TOOL marker returns None", tool_doc6 is None)

# --- run_tool()：未載入技能仍可執行（soft-allow）並帶提醒 ---
result_unloaded = agent.run_tool('EXECUTE: find_file_cmd.py "Agent_Runner" .')
check(
    "EXECUTE on unloaded skill still runs (soft-allow) and includes [INFO] reminder",
    result_unloaded is not None and "[INFO]" in result_unloaded,
)

# --- run_tool()：已載入技能（change_dir -> cd_cmd.py）執行時不帶提醒，且確實同步 cwd ---
result_loaded = agent.run_tool("EXECUTE: cd_cmd.py /tmp")
check(
    "EXECUTE on loaded skill has no reminder prefix",
    result_loaded is not None and not result_loaded.startswith("[INFO] 提醒"),
)
check("cd actually updated current_cwd via [CWD_CHANGED]", agent.current_cwd.rstrip("/") == "/tmp")

check("loaded_tools non-empty before compression (sanity)", len(agent.loaded_tools) > 0)

# --- 壓縮後應清空所有技能載入追蹤狀態 ---
for i in range(8):
    agent.messages.append({"role": "user", "content": f"filler message {i}"})
agent.compress_context_to_file(num_to_keep=2)

check("loaded_tools cleared after compression", len(agent.loaded_tools) == 0)
check("loaded_scripts cleared after compression", len(agent.loaded_scripts) == 0)
check("consecutive_need_tool_count reset after compression", agent.consecutive_need_tool_count == 0)
check("tool_spec_messages cleared after compression", len(agent.tool_spec_messages) == 0)
check("tool_load_turn cleared after compression", len(agent.tool_load_turn) == 0)
check("tool_last_used_turn cleared after compression", len(agent.tool_last_used_turn) == 0)

print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}"))
sys.exit(1 if failures else 0)
