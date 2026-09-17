"""
迴歸測試：上下文預算監控（token 量測、事前壓縮、/budget、/objective 單行版本）。

驗證範圍：
- token_threshold 預設值與建構子覆寫。
- get_context_token_count()：只量測 messages（不重複計算 system prompt）。
- compress_context_to_file()：保留最新 N 筆、system prompt 不受影響。
- enforce_context_budget()：超過閾值才壓縮，且回傳真實的「是否真的壓縮了」。
- ask_ai() 每次呼叫模型前只呼叫一次 enforce_context_budget()。
- /budget、/objective（單行版本）指令解析邏輯。
"""
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from Agent_Runner import SkillAgent, DEFAULT_TOKEN_THRESHOLD

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# --- token_threshold 預設值與覆寫 ---
check("default token_threshold matches DEFAULT_TOKEN_THRESHOLD", SkillAgent(model="gemma4:e4b").token_threshold == DEFAULT_TOKEN_THRESHOLD)
check("constructor token_threshold override works", SkillAgent(model="gemma4:e4b", token_threshold=42).token_threshold == 42)

# --- get_context_token_count()：不重複計算 system prompt ---
agent = SkillAgent(model="gemma4:e4b", max_history=30)
agent.reset_conversation()
agent.messages.append({"role": "user", "content": "hello world, this is a test message to pad token count."})

new_count = agent.get_context_token_count()
joined = "".join(f"{m['role']}: {m['content']}\n" for m in agent.messages)
expected = agent.count_tokens(joined)
old_double_counted = agent.count_tokens(agent.get_system_prompt() + "\n" + joined)
check("get_context_token_count matches messages-only count (no double count of system prompt)", new_count == expected)
check("new count is smaller than the old double-counted formula (proves double-count was removed)", new_count < old_double_counted)

# --- compress_context_to_file()：保留最新 N 筆 ---
agent2 = SkillAgent(model="gemma4:e4b", max_history=100)
agent2.reset_conversation()
for i in range(12):
    agent2.messages.append({"role": "user", "content": f"filler message number {i}"})
before_len = len(agent2.messages)
agent2.compress_context_to_file(num_to_keep=2)
check("messages length after compression is 1+num_to_keep=3 (was 13 before)", before_len == 13 and len(agent2.messages) == 3)
check("messages[0] is still the system role", agent2.messages[0]["role"] == "system")
check(
    "the two most recent filler messages actually survived compression",
    agent2.messages[-1]["content"] == "filler message number 11" and agent2.messages[-2]["content"] == "filler message number 10",
)

# --- enforce_context_budget()：用極小閾值強制觸發壓縮 ---
agent3 = SkillAgent(model="gemma4:e4b", max_history=100, token_threshold=50)
agent3.reset_conversation()
for i in range(12):
    agent3.messages.append({"role": "user", "content": f"filler message number {i} " * 20})

before_check = agent3.get_context_token_count()
check("pre-check: context is indeed over the tiny threshold before enforcing", before_check > agent3.token_threshold)

compressed, count_after = agent3.enforce_context_budget()
check("enforce_context_budget reports it compressed", compressed is True)
check("token count dropped after enforcement", count_after < before_check)

compressed2, count_after2 = agent3.enforce_context_budget()
print(f"[INFO] second enforce call compressed={compressed2} (fine either way, just confirming no crash)")

# --- ask_ai() 每次呼叫模型前只呼叫一次 enforce_context_budget() ---
call_count = {"n": 0}
original_enforce = agent3.enforce_context_budget


def counting_wrapper():
    call_count["n"] += 1
    return original_enforce()


agent3.enforce_context_budget = counting_wrapper
agent3.messages.append({"role": "user", "content": "one more quick message"})
agent3.ask_ai()
check("ask_ai() calls enforce_context_budget() exactly once per call", call_count["n"] == 1)


# --- /budget 指令解析（比照 main() 邏輯）---
def parse_budget(user_msg, agent):
    remainder = user_msg[len("/budget"):].strip()
    if not remainder:
        return ("show", agent.token_threshold)
    try:
        agent.token_threshold = int(remainder)
        return ("set", agent.token_threshold)
    except ValueError:
        return ("error", None)


agent4 = SkillAgent(model="gemma4:e4b")
r = parse_budget("/budget", agent4)
check("/budget with no args reports current value", r == ("show", agent4.token_threshold))
r = parse_budget("/budget 999", agent4)
check("/budget 999 sets threshold to 999", r == ("set", 999) and agent4.token_threshold == 999)
r = parse_budget("/budget notanumber", agent4)
check("/budget notanumber reports error, doesn't crash", r == ("error", None))


# --- /objective 單行版本指令解析 ---
def parse_objective(user_msg, agent):
    remainder = user_msg[len("/objective"):].strip()
    if not remainder:
        return ("show", agent.sticky_objective)
    elif remainder.lower() == "clear":
        agent.sticky_objective = ""
        return ("clear", None)
    else:
        agent.sticky_objective = remainder
        return ("set", remainder)


agent5 = SkillAgent(model="gemma4:e4b")
r = parse_objective("/objective", agent5)
check("/objective with no args shows current (None initially)", r == ("show", None))
r = parse_objective("/objective finish the report", agent5)
check("/objective <text> sets sticky_objective in one line", r == ("set", "finish the report") and agent5.sticky_objective == "finish the report")
r = parse_objective("/objective clear", agent5)
check("/objective clear clears it", r == ("clear", None) and agent5.sticky_objective == "")

print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}"))
sys.exit(1 if failures else 0)
