"""GuardMixin：執行前關卡——會改變實體／外部狀態的技能，執行前一律由程式要求使用者確認（CLI 問 y/n、Web 顯示按鈕）。

跟 Plan 模式的核准關卡同一個原則：確認由程式路徑保證，不靠模型記得要先問。判斷在這裡（guard_check），
執行在 DispatchMixin.run_tool：沒有 approved=True 就不執行被擋的技能（CLI／Web 以外的呼叫端也一樣擋得住）。
哪些技能要擋見 config.GUARDED_SKILLS；docker_runcmd 的指令若是唯讀（is_readonly_shell）就放行，
workpackage_send 加 --dry-run 只顯示不送，也放行。組合技能（make_skill）只要有一步是被擋的技能，整支都要確認。"""
import json
import os
import re
import shlex
from .config import GUARD_DENIED_TAG, GUARDED_SKILLS
from .protocol import action_text


_GUARD_REASONS = {
    "workpackage_send": "送出工作包：實體 AMR 會開始移動",
    "workpackage_cancel": "取消工作包：正在執行的任務會停止派下一站",
    "overpending_cancel": "刪除卡在逾時區的任務",
    "docker_est": "建立新的容器",
    "docker_runcmd": "在容器內執行指令：這個指令不在唯讀清單裡，可能會改變容器內的狀態",
}

# ---------------------------------------------------------------- docker_runcmd 的唯讀判斷
# 保守的白名單：每一段（&&、||、;、| 分隔）的指令都在清單裡、沒有寫檔的轉向或指令替換，才算唯讀。
# 判斷不了的一律當作「可能改變狀態」→ 請使用者確認（多問一次的代價，遠小於誤動到實體設備）。
_READONLY_CMDS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "pwd", "echo", "printf", "printenv",
    "ps", "pgrep", "df", "du", "free", "uptime", "which", "whereis", "type", "whoami", "id", "uname",
    "stat", "file", "wc", "cut", "tr", "basename", "dirname", "realpath", "readlink",
    "nproc", "lscpu", "lsusb", "lspci", "lsblk", "column", "jq",
    "diff", "md5sum", "sha256sum", "test", "[", "true", "sleep", "cd", "export",
}
# 看起來唯讀、但某些參數會寫檔或改狀態的指令：帶了這些參數就不算唯讀
_WRITE_FLAGS = {
    "sort": {"-o", "--output"},      # sort -o 檔案 會寫檔
    "tree": {"-o"},                  # tree -o 檔案 會寫檔
    "date": {"-s", "--set"},         # date -s 會改系統時間
}
# 沒有參數時才唯讀：hostname 名稱 會改主機名稱；env 指令… 會執行後面的指令
_NO_ARGS_ONLY = {"hostname", "env"}
# source／. 只放行 ROS／colcon 的環境設定檔（setup.bash 這類）；source 任意腳本等於執行它
_SETUP_FILE = re.compile(r"^(local_)?setup\.(bash|sh|zsh)$")
# ros2 <群組> <動作>：只有查詢類的動作算唯讀（pub、call、set、send_goal、run、launch 都不是）；None＝整個群組都唯讀
_ROS2_READONLY = {
    "topic": {"list", "echo", "info", "hz", "bw", "type", "find", "delay"},
    "node": {"list", "info"},
    "service": {"list", "type", "find"},
    "param": {"list", "get", "describe"},   # dump 在舊版 ROS2 會寫檔，不放行
    "interface": {"list", "show", "package", "packages", "proto"},
    "action": {"list", "info"},
    "pkg": {"list", "prefix", "executables", "xml"},
    "lifecycle": {"get", "list", "nodes"},
    "doctor": None,
    "wtf": None,
}
_FIND_WRITES = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
_HARMLESS_REDIRECT = re.compile(r"\d?>\s*/dev/null|\d?>&\d")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\||\n")


def _segment_readonly(segment):
    try:
        toks = shlex.split(segment)
    except ValueError:
        return False
    while toks and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]):   # VAR=value 前綴只影響這一段的環境
        toks = toks[1:]
    if not toks:
        return True
    cmd = os.path.basename(toks[0])
    if cmd == "timeout":   # timeout [-s 訊號] [-k 秒] 秒數 指令…：檢查後面真正執行的指令
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            i += 2 if toks[i] in ("-s", "-k", "--signal", "--kill-after") else 1
        rest = toks[i + 1:]
        return bool(rest) and _segment_readonly(" ".join(shlex.quote(t) for t in rest))
    if cmd == "ros2":
        group = toks[1] if len(toks) > 1 else ""
        if group not in _ROS2_READONLY:
            return False
        verbs = _ROS2_READONLY[group]
        return verbs is None or (len(toks) > 2 and toks[2] in verbs)
    if cmd == "find":
        return not any(t in _FIND_WRITES for t in toks)
    if cmd == "top":
        return "-b" in toks
    if cmd in ("source", "."):
        return len(toks) == 2 and bool(_SETUP_FILE.match(os.path.basename(toks[1])))
    if cmd in _NO_ARGS_ONLY:
        return len(toks) == 1
    if cmd == "uniq":   # uniq 輸入 輸出 會寫進第二個檔
        return len([t for t in toks[1:] if not t.startswith("-")]) <= 1
    if cmd in _WRITE_FLAGS:
        return not any(t in _WRITE_FLAGS[cmd] or any(t.startswith(f + "=") for f in _WRITE_FLAGS[cmd]) for t in toks[1:])
    return cmd in _READONLY_CMDS


