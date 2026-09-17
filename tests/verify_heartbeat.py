"""
迴歸測試：心跳機制（背景執行緒、logs/heartbeat.md、/heartbeat interval）。

驗證範圍：
- 預設間隔／健檢清單與常數一致。
- start_heartbeat()/stop_heartbeat()/is_heartbeat_running() 的狀態機行為
  （重複啟動、重複停止皆不出錯）。
- 背景執行緒真的會定期寫入心跳紀錄檔，且絕對不會碰 self.messages。
- get_system_prompt() 會反映心跳運作狀態與最近的心跳紀錄。
- /clear（reset_conversation）不會連帶停止正在運作的心跳。
- /heartbeat interval 指令解析。

為避免污染專案內真正的 logs/heartbeat.md，改用系統暫存目錄下的一次性檔案。
"""
import sys
import os
import time
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from Agent_Runner import SkillAgent, DEFAULT_HEARTBEAT_INTERVAL_SECONDS, DEFAULT_HEARTBEAT_CHECKS

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


TEST_LOG = os.path.join(tempfile.gettempdir(), "ai_agent_harness_test_heartbeat.md")
if os.path.exists(TEST_LOG):
    os.remove(TEST_LOG)

agent = SkillAgent(model="gemma4:e4b", max_history=30, heartbeat_interval=1)
agent.heartbeat_log_file = TEST_LOG  # 重導向，避免污染真正的 logs/heartbeat.md
agent.reset_conversation()

check("default heartbeat_interval matches constant", SkillAgent(model="gemma4:e4b").heartbeat_interval == DEFAULT_HEARTBEAT_INTERVAL_SECONDS)
check("default heartbeat_checks matches constant", SkillAgent(model="gemma4:e4b").heartbeat_checks == DEFAULT_HEARTBEAT_CHECKS)
check("not running before start", agent.is_heartbeat_running() is False)

messages_len_before = len(agent.messages)

started = agent.start_heartbeat()
check("start_heartbeat returns True on first call", started is True)
check("is_heartbeat_running is True right after start", agent.is_heartbeat_running() is True)

started_again = agent.start_heartbeat()
check("calling start_heartbeat again while running returns False (no duplicate thread)", started_again is False)

# interval 是 1 秒，等久一點確保至少跑過一輪
time.sleep(2.5)

check("heartbeat log file was created", os.path.exists(TEST_LOG))
if os.path.exists(TEST_LOG):
    content = open(TEST_LOG, encoding="utf-8").read()
    check("heartbeat log contains the check label 'robot_ping'", "robot_ping" in content)
    check("heartbeat log contains a real robot_ping result string", "V4.9" in content or "核心引擎" in content)
    check("heartbeat log has at least 2 entries after 2.5s at 1s interval", content.count("### 心跳檢查") >= 2)

check("heartbeat thread NEVER touched self.messages (still same length)", len(agent.messages) == messages_len_before)

tail = agent.load_recent_heartbeat_checks(max_lines=50)
check("load_recent_heartbeat_checks returns real content, not the placeholder", tail != "No heartbeat checks yet." and "robot_ping" in tail)

sp = agent.get_system_prompt()
check("system prompt shows HEARTBEAT: 運作中 while running", "HEARTBEAT: 運作中" in sp)
check("system prompt includes the Recent Heartbeat Checks section with real content", "Recent Heartbeat Checks" in sp and "robot_ping" in sp)

stopped = agent.stop_heartbeat()
check("stop_heartbeat returns True", stopped is True)
check("is_heartbeat_running is False after stop", agent.is_heartbeat_running() is False)

stopped_again = agent.stop_heartbeat()
check("stopping again when already stopped returns False, no crash", stopped_again is False)

count_after_stop = open(TEST_LOG, encoding="utf-8").read().count("### 心跳檢查") if os.path.exists(TEST_LOG) else 0
time.sleep(2)
count_later = open(TEST_LOG, encoding="utf-8").read().count("### 心跳檢查") if os.path.exists(TEST_LOG) else 0
check("no new heartbeat entries appear after stop (thread truly stopped, not just flagged)", count_after_stop == count_later)

sp2 = agent.get_system_prompt()
check("system prompt shows HEARTBEAT: 已停止 after stop", "HEARTBEAT: 已停止" in sp2)

# --- reset_conversation()（/clear）不應影響正在運作的心跳（兩者是獨立的關注點）---
agent.start_heartbeat()
agent.reset_conversation()
check("/clear (reset_conversation) does not stop an already-running heartbeat", agent.is_heartbeat_running() is True)
agent.stop_heartbeat()


# --- /heartbeat interval 指令解析（比照 main() 邏輯）---
def parse_heartbeat_interval(user_msg, agent):
    remainder = user_msg[len("/heartbeat interval"):].strip()
    if not remainder:
        return ("show", agent.heartbeat_interval)
    try:
        agent.heartbeat_interval = int(remainder)
        return ("set", agent.heartbeat_interval)
    except ValueError:
        return ("error", None)


r = parse_heartbeat_interval("/heartbeat interval", agent)
check("/heartbeat interval with no args shows current value", r == ("show", agent.heartbeat_interval))
r = parse_heartbeat_interval("/heartbeat interval 600", agent)
check("/heartbeat interval 600 sets it", r == ("set", 600) and agent.heartbeat_interval == 600)
r = parse_heartbeat_interval("/heartbeat interval abc", agent)
check("/heartbeat interval abc reports error, doesn't crash", r == ("error", None))

if os.path.exists(TEST_LOG):
    os.remove(TEST_LOG)

print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}"))
sys.exit(1 if failures else 0)
