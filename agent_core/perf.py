"""PerfMixin：每次模型呼叫的耗時記錄（logs/perf.jsonl）與包一層 ollama.chat 的 _timed_chat。

為什麼要記：Ollama 回報的 prompt_eval_count 在 KV cache 命中時仍是完整的 prompt 長度（已實測），看不出 cache
有沒有命中；prompt_eval_duration 才看得出來——命中時只算新增的那一段，毫秒數會小很多。主對話的每一筆另外記
「system prompt 跟上一次主對話呼叫比有沒有變」與「中間跑了幾次獨立 session」（同一個模型只有一個 slot 時，
獨立 session 會把主對話的 cache 洗掉），evals/perf_report.py 依這兩個欄位分組比較。"""
import json
import os
import time
import ollama
from .config import PERF_LOG, PERF_LOG_ENABLED


def _ms(ns):
    try:
        return round(int(ns) / 1e6, 1)
    except (TypeError, ValueError):
        return None


class PerfMixin:

    def _timed_chat(self, kind, **kwargs):
        """獨立 session（tool_summary、recall、compress、skill_draft）呼叫模型都走這裡：照常呼叫 ollama.chat，
        再把 Ollama 回報的計量記進 perf.jsonl。主對話在 _chat_once 另外記（要多記 system prompt 有沒有變）。"""
        t0 = time.time()
        res = ollama.chat(**kwargs)
        self._perf_record(kind, res, kwargs.get("model"), wall_ms=round((time.time() - t0) * 1000, 1))
        self._side_calls_since_main += 1
        return res

    def _perf_record(self, kind, response, model, wall_ms=None, **extra):
        get = getattr(response, "get", None)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "session": self.session_id, "kind": kind, "model": model,
            "context_mode": getattr(self, "context_mode", None),
            "prompt_tokens": get("prompt_eval_count") if get else None,
            "prompt_ms": _ms(get("prompt_eval_duration")) if get else None,
            "eval_tokens": get("eval_count") if get else None,
            "eval_ms": _ms(get("eval_duration")) if get else None,
            "load_ms": _ms(get("load_duration")) if get else None,
            "total_ms": _ms(get("total_duration")) if get else None,
            "wall_ms": wall_ms, **extra,
        }
        self.last_perf = rec
        self.perf_calls.append(rec)
        del self.perf_calls[:-200]   # 只留最近 200 筆在記憶體（評測與 UI 用）；完整紀錄在 perf.jsonl
        if not PERF_LOG_ENABLED:
            return
        try:
            log_dir = os.path.join(self.script_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, PERF_LOG), "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError):
            pass