def is_readonly_shell(command):
    """容器內的 shell 指令是否只讀不寫。有 >／>> 轉向（/dev/null 除外）、反引號、$( ) 就不算；
    其餘每一段的第一個字要在白名單裡（ros2 看群組與動作，find 不能帶 -delete／-exec）。"""
    text = str(command or "").strip()
    if not text:
        return False
    text = _HARMLESS_REDIRECT.sub(" ", text)
    if re.search(r"[>`]|\$\(|<\(", text):
        return False
    segments = [s.strip() for s in _SEGMENT_SPLIT.split(text) if s.strip()]
    return bool(segments) and all(_segment_readonly(s) for s in segments)


def _docker_runcmd_command(args):
    """docker_runcmd 的參數 → 真正要在容器內執行的指令（--timeout 與它的值略過）。比照腳本：只有一個位置參數時
    整串是指令；有好幾個時，第一個若像容器名稱（不是白名單裡的指令字）就是容器、其餘接起來是指令，否則全部接起來
    都是指令（模型常忘記引號：docker_runcmd ls -l /opt/ros）。只看最後一個參數會把 ls -l /opt/ros 誤判成 /opt/ros。"""
    positional, i = [], 0
    while i < len(args):
        if args[i] == "--timeout":
            i += 2
            continue
        positional.append(args[i])
        i += 1
    if len(positional) <= 1:
        return positional[0] if positional else ""
    first = positional[0]
    looks_like_container = (re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", first) is not None
                            and first not in _READONLY_CMDS and first not in ("ros2", "find", "top", "source"))
    return " ".join(positional[1:] if looks_like_container else positional)


class GuardMixin:

    def _guard_exempt(self, skill, args):
        """被擋的技能在這些參數下其實不會改變狀態：workpackage_send --dry-run、docker_runcmd 的唯讀指令。"""
        if skill == "workpackage_send":
            return "--dry-run" in args
        if skill == "docker_runcmd":
            command = _docker_runcmd_command(args)
            return "{" not in command and is_readonly_shell(command)   # 組合技能的 {參數} 佔位符看不出實際指令，不放行
        return False

    @staticmethod
    def _composite_steps(script_path):
        """make_skill 產生的組合腳本（資料檔：NAME／PARAMS／STEPS）→ STEPS；不是組合腳本回傳 None。"""
        try:
            with open(script_path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return None
        if "from _composite import run_composite" not in text:
            return None
        m = re.search(r"^STEPS = (\[.*?\])\n\n", text, re.S | re.M)
        try:
            steps = json.loads(m.group(1)) if m else []
        except ValueError:
            steps = []
        return [s for s in steps if isinstance(s, dict)]

    def guard_check(self, parsed):
        """這一輪的 action 執行前需不需要使用者確認。回傳 None（不需要），或
        {"skill", "script", "command", "reason"} 給 CLI／Web 顯示。parsed 是 parse_agent_reply 的結果（或含 action 的 dict）。
        技能名稱（只載入規格）、找不到的腳本、偽技能（result_recall）都不需要確認。"""
        if not getattr(self, "guard_enabled", True) or not GUARDED_SKILLS:
            return None
        action = (parsed or {}).get("action") if isinstance(parsed, dict) else None
        if not action:
            return None
        payload = action_text(action)
        parts = payload.split(maxsplit=1)
        raw_token = os.path.basename(parts[0])
        remainder = parts[1] if len(parts) > 1 else ""
        doc_name = raw_token[:-3] if raw_token.endswith(".md") else raw_token
        if os.path.exists(os.path.join(self.tools_dir, f"{doc_name}.md")):
            return None
        script_name = self._normalize_script_name(raw_token)
        script_path = os.path.join(self.base_path, "scripts", script_name)
        if not os.path.exists(script_path):
            return None
        skill_map = self._script_skill_map()
        skill = skill_map.get(script_name)
        args = self._parse_script_args(script_name, remainder)
        steps = self._composite_steps(script_path)
        if steps is not None:
            guarded = []
            for st in steps:
                sk = st.get("skill") or skill_map.get(st.get("script") or "")
                if sk in GUARDED_SKILLS and not self._guard_exempt(sk, [str(a) for a in st.get("args") or []]):
                    guarded.append(sk)
            if not guarded:
                return None
            reason = f"組合技能，包含會改變狀態的步驟：{'、'.join(dict.fromkeys(guarded))}"
        else:
            if skill not in GUARDED_SKILLS or self._guard_exempt(skill, args):
                return None
            reason = _GUARD_REASONS.get(skill, "這個技能會改變系統狀態")
        return {"skill": skill or script_name, "script": script_name,
                "command": f"scripts/{script_name} {remainder}".strip(), "reason": reason}

    @staticmethod
    def guard_prompt_text(info):
        """給使用者看的確認說明（CLI 印出、Web 顯示在確認列上方）。"""
        return (f"🛡️ AI 要執行會改變系統狀態的動作，需要你確認：\n"
                f"- 技能：{info['skill']}（{info['reason']}）\n"
                f"- 指令：{info['command']}")

    @staticmethod
    def guard_denied_text(info):
        """使用者拒絕時交給模型的結果（包成 [tool result] 進主對話）。"""
        return (f"{GUARD_DENIED_TAG} 使用者沒有同意執行 {info['skill']}（{info['command']}）：這個動作沒有執行，系統狀態沒有改變。"
                f"不要再送出同一個指令；等使用者說明要怎麼調整，或改做別的。")

    @staticmethod
    def guard_blocked_text(info):
        """run_tool 沒拿到 approved=True 就碰到被擋的技能時的回傳（CLI／Web 以外的呼叫端、或漏接確認的程式路徑）。"""
        return (f"{GUARD_DENIED_TAG} {info['skill']} 會改變系統狀態（{info['reason']}），需要使用者在畫面上確認才能執行；"
                f"這次沒有執行。")
