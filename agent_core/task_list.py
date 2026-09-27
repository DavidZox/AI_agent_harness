"""TaskListMixin：任務清單（plan_task 偽技能）——模型自己開的步驟清單，不需要使用者核准；/plan on 核准的計畫
也轉成同一份清單（confirm_plan）。清單每次呼叫模型都附在送出內容的最尾端（todo_block，見 PromptMixin.build_state_tail），
模型每一輪都看得到「原本要做什麼、做到第幾步」，不會被滾動摘要沖掉。

進度由誰維護依上下文模式而定：
- harness：程式依執行紀錄自動打勾（todo_auto_tick）——步驟文字裡寫的技能執行成功就算完成，失敗記一次嘗試。
- claude_code：模型自己用 plan_task done <n> 勾（跟 Claude Code 的 TodoWrite 一樣），太多個動作沒更新時提醒一句。"""
import re
import shlex
from .config import DERIVED_RESULT_SCRIPTS, TODO_ITEM_MAX_CHARS, TODO_MAX_ITEMS, TODO_REMIND_AFTER


_MARKS = {"pending": "[ ]", "done": "[✓]", "failed": "[✗]"}
_NUMBERED = re.compile(r"(?:^|\s)\d{1,2}[.、)）]\s*")
_USAGE = ('[ERROR] 用法：plan_task set "<步驟1>" "<步驟2>" …（每步寫「技能：要做什麼；失敗→怎麼辦」）｜'
          'add "<步驟>"｜done <編號>[,<編號>]｜fail <編號> "<原因>"｜clear｜show')


def split_steps(text):
    """一段文字 → 步驟清單：依換行或全形「｜」分開；只有一段但含「1. 2. 3.」時依編號分開；去掉每步開頭的編號。
    不用「；」分：每一步的寫法本來就是「技能：要做什麼；失敗→怎麼辦」。也不用半形 |（grep 的同義詞會用到）。"""
    pieces = [p.strip() for p in re.split(r"[\n｜]+", str(text or "")) if p.strip()]
    if len(pieces) == 1 and len(_NUMBERED.findall(pieces[0])) >= 2:
        pieces = [p.strip() for p in _NUMBERED.split(pieces[0]) if p.strip()]
    return [re.sub(r"^\s*(?:\d{1,2}[.、)）]|[-*•])\s*", "", p).strip() for p in pieces if p.strip()]


def plan_lines(plan_text):
    """/plan on 核准的計畫（模型寫在 reply 的條列文字）→ 步驟清單：抓「1. …」「2) …」開頭的行；抓不到就整段當一步。"""
    items = [m.group(1).strip() for m in re.finditer(r"^\s*\d{1,2}[.、)）]\s*(.+)$", str(plan_text or ""), re.M)]
    if not items and str(plan_text or "").strip():
        items = [" ".join(str(plan_text).split())]
    return items


def _numbers(tokens):
    out = []
    for tok in tokens:
        for part in re.split(r"[,，、\s]+", str(tok)):
            if part.strip().lstrip("#").isdigit():
                out.append(int(part.strip().lstrip("#")))
    return out


