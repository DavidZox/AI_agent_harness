"""TrajectoryMixin：操作軌跡（logs/trajectory.jsonl）——run_tool 每執行一支腳本記一筆並觸發原文存檔；/trajectory 與 /make_skill 從這裡取步驟。"""
import json
import os
import re
import time
from .config import TRAJECTORY_LOG, TRAJECTORY_OUTPUT_HEAD


class TrajectoryMixin:


    # =========================================================
    # 🧩 操作軌跡記錄與 make_skill（把做對的步驟編譯成組合技能）
    # 分工：harness 記錄軌跡、挑步驟、驗證模型填的表單、用範本產生檔案、寫入索引；
    # 模型只填一份 JSON（標題、描述、分類、參數化、每步目的、注意事項）；使用者在預覽後核准。
    # 模型不寫任何 Python：產生的腳本只是資料，執行邏輯在 scripts/_composite.py。
    # =========================================================
    def _script_skill_map(self):
        """scripts/<x>.py -> 技能名稱。先以「<技能>.md 提到 scripts/<技能>_cmd.py」為準，
        其餘出現過的腳本再補上；每次重建（十幾個小檔案，成本可忽略），技能新增後不需重啟。"""
        mapping, mentions = {}, {}
        if not os.path.isdir(self.tools_dir):
            return mapping
        for fname in sorted(os.listdir(self.tools_dir)):
            if not fname.endswith(".md"):
                continue
            skill = fname[:-3]
            try:
                with open(os.path.join(self.tools_dir, fname), "r", encoding="utf-8") as f:
                    scripts = set(re.findall(r"scripts/([A-Za-z0-9_]+\.py)", f.read()))
            except OSError:
                continue
            if f"{skill}_cmd.py" in scripts:
                mapping[f"{skill}_cmd.py"] = skill
            for s in scripts:
                mentions.setdefault(s, skill)
        for s, skill in mentions.items():
            mapping.setdefault(s, skill)
        return mapping

    def _record_trajectory(self, script_name, args, output_text, cwd, container_cwd, target_container=""):
        """run_tool 每執行一支腳本（成功、失敗、逾時都算；找不到腳本的猜測不算）記一筆。"""
        status = "ERROR" if output_text.lstrip().startswith("[ERROR]") else "PASS"
        self.trajectory_seq += 1
        record = {
            "id": self.trajectory_seq,
            "kind": "exec",
            "ts": time.strftime("%m-%d %H:%M:%S"),
            "script": script_name,
            "skill": self._script_skill_map().get(script_name),
            "args": list(args),
            "command": self._format_execute(script_name, args),
            "cwd": cwd,
            "container_cwd": container_cwd,
            "target_container": target_container,
            "status": status,
            "output_head": output_text[:TRAJECTORY_OUTPUT_HEAD],
            "task": (self.current_task or "")[:200],
            "plan_active": bool(self.current_plan),
        }
        record["result_file"] = self._archive_tool_result(record, output_text)
        self.last_result_id = record["id"] if record["result_file"] else None
        self.last_result_file = record["result_file"]
        self.trajectory.append(record)
        self._append_trajectory_log(record)
        return record

    def add_trajectory_boundary(self, reason, **extra):
        """在軌跡記一個起點（/clear、計畫核准、make_skill 完成）；連續的起點只留一個。"""
        if not self.trajectory:
            return None  # 什麼都還沒執行（例如啟動時的 reset_conversation），不需要起點
        if self.trajectory[-1]["kind"] == "boundary" and not extra:
            return None  # 連續的一般起點只留一個；帶資料的起點（計畫核准、make_skill）一律記
        self.trajectory_seq += 1
        record = {"id": self.trajectory_seq, "kind": "boundary", "ts": time.strftime("%m-%d %H:%M:%S"),
                  "reason": reason, **extra}
        self.trajectory.append(record)
        self._append_trajectory_log(record)
        return record

    def _append_trajectory_log(self, record):
        """追加到 logs/trajectory.jsonl（跨 session 的稽核記錄；寫不進去不影響主流程）。"""
        try:
            log_dir = os.path.join(self.script_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            with open(os.path.join(log_dir, TRAJECTORY_LOG), "a", encoding="utf-8") as f:
                f.write(json.dumps({"session": self.session_id, **record}, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError):
            pass

    @staticmethod
    def _quote_arg(arg):
        """顯示用：含空白／引號的參數以雙引號包住（與 AGENT.md 範例一致，shlex 可還原）。"""
        if arg == "" or any(c.isspace() for c in arg) or '"' in arg or "'" in arg:
            return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return arg

    def _format_execute(self, script_name, args):
        tail = " ".join(self._quote_arg(a) for a in args)
        return f"EXECUTE: scripts/{script_name}" + (f" {tail}" if tail else "")

    def trajectory_steps(self, spec=None):
        """挑出要編譯的步驟，回傳 (steps, error)。
        spec 省略＝上一個起點之後的全部步驟；'all'＝整個 session；'3-7'、'3,5,8'、'3-5,9'＝依編號。"""
        execs = [r for r in self.trajectory if r["kind"] == "exec"]
        if not execs:
            return [], "本次 session 還沒有執行過任何腳本，沒有可編譯的軌跡。"
        spec = (spec or "").strip().lower()
        if spec in ("", "recent", "last"):
            last_boundary = max((i for i, r in enumerate(self.trajectory) if r["kind"] == "boundary"), default=-1)
            steps = [r for r in self.trajectory[last_boundary + 1:] if r["kind"] == "exec"]
            if not steps:
                return [], ("自上一個起點（/clear、計畫核准或上一次 make_skill）之後沒有執行過腳本；"
                            "要用更早的步驟請指定編號範圍（例如 3-7）或 all，/trajectory 可查編號。")
            return steps, None
        if spec == "all":
            return execs, None
        wanted = set()
        for part in spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                lo, _, hi = part.partition("-")
                if not (lo.strip().isdigit() and hi.strip().isdigit()):
                    return [], f"步驟範圍格式錯誤：{part}（可用 3-7、3,5,8 或 all）"
                lo, hi = sorted((int(lo), int(hi)))
                wanted.update(range(lo, hi + 1))
            elif part.isdigit():
                wanted.add(int(part))
            else:
                return [], f"步驟範圍格式錯誤：{part}（可用 3-7、3,5,8 或 all）"
        steps = [r for r in execs if r["id"] in wanted]
        if not steps:
            return [], f"編號 {spec} 沒有對應到任何已執行的腳本步驟，輸入 /trajectory 查看編號。"
        return steps, None

    def format_trajectory(self):
        """/trajectory：列出本次 session 的軌跡（編號、成功／失敗、指令、所屬技能、起點）。"""
        if not self.trajectory:
            return "本次 session 尚未記錄任何腳本執行。"
        labels = {"clear": "/clear", "plan_confirmed": "計畫核准", "make_skill": "make_skill"}
        lines = ["🧭 操作軌跡（✅ 成功 / ❌ 失敗；/make_skill 預設取最後一個起點之後的步驟）："]
        for r in self.trajectory:
            if r["kind"] == "boundary":
                label = labels.get(r["reason"], r["reason"])
                if r.get("name"):
                    label += f" {r['name']}"
                lines.append(f"── 起點 #{r['id']}：{label}（{r['ts']}）──")
            else:
                mark = "✅" if r["status"] == "PASS" else "❌"
                skill = f"（{r['skill']}）" if r.get("skill") else ""
                lines.append(f"#{r['id']} {mark} {r['command']}{skill}")
        lines.append("用法：/make_skill <技能名稱> [3-7 | 3,5,8 | all]")
        return "\n".join(lines)

    @staticmethod
    def _group_trajectory(steps):
        """成功步驟各自帶著它之前的失敗嘗試（同一個意圖下的修正歷程）；最後仍未修正的失敗另外回傳。"""
        groups, pending = [], []
        for r in steps:
            if r["status"] == "PASS":
                groups.append({"step": r, "failed_before": pending})
                pending = []
            else:
                pending.append(r)
        return groups, pending

    def _plan_for_steps(self, steps):
        """這批步驟所屬的已核准計畫：最後一個步驟之前最近的計畫核准起點；沒有就用目前的 current_plan。"""
        last_id = steps[-1]["id"]
        for r in reversed(self.trajectory):
            if r["id"] < last_id and r["kind"] == "boundary" and r.get("reason") == "plan_confirmed" and r.get("plan"):
                return r["plan"]
        return self.current_plan
