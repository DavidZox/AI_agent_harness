"""模型呼叫耗時分析：看 KV cache 在不同情況下實際省了多少。

    python3 evals/perf_report.py                              # 讀 logs/perf.jsonl（平常使用 CLI／Web 時累積的）
    python3 evals/perf_report.py evals/results/<時間>/runs.jsonl   # 讀評測結果裡每一次的呼叫紀錄

Ollama 回報的 prompt_eval_count 在 KV cache 命中時仍是完整長度，看不出有沒有命中；prompt_eval_duration（prompt_ms）
才看得出來：命中時只算新增的那一段。主對話的呼叫依「呼叫前發生了什麼」分組：
- 穩定：system prompt 沒變、歷史沒被改寫、中間沒跑獨立 session → cache 應該從頭命中到上一則訊息
- 跑過獨立 session：同一個模型只有一個 slot 時，獨立 session 會把主對話的 cache 洗掉 → 整段重算
- system prompt 變了：壓縮、記憶或技能索引改變、檢索清單多一筆 → 從變的地方開始重算
- 歷史被改寫：claude_code 模式清除舊回傳、或壓縮刪掉舊訊息 → 從被改的那一則開始重算
比較各組「每 1000 個 prompt token 花幾毫秒」，就知道 cache 失效的實際代價。"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)


def load(path):
    recs = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            d = json.loads(ln)
            if "perf" in d and isinstance(d["perf"], list):   # 評測的 runs.jsonl：每一次都帶自己的呼叫紀錄
                recs += [dict(p, context_mode=p.get("context_mode") or d.get("mode")) for p in d["perf"]]
            elif "kind" in d:
                recs.append(d)
    return recs


def group_of(p):
    if p.get("system_changed") is None:
        return "第一次呼叫（沒有 cache）"
    if p.get("side_calls_before"):
        return "跑過獨立 session 之後"
    if p.get("history_rewritten"):
        return "歷史被改寫之後（清除／壓縮）"
    if p.get("system_changed"):
        return "system prompt 變了"
    return "穩定（應該命中 cache）"


def row(name, ps):
    ps = [p for p in ps if p.get("prompt_tokens") and p.get("prompt_ms") is not None]
    if not ps:
        return f"| {name} | 0 | | | |"
    tok = sum(p["prompt_tokens"] for p in ps)
    ms = sum(p["prompt_ms"] for p in ps)
    return f"| {name} | {len(ps)} | {round(tok / len(ps))} | {round(ms / len(ps))} | {round(ms / tok * 1000, 1)} |"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(PROJECT, "logs", "perf.jsonl")
    if not os.path.exists(path):
        print(f"找不到 {path}：先用 CLI／Web 跑幾輪（AGENT_PERF_LOG 預設開啟），或指定評測結果的 runs.jsonl。")
        return
    recs = load(path)
    print(f"# 模型呼叫耗時（{path}，共 {len(recs)} 筆）\n")
    for mode in sorted({p.get("context_mode") or "?" for p in recs}):
        mine = [p for p in recs if (p.get("context_mode") or "?") == mode]
        main_calls = [p for p in mine if p["kind"] == "main"]
        print(f"## 上下文模式：{mode}\n")
        print("主對話呼叫，依呼叫前發生的事分組：\n")
        print("| 分組 | 次數 | 平均 prompt tokens | 平均 prompt 毫秒 | 每 1000 tokens 毫秒 |")
        print("|---|---|---|---|---|")
        for g in ("穩定（應該命中 cache）", "system prompt 變了", "歷史被改寫之後（清除／壓縮）", "跑過獨立 session 之後", "第一次呼叫（沒有 cache）"):
            print(row(g, [p for p in main_calls if group_of(p) == g]))
        side = [p for p in mine if p["kind"] != "main"]
        if side:
            print("\n獨立 session 呼叫：\n")
            print("| 種類 | 次數 | 平均 prompt tokens | 平均 prompt 毫秒 | 每 1000 tokens 毫秒 |")
            print("|---|---|---|---|---|")
            for k in sorted({p["kind"] for p in side}):
                print(row(k, [p for p in side if p["kind"] == k]))
        print()
    print("讀法：「穩定」那一列每 1000 tokens 的毫秒數明顯比其他列小，代表 cache 有命中；差距就是失效的代價。")


if __name__ == "__main__":
    main()
