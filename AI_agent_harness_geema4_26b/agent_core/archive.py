"""ArchiveMixin：工具回傳的原文存檔（logs/tool_results/<session>_<編號>_<腳本>.md）與 index.md。"""
import os
import re
import time
from skills_system.scripts import _results_common
from .config import (
    TOOL_RESULT_FILE_RE,
    TOOL_RESULTS_DIRNAME,
    TOOL_RESULTS_INDEX,
    TOOL_RESULTS_KEEP,
    TOOL_RESULTS_MAX_MB,
)


class ArchiveMixin:

    # ---------------------------------------------------------------- 📄 工具結果存檔
    def results_dir(self):
        return os.path.join(self.script_dir, "logs", TOOL_RESULTS_DIRNAME)

    def _max_archived_id(self):
        """既有存檔裡最大的編號（沒有存檔就 0）：__init__ 用它當 trajectory_seq 的起點，讓編號跨 session 不重複。"""
        return max((f[1] for f in _results_common.list_result_files(self.results_dir())), default=0)

    def _archive_tool_result(self, record, output_text):
        """把一次腳本執行的完整原始輸出寫成 logs/tool_results/<session>_<id>_<腳本>.md，並在 index.md 追加一行。
        檔頭是簡單的 key: value（宿主機不一定有 PyYAML）。寫不進去不影響主流程（回傳 None）。"""
        try:
            d = self.results_dir()
            os.makedirs(d, exist_ok=True)
            stem = re.sub(r"[^A-Za-z0-9_-]", "_", re.sub(r"(_cmd)?\.py$", "", record["script"]))
            filename = f"{self.session_id}_{record['id']:03d}_{stem}.md"
            task = " ".join((record.get("task") or "").split())
            header = [
                "---", f"id: {record['id']}", f"session: {self.session_id}", f"ts: {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"script: {record['script']}", f"skill: {record.get('skill') or ''}", f"command: {record['command']}",
                f"cwd: {record['cwd']}", f"container: {record.get('target_container') or ''}", f"status: {record['status']}",
                f"chars: {len(output_text)}", f"task: {task[:200]}", "---",
            ]
            with open(os.path.join(d, filename), "w", encoding="utf-8") as f:
                f.write("\n".join(header) + "\n" + output_text + ("\n" if not output_text.endswith("\n") else ""))
            line = (f"#{record['id']} | {time.strftime('%Y-%m-%d %H:%M')} | {record['script']} | {record['status']} | "
                    f"{len(output_text)} 字 | {filename} | 任務：{task[:60]} | 回答：")
            with open(os.path.join(d, TOOL_RESULTS_INDEX), "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._prune_tool_results(d)
            return filename
        except OSError:
            return None

    def _prune_tool_results(self, d):
        """只留最近 TOOL_RESULTS_KEEP 個檔、總大小不超過 TOOL_RESULTS_MAX_MB；刪最舊的並把 index.md 裡對應的行拿掉。"""
        names = sorted(fn for fn in os.listdir(d) if TOOL_RESULT_FILE_RE.match(fn))   # 檔名＝session_id + 三位數編號，排序即時間順序
        sizes = {fn: os.path.getsize(os.path.join(d, fn)) for fn in names}
        total, removed = sum(sizes.values()), []
        while names and (len(names) > TOOL_RESULTS_KEEP or total > TOOL_RESULTS_MAX_MB * 1024 * 1024):
            fn = names.pop(0)
            total -= sizes[fn]
            os.remove(os.path.join(d, fn))
            removed.append(fn)
        if removed:
            index = os.path.join(d, TOOL_RESULTS_INDEX)
            if os.path.exists(index):
                with open(index, encoding="utf-8") as f:
                    lines = [ln for ln in f.read().splitlines() if not any(f"| {fn} |" in ln for fn in removed)]
                with open(index, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + ("\n" if lines else ""))
        return removed

    def update_result_answer(self, result_id, answer):
        """摘要 session 產出「回答」後回填到 index.md 該筆的「回答：」欄，result_list 一眼就能看到每個存檔在講什麼。"""
        if not result_id or not answer:
            return False
        try:
            index = os.path.join(self.results_dir(), TOOL_RESULTS_INDEX)
            with open(index, encoding="utf-8") as f:
                lines = f.read().splitlines()
            key = f"#{result_id} | "
            for i, ln in enumerate(lines):
                if ln.startswith(key) and f"| {self.session_id}_" in ln:
                    lines[i] = ln.rsplit("| 回答：", 1)[0] + "| 回答：" + " ".join(answer.split())[:120]
                    break
            else:
                return False
            with open(index, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            return True
        except OSError:
            return False

    def result_file_path(self, ref):
        """'16'／'#16'（優先目前 session）、'latest'、'index' 或檔名 → 完整路徑；找不到回 None。Web 的 /api/results/<ref> 用。"""
        d = self.results_dir()
        if not os.path.isdir(d):
            return None
        ref = str(ref or "").strip()
        if ref == "index":
            p = os.path.join(d, TOOL_RESULTS_INDEX)
            return p if os.path.exists(p) else None
        names = sorted(fn for fn in os.listdir(d) if TOOL_RESULT_FILE_RE.match(fn))
        if ref.lstrip("#").isdigit():
            rid = int(ref.lstrip("#"))
            cands = [fn for fn in names if fn.split("_")[2] == f"{rid:03d}"]
            mine = [fn for fn in cands if fn.startswith(self.session_id + "_")]
            chosen = (mine or cands)
            return os.path.join(d, chosen[-1]) if chosen else None
        if ref in ("latest", "last"):
            return os.path.join(d, names[-1]) if names else None
        base = os.path.basename(ref)
        return os.path.join(d, base) if base in names else None
