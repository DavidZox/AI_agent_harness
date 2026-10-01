"""TrajectoryMixin：操作軌跡（logs/trajectory.jsonl）與對話紀錄（transcript）——run_tool 每執行一支腳本記一筆並觸發
原文存檔；每則對話訊息留一份副本。/trajectory 列軌跡，/make_skill 從這兩份取「整段對話＋工具回傳」。"""
import json
import os
import re
import time
from .config import TRAJECTORY_LOG, TRAJECTORY_OUTPUT_HEAD
from .protocol import is_procedure_doc


class TrajectoryMixin:


    # =========================================================
    # 🧩 操作軌跡、對話紀錄與 make_skill（把一段做對的對話整理成流程技能的規格）
    # 分工：harness 記錄軌跡與對話、挑範圍、驗證模型寫的規格（引用的技能與腳本都要真的存在）、用範本排版、寫入索引；
    # 模型讀整段對話推論出規格的內容；使用者在預覽後核准。不產生任何腳本：步驟只呼叫既有技能的腳本。
    # =========================================================
    def _script_skill_map(self):
        """scripts/<x>.py -> 技能名稱。先以「<技能>.md 提到 scripts/<技能>_cmd.py」為準，
        其餘出現過的腳本再補上；每次重建（十幾個小檔案，成本可忽略），技能新增後不需重啟。
        流程技能（type: Procedure）的規格引用的是別的技能的腳本，不算數——不然它的名稱排在前面時，
        ls_cmd.py 這類腳本會被誤認成屬於它。"""
        mapping, mentions = {}, {}
        if not os.path.isdir(self.tools_dir):
            return mapping
        for fname in sorted(os.listdir(self.tools_dir)):
            if not fname.endswith(".md"):
                continue
            skill = fname[:-3]
            try:
                with open(os.path.join(self.tools_dir, fname), "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            if is_procedure_doc(text):
                continue
            scripts = set(re.findall(r"scripts/([A-Za-z0-9_]+\.py)", text))
            if f"{skill}_cmd.py" in scripts:
                mapping[f"{skill}_cmd.py"] = skill
            for s in scripts:
                mentions.setdefault(s, skill)
        for s, skill in mentions.items():
            mapping.setdefault(s, skill)
        return mapping

    def _next_id(self):
        """配一個新的全域編號（軌跡、工具結果存檔、被壓縮的對話片段存檔共用同一個序列，跨 session 不重複）。
        背景壓縮執行緒也會配編號（對話片段存檔），所以要上鎖。"""
        with self._id_lock:
            self.trajectory_seq += 1
            return self.trajectory_seq

    def _record_trajectory(self, script_name, args, output_text, cwd, container_cwd, target_container=""):
        """run_tool 每執行一支腳本（成功、失敗、逾時都算；找不到腳本的猜測不算）記一筆。
"""
        status = "ERROR" if output_text.lstrip().startswith("[ERROR]") else "PASS"
        self._sync_transcript()   # 下這個 action 的 AI 回覆已經在 messages 裡；工具回傳接著會落在 transcript_pos
        record = {
            "id": self._next_id(),
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
            "task": self.current_task or "",
            "plan_active": bool(self.current_plan),
            "transcript_pos": len(self.transcript),
        }
        record["result_file"] = self._archive_tool_result(record, output_text)
        self.last_result_id = record["id"] if record["result_file"] else None
        self.last_result_file = record["result_file"]
        self.trajectory.append(record)
        self._append_trajectory_log(record)
        self.plan_on_exec(record)   # /plan 執行中：這一步的技能成功就換下一步
        return record

    def add_trajectory_boundary(self, reason, **extra):
        """在軌跡記一個起點（/clear、計畫核准、make_skill 完成）；連續的起點只留一個。對話紀錄的起點每次都記
        （_mark_transcript_start），就算還沒執行過任何腳本。"""
        self._mark_transcript_start(reason, extra.get("plan"))
        if not self.trajectory:
            return None  # 什麼都還沒執行（例如啟動時的 reset_conversation），不需要起點
        if self.trajectory[-1]["kind"] == "boundary" and not extra:
            return None  # 連續的一般起點只留一個；帶資料的起點（計畫核准、make_skill）一律記
        record = {"id": self._next_id(), "kind": "boundary", "ts": time.strftime("%m-%d %H:%M:%S"),
                  "reason": reason, **extra}
        self.trajectory.append(record)
        self._append_trajectory_log(record)
        return record

    # ---------------------------------------------------------------- 📜 對話紀錄（transcript）
    def _sync_transcript(self):
        """把 messages 裡還沒記過的訊息（system 除外）照順序抄進 transcript。訊息會被壓縮刪掉、被 /clear 清掉、
        被 claude_code 清成佔位，所以在這些事發生之前呼叫：每次呼叫主模型前（ask_ai）、記軌跡時、壓縮刪除前、
        /clear 前、/make_skill 前。只抄當下的內容（字串不會變），之後清成佔位也不影響這裡的副本。"""
        with self.messages_lock:
            for m in self.messages:
                if m.get("role") == "system" or id(m) in self._transcript_ids:
                    continue
                self._transcript_ids.add(id(m))
                self._transcript_refs.append(m)
                self.transcript.append({"role": m.get("role"), "content": m.get("content") or ""})

    def _transcript_replaced(self, old, new):
        """claude_code 把舊的工具回傳換成佔位時會換成新的 dict：新的那個不是新訊息，標成已記過（原文的副本已經在 transcript）。"""
        if id(old) not in self._transcript_ids:
            self._sync_transcript()
        self._transcript_ids.add(id(new))
        self._transcript_refs.append(new)

    def _mark_transcript_start(self, reason, plan=None):
        """記一個對話紀錄的起點。計畫核准的起點落在剛加入的那則任務訊息（它就是這段工作的開頭），並記下計畫內容
        （計畫的步驟在動態區、不在 messages 裡，/make_skill 要另外交給草擬 session）；/clear 與 make_skill 落在目前的尾端。"""
        self._sync_transcript()
        pos = len(self.transcript)
        if reason == "plan_confirmed":
            pos = next((i for i in range(len(self.transcript) - 1, -1, -1) if self._is_user_turn(self.transcript[i])), pos)
        self.transcript_starts.append({"reason": reason, "pos": pos, "ts": time.strftime("%m-%d %H:%M:%S"),
                                       "plan": plan or ""})

    def _is_user_turn(self, entry):
        """transcript 的這一則是不是使用者自己說的話（工具回傳、規劃流程的系統訊息不算）。"""
        return entry.get("role") == "user" and self._user_text_of(entry) is not None

    def transcript_scope(self, spec=None):
        """/make_skill 要讀的範圍：回傳 (start, end, steps, error)。transcript[start:end] 是對話紀錄的那一段，steps 是
        落在這段裡的操作軌跡。spec 省略＝上一個起點（/clear、計畫核准、上一次 make_skill）之後；'all'＝整個 session；
        '3-7'、'3,5,8'＝這幾個軌跡步驟，對話從第一步之前使用者的那句話開始、到最後一步之後使用者的下一句話之前。"""
        self._sync_transcript()
        spec_l = (spec or "").strip().lower()
        execs = [r for r in self.trajectory if r["kind"] == "exec"]
        if spec_l in ("", "recent", "last"):
            start, end = self.transcript_starts[-1]["pos"], len(self.transcript)
            steps = [r for r in execs if start <= r.get("transcript_pos", -1) < end]
        elif spec_l == "all":
            start, end, steps = 0, len(self.transcript), execs
        else:
            steps, err = self.trajectory_steps(spec)
            if err:
                return 0, 0, [], err
            positions = [r["transcript_pos"] for r in steps if "transcript_pos" in r]
            if not positions:
                return 0, 0, [], "這幾個步驟沒有對應的對話紀錄。"
            first, last = min(positions), max(positions)
            start = next((i for i in range(min(first, len(self.transcript)) - 1, -1, -1)
                          if self._is_user_turn(self.transcript[i])), 0)
            end = next((i for i in range(last, len(self.transcript)) if self._is_user_turn(self.transcript[i])),
                       len(self.transcript))
        if not any(self._is_user_turn(e) for e in self.transcript[start:end]):
            return 0, 0, [], ("自上一個起點（/clear、計畫核准或上一次 make_skill）之後還沒有對話；要用更早的對話請指定"
                              "軌跡編號範圍（例如 3-7）或 all，/trajectory 可查編號。")
        return start, end, steps, None

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
        lines = ["🧭 操作軌跡（✅ 成功 / ❌ 失敗；/make_skill 預設讀最後一個起點之後的對話與步驟）："]
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