class TaskListMixin:

    # ---------------------------------------------------------------- 狀態
    def _todo_set(self, items, source):
        items = [" ".join(str(x).split())[:TODO_ITEM_MAX_CHARS] for x in items if str(x).strip()][:TODO_MAX_ITEMS]
        self.todo = [{"text": t, "status": "pending", "note": ""} for t in items]
        self.todo_source = source if self.todo else None
        self.todo_updated_seq = self.trajectory_seq

    def clear_todo(self):
        self.todo, self.todo_source = [], None

    def todo_all_done(self):
        return bool(self.todo) and all(it["status"] != "pending" for it in self.todo)

    def todo_progress(self):
        """(完成數, 總數)；沒有清單回 (0, 0)。UI 狀態列用。"""
        return sum(1 for it in self.todo if it["status"] == "done"), len(self.todo)

    def _todo_text(self):
        current = next((i for i, it in enumerate(self.todo) if it["status"] == "pending"), None)
        lines = []
        for i, it in enumerate(self.todo):
            mark = "[→]" if i == current else _MARKS.get(it["status"], "[ ]")
            note = f"（{it['note']}）" if it["note"] else ""
            lines.append(f"{i + 1}. {mark} {it['text']}{note}")
        return "\n".join(lines)

    def _execs_since_todo_update(self):
        return sum(1 for r in self.trajectory
                   if r.get("kind") == "exec" and r["id"] > self.todo_updated_seq
                   and r.get("script") not in DERIVED_RESULT_SCRIPTS)

    # ---------------------------------------------------------------- plan_task 偽技能
    def _plan_task_command(self, remainder):
        """scripts/plan_task_cmd.py <動作> …（沒有實體腳本，dispatch 在存在性檢查前攔截，跟 result_recall 一樣）。
        set／add／done／fail／clear／show；回傳 [PASS]／[ERROR] 開頭的文字給模型。不記軌跡、不存檔（它不是對外的動作）。"""
        try:
            parts = shlex.split(remainder) if remainder.strip() else []
        except ValueError:
            parts = remainder.split()
        verb, args = (parts[0].lower(), parts[1:]) if parts else ("show", [])

        if verb in ("set", "new"):
            items = [s for a in args for s in split_steps(a)]
            if not items:
                return _USAGE
            self._todo_set(items, "self")
            follow = ("步驟裡寫的技能執行成功，系統會自動打勾" if self.context_mode == "harness"
                      else "每做完一步用 plan_task done <編號> 勾掉（一次一個 action，勾完再做下一步）")
            return (f"[PASS] 已建立任務清單（{len(self.todo)} 步；你自己規劃的，不需要使用者核准）：\n{self._todo_text()}\n"
                    f"現在開始做第 1 步；{follow}。清單每一輪都會附在最後面，不用重抄。")
        if verb == "add":
            items = [s for a in args for s in split_steps(a)]
            if not items:
                return _USAGE
            room = TODO_MAX_ITEMS - len(self.todo)
            for t in items[:max(0, room)]:
                self.todo.append({"text": " ".join(t.split())[:TODO_ITEM_MAX_CHARS], "status": "pending", "note": ""})
            self.todo_source = self.todo_source or "self"
            self.todo_updated_seq = self.trajectory_seq
            return f"[PASS] 已加入 {min(len(items), max(0, room))} 步：\n{self._todo_text()}"
        if verb in ("done", "fail"):
            if not self.todo:
                return "[ERROR] 目前沒有任務清單：先用 plan_task set 列出步驟。"
            nums = _numbers(args[:1] if verb == "fail" else args)
            bad = [n for n in nums if not 1 <= n <= len(self.todo)]
            if not nums or bad:
                return f"[ERROR] 編號不對（清單共 {len(self.todo)} 步）。{_USAGE[8:]}"
            for n in nums:
                it = self.todo[n - 1]
                it["status"] = "done" if verb == "done" else "failed"
                if verb == "fail":
                    it["note"] = " ".join(" ".join(args[1:]).split())[:80] or "失敗"
            self.todo_updated_seq = self.trajectory_seq
            tail = ("全部步驟都結束了：向使用者回報結果。" if self.todo_all_done()
                    else "繼續做 [→] 那一步。")
            return f"[PASS] 已更新任務清單：\n{self._todo_text()}\n{tail}"
        if verb == "clear":
            self.clear_todo()
            return "[PASS] 已清除任務清單。"
        if verb == "show":
            return "[PASS] " + (f"目前的任務清單：\n{self._todo_text()}" if self.todo else "目前沒有任務清單。")
        return _USAGE

    # ---------------------------------------------------------------- harness 模式：依執行紀錄自動打勾
    def todo_auto_tick(self, record):
        """_record_trajectory 每記一筆真正的腳本執行就呼叫。harness 模式下，第一個還沒完成、而且文字裡提到這次執行
        的技能（或腳本名稱）的步驟：成功 → 打勾；失敗 → 記一次失敗的嘗試（不打叉，模型可能修正參數後重試）。
        claude_code 模式不動（由模型自己勾）；存檔工具（result_*）是回查、不是任務的步驟，不算。"""
        if getattr(self, "context_mode", "harness") != "harness" or not self.todo:
            return
        if record.get("kind") != "exec" or record.get("script") in DERIVED_RESULT_SCRIPTS:
            return
        script = record.get("script") or ""
        names = {n for n in (record.get("skill"), script[:-len("_cmd.py")] if script.endswith("_cmd.py") else script) if n}
        for it in self.todo:
            if it["status"] != "pending" or not any(n in it["text"] for n in names):
                continue
            if record.get("status") == "PASS":
                it["status"] = "done"
                it["note"] = f"#{record['id']}"
            else:
                it["note"] = (it["note"] + "、" if it["note"] else "") + f"#{record['id']} 失敗"
            self.todo_updated_seq = self.trajectory_seq
            break

    # ---------------------------------------------------------------- 顯示（送出內容最尾端的動態區）
    def todo_block(self):
        if not self.todo:
            return ""
        done, total = self.todo_progress()
        who = "使用者核准的計畫" if self.todo_source == "user_plan" else "你自己規劃的清單（不需要使用者核准）"
        if self.context_mode == "harness":
            rule = "步驟裡寫的技能執行成功，系統會自動打勾；[→] 是現在這一步，一次只做一步。"
        else:
            rule = "[→] 是現在這一步，一次只做一步；做完用 plan_task done <編號> 自己勾，做不成用 plan_task fail <編號> \"原因\"。"
        lines = [f"## 任務清單（{who}，{done}/{total} 完成）", self._todo_text(), rule]
        if self.todo_all_done():
            lines.append("全部步驟都結束了：向使用者回報結果；使用者送出下一個新任務時清單會自動清除。")
        elif self.context_mode == "claude_code":
            stale = self._execs_since_todo_update()
            if stale >= TODO_REMIND_AFTER:
                lines.append(f"提醒：清單已經 {stale} 個動作沒有更新；做完的步驟記得勾掉，計畫變了就用 plan_task set 重列。")
        return "\n".join(lines)
