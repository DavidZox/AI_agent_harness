"""跑一個評測情境（run_evals.py 在臨時的專案副本裡呼叫它；不要直接在正式專案目錄執行）。

    python3 evals/worker.py <情境 id> <輸出 json>
環境變數：AGENT_CONTEXT_MODE（harness／claude_code）、EVAL_MODEL（預設 gemma4:26b）、EVAL_FAKE=1（假模型，只測流程）。

流程比照 CLI 的 auto 模式（工具結果自動加入上下文、連續執行到模型不再下 action），差別只有：外部工具換成
scenarios 的假資料、執行前關卡依情境自動回答、每一句使用者訊息最多 MAX_STEPS 次模型回覆。"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

MAX_STEPS = 20   # 每一句使用者訊息最多幾次模型回覆（Web 的 auto 上限是 25；claude_code 模式每步還要自己打勾，給太少不公平）
RESULT_SCRIPTS = {"result_recall_cmd.py", "result_grep_cmd.py", "result_view_cmd.py", "result_list_cmd.py"}


def install_fake_model():
    """EVAL_FAKE=1：不呼叫 Ollama，只驗證評測流程本身（主對話第一句就直接回答、不下 action）。"""
    import ollama

    def fake_chat(model, messages, format=None, **kw):
        props = sorted((format or {}).get("properties", {})) if isinstance(format, dict) else []
        if "action" in props:
            data = {"thought": "（假模型）", "reply": "（假模型的回答）", "action": None}
        elif "index_hint" in props:
            data = {"answer": "（假）", "facts": [], "errors": [], "not_covered": "無", "suggested_questions": [],
                    "index_hint": "（假）", "related_records": []}
        else:
            data = {"overview": "（假）", "key_progress": [], "results_and_errors": [], "user_preferences": [], "open_items": []}
        return {"message": {"content": json.dumps(data, ensure_ascii=False)}, "prompt_eval_count": 100, "eval_count": 10}
    ollama.chat = fake_chat


def seed_archives(results_dir, seeds):
    """把「之前的存檔」寫進 logs/tool_results/：原文檔（跟 ArchiveMixin._archive_tool_result 同一種檔頭）、
    index.md 一行、tools_use_index.md 一行（檢索清單）。"""
    os.makedirs(results_dir, exist_ok=True)
    for s in seeds:
        stem = s["script"].replace("_cmd.py", "").replace(".py", "")
        filename = f"{s['session']}_{s['id']:03d}_{stem}.md"
        header = ["---", f"id: {s['id']}", f"session: {s['session']}", f"ts: {s['ts']}", f"script: {s['script']}",
                  f"skill: {s['skill']}", f"command: EXECUTE: scripts/{s['script']}", f"cwd: {ROOT}", "container: rmf_sim",
                  "status: PASS", f"chars: {len(s['output'])}", f"task: {s['task']}", "---"]
        with open(os.path.join(results_dir, filename), "w", encoding="utf-8") as f:
            f.write("\n".join(header) + "\n" + s["output"] + "\n")
        with open(os.path.join(results_dir, "index.md"), "a", encoding="utf-8") as f:
            f.write(f"#{s['id']} | {s['ts'][:16]} | {s['script']} | PASS | {len(s['output'])} 字 | {filename} | "
                    f"任務：{s['task']} | 回答：\n")
        with open(os.path.join(results_dir, "tools_use_index.md"), "a", encoding="utf-8") as f:
            f.write(f"#{s['id']} | {s['ts'][:16]} | {s['session']} | {filename} | {s['hint']}\n")


def evaluate(expect, turn):
    """一句使用者訊息的回合結束後，檢查 expect 裡的每個條件，回傳 {條件: bool}。
    reply_* 看這一句回合裡模型所有的 reply（使用者都看得到；多步驟任務常在前一則就報告完、最後一則只是收尾）。"""
    reply = "\n".join(s["reply"] or "" for s in turn["steps"])
    low = reply.lower()
    skills = [c["skill"] or c["script"] for c in turn["tool_calls"]]
    lookups = [c for c in turn["tool_calls"] if c["script"] in RESULT_SCRIPTS]
    checks = {}
    if "reply_all" in expect:
        checks["reply_all"] = all(s.lower() in low for s in expect["reply_all"])
    if "reply_any" in expect:
        checks["reply_any"] = any(s.lower() in low for s in expect["reply_any"])
    if "reply_none" in expect:
        checks["reply_none"] = not any(s.lower() in low for s in expect["reply_none"])
    if "tools_all" in expect:
        checks["tools_all"] = all(s in skills for s in expect["tools_all"])
    if "tools_any" in expect:
        checks["tools_any"] = any(s in skills for s in expect["tools_any"])
    if "tools_none" in expect:
        checks["tools_none"] = not any(s in skills for s in expect["tools_none"])
    if "archive_lookup" in expect:
        checks["archive_lookup"] = bool(lookups) == expect["archive_lookup"]
    if "archive_ids" in expect:
        used = " ".join(" ".join(c["args"]) for c in lookups)
        checks["archive_ids"] = all(str(i) in used for i in expect["archive_ids"])
    if "guard" in expect:
        checks["guard"] = bool(turn["guards"]) == expect["guard"]
    if "memory_skill" in expect:
        mem = [c for c in turn["tool_calls"] if c["script"] == "modify_memory_cmd.py"]
        checks["memory_skill"] = any(("--skill" in c["args"] or "--move-last-to-skill" in c["args"])
                                     and expect["memory_skill"] in c["args"] for c in mem)
    if "skill_memory_has" in expect:   # 看臨時副本裡的檔案：[技能, 要出現的字]
        skill, text = expect["skill_memory_has"]
        path = os.path.join(ROOT, "skills_system", "memory", f"{skill}.md")
        checks["skill_memory_has"] = os.path.exists(path) and text in open(path, encoding="utf-8").read()
    if "global_memory_lacks" in expect:
        mem_text = open(os.path.join(ROOT, "Memory.md"), encoding="utf-8").read() if os.path.exists(os.path.join(ROOT, "Memory.md")) else ""
        checks["global_memory_lacks"] = not any(t in mem_text for t in expect["global_memory_lacks"])
    return checks


def main():
    scenario_id, out_path = sys.argv[1], sys.argv[2]
    if os.environ.get("EVAL_FAKE") == "1":
        install_fake_model()
    from evals.scenarios import COMMON_FIXTURES, REAL_SCRIPTS, SCENARIO_BY_ID
    sc = SCENARIO_BY_ID[scenario_id]
    if sc.get("memory") is not None:
        with open(os.path.join(ROOT, "Memory.md"), "w", encoding="utf-8") as f:
            f.write(sc["memory"])
    seed_archives(os.path.join(ROOT, "logs", "tool_results"), sc.get("seeds", []))

    from agent_core.agent import SkillAgent
    from agent_core.protocol import action_text, tool_result_message
    from agent_core.turn import _content_for_context, after_turn_compression

    agent = SkillAgent(model=os.environ.get("EVAL_MODEL", "gemma4:26b"))
    agent.reset_conversation()
    for k, v in (sc.get("state") or {}).items():
        setattr(agent, k, v)
    fixtures = {**COMMON_FIXTURES, **(sc.get("fixtures") or {})}
    real_exec = agent._exec_script

    def fixture_exec(script_name, script_path, clean_args):
        fx = fixtures.get(script_name)
        if fx is not None:
            return fx(list(clean_args), agent) if callable(fx) else fx
        if script_name in REAL_SCRIPTS:
            return real_exec(script_name, script_path, clean_args)
        return (f"[ERROR] （評測環境）沒有為 {script_name} {' '.join(clean_args)} 準備假資料：這個情境用不到它，請換別的做法。")

    agent._exec_script = fixture_exec
    skill_map = agent._script_skill_map()
    started = time.time()
    turns, all_calls = [], []
    for spec in sc["turns"]:
        t0 = time.time()
        perf_before = len(agent.perf_calls)
        agent.end_plan_for_new_task()
        agent.set_current_task(spec["user"])
        agent.messages.append({"role": "user", "content": spec["user"]})
        turn = {"user": spec["user"], "steps": [], "tool_calls": [], "guards": [], "final_reply": "", "hit_step_limit": False}
        for _ in range(MAX_STEPS):
            raw = agent.ask_ai()
            parsed = agent.parse_reply(raw)
            agent.messages.append({"role": "assistant", "content": raw})
            turn["steps"].append({"thought": parsed["thought"], "reply": parsed["reply"],
                                  "action": action_text(parsed["action"]) if parsed["action"] else None, "valid": parsed["valid"]})
            turn["final_reply"] = parsed["reply"]
            guard = agent.guard_check(parsed)
            if guard:
                turn["guards"].append(guard)
                if sc.get("guard", "deny") != "approve":
                    agent.messages.append({"role": "user", "content": tool_result_message(agent.guard_denied_text(guard), parsed["action"])})
                    break
            result = agent.run_tool(parsed, approved=bool(guard))
            if not result:
                break
            payload = action_text(parsed["action"])
            token = os.path.basename(payload.split()[0]) if payload else ""
            script = agent._normalize_script_name(token)
            is_doc = os.path.exists(os.path.join(agent.tools_dir, (token[:-3] if token.endswith(".md") else token) + ".md"))
            if not is_doc:
                args = agent._parse_script_args(script, payload.split(maxsplit=1)[1] if len(payload.split(maxsplit=1)) > 1 else "")
                call = {"script": script, "skill": skill_map.get(script) or ("result_recall" if script == "result_recall_cmd.py" else None),
                        "args": args, "status": "ERROR" if result.lstrip().startswith(("[ERROR]", "[DENIED]")) else "PASS",
                        "result_id": agent.last_result_id, "result_tokens": agent.count_tokens(result)}
                turn["tool_calls"].append(call)
                all_calls.append(call)
            content = _content_for_context(result, agent.count_tokens(result), agent=agent, use_summary=True)
            agent.messages.append({"role": "user", "content": tool_result_message(content, parsed["action"])})
        else:
            turn["hit_step_limit"] = True
        after_turn_compression(agent, False, lambda text: turn.setdefault("notices", []).append(text))
        perf = agent.perf_calls[perf_before:]
        turn["main_calls"] = sum(1 for p in perf if p["kind"] == "main")
        turn["side_calls"] = sum(1 for p in perf if p["kind"] != "main")
        turn["prompt_tokens"] = sum(p.get("prompt_tokens") or 0 for p in perf)
        turn["eval_tokens"] = sum(p.get("eval_tokens") or 0 for p in perf)
        turn["prompt_ms"] = round(sum(p.get("prompt_ms") or 0 for p in perf), 1)
        turn["seconds"] = round(time.time() - t0, 1)
        turn["checks"] = evaluate(spec.get("expect") or {}, turn)
        turn["passed"] = all(turn["checks"].values())
        turns.append(turn)
    seen, reruns = set(), 0
    for c in all_calls:
        key = (c["script"], tuple(c["args"]))
        if c["script"] not in RESULT_SCRIPTS and key in seen:
            reruns += 1
        seen.add(key)
    out = {"scenario": scenario_id, "mode": agent.context_mode, "model": agent.model,
           "passed": all(t["passed"] for t in turns), "turns": turns, "reruns": reruns,
           "seconds": round(time.time() - started, 1), "perf": agent.perf_calls,
           "context_tokens_end": agent.context_tokens()}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
