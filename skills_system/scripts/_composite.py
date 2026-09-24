"""組合技能（make_skill 產生）共用執行層。

make_skill 把一段使用者引導 Agent「做對了」的操作軌跡編譯成新技能時，不讓模型寫任何 Python：
產生的 scripts/<name>_cmd.py 只是一份資料（NAME / PARAMS / STEPS），真正的執行邏輯都在這裡：

1. 依 PARAMS 的順序把命令列位置參數對應成 {名稱} 佔位符的值，代入每個步驟的 args；
   其他大括號（docker 的 {{.Names}}、shell 的 ${VAR}）不是已宣告的參數名就原樣保留。
2. 依序以 python3 執行既有技能的腳本（與 harness 的 run_tool 相同：同一個 cwd、同一組環境變數），
   任一步輸出以 [ERROR] 開頭或非零結束即停止，不再執行後面的步驟。
3. 步驟之間同步狀態：change_dir 類腳本印出的 [CWD_CHANGED] 會改變後續步驟的 cwd，
   [CONTAINER_CWD]／[TARGET_CONTAINER] 會更新後續步驟的環境變數；這些標記行原樣保留在總輸出裡，harness 也會同步。
4. 逾時：單步 STEP_TIMEOUT_SECONDS，全部合計 TOTAL_TIMEOUT_SECONDS（低於 harness 的 600 秒總逾時），
   各底層腳本自己更短的逾時仍然有效。
5. 回傳沿用專案慣例：成功以 [PASS] 開頭並附各步驟輸出（中間步驟截短、最後一步保留較多），
   失敗以 [ERROR] 開頭並指出第幾步、附該步原因與之前步驟的輸出供診斷。

草稿階段的腳本位於 skills_system/drafts/<name>/，會自行往上找到本目錄；正式註冊後與其他腳本同目錄。
"""
import os
import re
import subprocess
import sys
import time

STEP_TIMEOUT_SECONDS = 300
TOTAL_TIMEOUT_SECONDS = 570      # 必須低於 Agent_Runner.TOOL_EXEC_TIMEOUT (600)
MID_STEP_OUTPUT_CHARS = 600
LAST_STEP_OUTPUT_CHARS = 3000

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def placeholders(arg):
    """arg 裡出現的所有佔位符名稱（不去重）。"""
    return _PLACEHOLDER.findall(arg or "")


def substitute(arg, values):
    """只替換 values 裡有的佔位符，其他大括號原樣保留。"""
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)] if m.group(1) in values else m.group(0), arg or "")


def usage_text(name, params):
    if not params:
        return f"用法: scripts/{name}_cmd.py（不需要參數）"
    sig = " ".join(f"<{p['name']}>" for p in params)
    lines = [f"用法: scripts/{name}_cmd.py {sig}"]
    for p in params:
        lines.append(f"  {p['name']}：{p.get('description', '')}（例：{p.get('example', '')}）")
    return "\n".join(lines)


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n…（此步輸出共 {len(text)} 字，已截短）"


def _sync_state(text, cwd, env):
    """比照 Agent_Runner._sync_state_from_tool_output：讓後續步驟接續前一步改變的目錄。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("[CWD_CHANGED]"):
            new_cwd = line[len("[CWD_CHANGED]"):].strip()
            if new_cwd and os.path.isdir(new_cwd):
                cwd = new_cwd
        if line.startswith("[CONTAINER_CWD]") and i + 1 < len(lines):
            env = dict(env)
            env["CONTAINER_CWD"] = lines[i + 1].strip()
        if line.startswith("[TARGET_CONTAINER]"):   # 前一步選定／操作的容器，後續省略容器名稱的步驟接著用
            env = dict(env)
            env["TARGET_CONTAINER"] = line[len("[TARGET_CONTAINER]"):].strip()
    return cwd, env


def _fail(name, idx, total, purpose, error_text, outputs):
    parts = [f"[ERROR] {name} 在第 {idx}/{total} 步（{purpose}）失敗，已停止：", error_text]
    if outputs:
        parts.append(f"\n--- 之前 {len(outputs)} 步的輸出（供診斷）---")
        for i, p, t in outputs:
            parts.append(f"[步驟 {i}/{total}：{p}]\n{_truncate(t, MID_STEP_OUTPUT_CHARS)}")
    return "\n".join(parts)


def run_composite(name, params, steps, argv, scripts_dir=None):
    """依序執行 steps；回傳 [PASS]/[ERROR] 開頭的字串（不拋例外，讓呼叫端直接 print）。"""
    scripts_dir = scripts_dir or os.path.dirname(os.path.abspath(__file__))
    argv = list(argv)
    if len(argv) != len(params):
        return f"[ERROR] {name} 需要 {len(params)} 個參數，收到 {len(argv)} 個。\n{usage_text(name, params)}"
    if not steps:
        return f"[ERROR] {name} 沒有任何步驟可執行（STEPS 為空）。"
    values = {p["name"]: v for p, v in zip(params, argv)}

    cwd = os.getcwd()
    env = os.environ.copy()
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    outputs = []
    total = len(steps)

    for idx, step in enumerate(steps, 1):
        purpose = step.get("purpose") or step.get("script", "")
        script_path = os.path.join(scripts_dir, step["script"])
        if not os.path.exists(script_path):
            return _fail(name, idx, total, purpose,
                         f"[ERROR] 找不到底層腳本 {step['script']}（{script_path}），此組合技能依賴的技能可能已被移除或改名。",
                         outputs)
        args = [substitute(a, values) for a in step.get("args", [])]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _fail(name, idx, total, purpose,
                         f"[ERROR] 全部步驟合計已超過 {TOTAL_TIMEOUT_SECONDS} 秒，此步未執行。", outputs)
        timeout = min(STEP_TIMEOUT_SECONDS, remaining)
        try:
            res = subprocess.run(
                [sys.executable, script_path] + args,
                capture_output=True, text=True, cwd=cwd, env=env, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return _fail(name, idx, total, purpose,
                         f"[ERROR] 步驟逾時（超過 {int(timeout)} 秒）：scripts/{step['script']} {' '.join(args)}", outputs)
        text = (res.stdout or "").strip()
        if res.returncode != 0:
            text = (res.stderr or "").strip() or text or "（沒有任何輸出）"
            if not text.startswith("[ERROR]"):
                text = f"[ERROR] 腳本 {step['script']} 異常結束（exit code {res.returncode}）:\n{text}"
        if text.lstrip().startswith("[ERROR]"):
            return _fail(name, idx, total, purpose, text, outputs)
        cwd, env = _sync_state(text, cwd, env)
        outputs.append((idx, purpose, text))

    parts = [f"[PASS] {name} 完成 {total}/{total} 步"]
    for idx, purpose, text in outputs:
        limit = LAST_STEP_OUTPUT_CHARS if idx == total else MID_STEP_OUTPUT_CHARS
        parts.append(f"--- 步驟 {idx}/{total}：{purpose} ---\n{_truncate(text, limit)}")
    return "\n".join(parts)
