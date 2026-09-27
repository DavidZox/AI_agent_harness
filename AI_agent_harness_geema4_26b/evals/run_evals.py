"""評測：同一組情境分別用 harness 與 claude_code 兩種上下文模式跑，比較成功率、呼叫次數、token 與時間。

    python3 evals/run_evals.py                          # 兩種模式 × 全部情境 × 1 次
    python3 evals/run_evals.py --runs 5                 # 每個組合跑 5 次（temperature 0.2 仍有隨機性，建議 ≥5）
    python3 evals/run_evals.py --modes claude_code --only fleet_battery,fleet_followup
    python3 evals/run_evals.py --fake                   # 假模型，只驗證評測流程本身（幾秒鐘）

每一次都在臨時目錄複製一份專案（agent_core、skills_system、context_modes、evals、AGENT.md、Memory.md）來跑，
不會動到正式的 logs/ 與 Memory.md。結果寫到 evals/results/<時間>/：runs.jsonl（每一次的完整紀錄，含每一步的
thought／reply／action 與每次模型呼叫的耗時）與 report.md（彙整表）。改 prompt、門檻或切換邏輯之後重跑一次，
跟上一次的 report.md 比，才知道是變好還是變壞。"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
sys.path.insert(0, PROJECT)
from evals.scenarios import SCENARIOS  # noqa: E402

COPY_ITEMS = ["agent_core", "skills_system", "context_modes", "evals", "AGENT.md", "Memory.md"]


def make_copy():
    tmp = tempfile.mkdtemp(prefix="agent_eval_")
    ignore = shutil.ignore_patterns("__pycache__", "results", "drafts")
    for item in COPY_ITEMS:
        src = os.path.join(PROJECT, item)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(tmp, item), ignore=ignore)
        elif os.path.exists(src):
            shutil.copy2(src, os.path.join(tmp, item))
    os.makedirs(os.path.join(tmp, "logs"), exist_ok=True)
    return tmp


def run_one(scenario_id, mode, args):
    tmp = make_copy()
    out_path = os.path.join(tmp, "result.json")
    env = dict(os.environ, AGENT_CONTEXT_MODE=mode, EVAL_MODEL=args.model, AGENT_PERF_LOG="1",
               PYTHONPATH=tmp, PYTHONDONTWRITEBYTECODE="1")
    if args.fake:
        env["EVAL_FAKE"] = "1"
    t0 = time.time()
    try:
        proc = subprocess.run([sys.executable, os.path.join(tmp, "evals", "worker.py"), scenario_id, out_path],
                              cwd=tmp, env=env, capture_output=True, text=True, timeout=args.timeout)
        if os.path.exists(out_path):
            with open(out_path, encoding="utf-8") as f:
                result = json.load(f)
        else:
            result = {"scenario": scenario_id, "mode": mode, "passed": False, "error": (proc.stderr or proc.stdout)[-2000:]}
    except subprocess.TimeoutExpired:
        result = {"scenario": scenario_id, "mode": mode, "passed": False, "error": f"逾時（{args.timeout} 秒）"}
    result["wall_seconds"] = round(time.time() - t0, 1)
    if args.keep:
        result["copy_dir"] = tmp
    else:
        shutil.rmtree(tmp, ignore_errors=True)
    return result


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 1) if xs else 0


def write_report(results, modes, out_dir, args):
    by = {}
    for r in results:
        by.setdefault((r["scenario"], r["mode"]), []).append(r)
    lines = [f"# 評測報告（{time.strftime('%Y-%m-%d %H:%M')}）", "",
             f"模型：{args.model}{'（假模型：只驗證流程）' if args.fake else ''}；每個組合跑 {args.runs} 次；"
             f"情境 {len({r['scenario'] for r in results})} 個。", ""]

    lines += ["## 各模式總覽", "",
              "| 模式 | 情境通過 | 回合通過 | 每回合主對話呼叫 | 每回合獨立 session 呼叫 | 每回合 prompt tokens | 每回合 prompt 毫秒 | 每個情境秒數 | 回存檔查詢 | 執行前確認 | 重跑同一個工具 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for mode in modes:
        rs = [r for r in results if r["mode"] == mode]
        turns = [t for r in rs for t in r.get("turns", [])]
        lookups = sum(1 for t in turns for c in t["tool_calls"] if c["script"].startswith("result_"))
        lines.append(
            f"| {mode} | {sum(1 for r in rs if r.get('passed'))}/{len(rs)} | {sum(1 for t in turns if t['passed'])}/{len(turns)} | "
            f"{_avg([t['main_calls'] for t in turns])} | {_avg([t['side_calls'] for t in turns])} | "
            f"{_avg([t['prompt_tokens'] for t in turns])} | {_avg([t['prompt_ms'] for t in turns])} | "
            f"{_avg([r.get('seconds') for r in rs])} | {lookups} | {sum(len(t['guards']) for t in turns)} | "
            f"{sum(r.get('reruns', 0) for r in rs)} |")
    lines += ["", "（prompt tokens 是每次呼叫 Ollama 回報的完整 prompt 長度加總；KV cache 命中時仍算完整長度，所以另外看 prompt 毫秒。）", ""]

    lines += ["## 各情境", "", "| 情境 | 要測的行為 | " + " | ".join(modes) + " |", "|---|---|" + "---|" * len(modes)]
    for sc in SCENARIOS:
        cells = []
        for mode in modes:
            rs = by.get((sc["id"], mode), [])
            if not rs:
                cells.append("—")
                continue
            ok = sum(1 for r in rs if r.get("passed"))
            fails = sorted({f"第{i + 1}句:{k}" for r in rs for i, t in enumerate(r.get("turns", []))
                            for k, v in t.get("checks", {}).items() if not v})
            err = "；執行錯誤" if any(r.get("error") for r in rs) else ""
            cells.append(f"{ok}/{len(rs)}（{_avg([r.get('seconds') for r in rs])} 秒）" + (f" ✗ {'、'.join(fails)}" if fails else "") + err)
        lines.append(f"| {sc['id']} | {sc['desc']} | " + " | ".join(cells) + " |")

    lines += ["", "## 每一次的最後回答與工具呼叫", ""]
    for r in results:
        lines.append(f"### {r['scenario']} · {r['mode']} · {'✅' if r.get('passed') else '❌'}")
        if r.get("error"):
            lines += ["```", r["error"][-1500:], "```"]
        for i, t in enumerate(r.get("turns", []), 1):
            calls = "、".join(f"{c['skill'] or c['script']}({' '.join(c['args'])[:60]})" for c in t["tool_calls"]) or "（沒有）"
            bad = [k for k, v in t["checks"].items() if not v]
            lines.append(f"- 第 {i} 句「{t['user']}」→ 工具：{calls}" + (f"；未通過：{'、'.join(bad)}" if bad else ""))
            lines.append(f"  - 回答：{(t['final_reply'] or '（空白）')[:300]}")
        lines.append("")
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--modes", default="harness,claude_code")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", default="", help="只跑這些情境（逗號分隔）")
    ap.add_argument("--model", default=os.environ.get("EVAL_MODEL", "gemma4:e4b"))
    ap.add_argument("--fake", action="store_true", help="假模型，只驗證評測流程")
    ap.add_argument("--timeout", type=int, default=900, help="單一情境的逾時秒數")
    ap.add_argument("--keep", action="store_true", help="保留每一次的臨時專案副本（除錯用）")
    ap.add_argument("--out", default=os.path.join(HERE, "results"))
    args = ap.parse_args()
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    scenarios = [s for s in SCENARIOS if not only or s["id"] in only]
    out_dir = os.path.join(args.out, time.strftime("%Y%m%d_%H%M%S") + ("_fake" if args.fake else ""))
    os.makedirs(out_dir, exist_ok=True)
    total = len(modes) * len(scenarios) * args.runs
    results, n = [], 0
    for run in range(args.runs):
        for mode in modes:
            for sc in scenarios:
                n += 1
                r = run_one(sc["id"], mode, args)
                r["run"] = run + 1
                results.append(r)
                with open(os.path.join(out_dir, "runs.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                bad = [f"{k}" for t in r.get("turns", []) for k, v in t.get("checks", {}).items() if not v]
                print(f"[{n}/{total}] {mode:<11} {sc['id']:<22} {'PASS' if r.get('passed') else 'FAIL'} "
                      f"{r.get('wall_seconds', 0):>6.1f}s" + (f"  未通過：{','.join(bad)}" if bad else "")
                      + (f"  錯誤：{r['error'][-200:]}" if r.get("error") else ""), flush=True)
                write_report(results, modes, out_dir, args)
    print(f"\n報表：{os.path.join(out_dir, 'report.md')}")


if __name__ == "__main__":
    main()
