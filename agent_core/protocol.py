"""回覆協議與訊息包裝：解析主模型的 JSON 回覆、把工具結果包成主對話訊息、判斷回傳是不是規格文件或已提煉的結果。"""
import json
import os
from .config import SKILL_DOC_PREFIX, TOOL_RESULT_FRAME, TOOL_SUMMARY_TAG


def attach_skill_docs(message, blocks):
    """把使用者手動載入的技能規格區塊（manual_skill_block 的回傳）附在這則使用者訊息後面。
    跟 📷 影像分析結果同一種做法：使用者原文在前、系統插入的內容在後，主對話維持純文字單一訊息，
    不會出現連續兩則 user 訊息。"""
    blocks = [b for b in (blocks or []) if b]
    if not blocks:
        return message
    return message + "\n\n" + "\n\n".join(blocks)


def quote_cli_arg(arg):
    """含空白／引號的參數以雙引號包住（與規格範例一致，shlex 可還原）。"""
    arg = str(arg)
    if arg == "" or any(c.isspace() for c in arg) or '"' in arg or "'" in arg:
        return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return arg


def parse_agent_reply(raw):
    """模型回覆（JSON 字串）→ {"thought", "reply", "action", "valid"}。

    action 正規化為 None 或 {"command": str, "args": str}。不是合法 JSON 時 valid=False、reply=原文、
    action=None：降級成「只顯示文字、不執行任何東西」，絕不退回用文字比對找指令（那正是要避免的誤觸發來源）。
    對非 format= 強制的後端保留一點容錯：args 給成陣列／物件、把整行 `EXECUTE: ...` 塞進 command、
    command 裡夾帶參數，都會被整理成同一種形狀。"""
    text = (raw or "").strip()
    if text.startswith("```"):  # format= 下不會出現，保險去掉程式碼區塊包裹
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        data = None
    if not isinstance(data, dict):
        return {"thought": "", "reply": (raw or "").strip(), "action": None, "valid": False}

    thought = str(data.get("thought") or "").strip()
    reply = data.get("reply")
    reply = "" if reply is None else str(reply).strip()
    action = data.get("action")
    norm = None
    if isinstance(action, str) and action.strip().lower() not in ("", "null", "none"):
        action = {"command": action, "args": ""}
    if isinstance(action, dict):
        command = str(action.get("command") or "").strip()
        args = action.get("args")
        if isinstance(args, list):
            args = " ".join(quote_cli_arg(a) for a in args)
        elif isinstance(args, dict):
            args = " ".join(quote_cli_arg(v) for v in args.values())
        args = "" if args is None else str(args).strip()
        if command.upper().startswith("EXECUTE:"):
            command = command[len("EXECUTE:"):].strip()
        head, _, rest = command.partition(" ")
        if rest.strip():  # command 夾帶了參數："scripts/cd_cmd.py /opt" → 拆到 args 前面
            command, args = head, (rest.strip() + (" " + args if args else ""))
        if command:
            norm = {"command": command, "args": args}
    return {"thought": thought, "reply": reply, "action": norm, "valid": True}


def action_text(action):
    """action 的單行文字表示（顯示與軌跡用）：`command args`。"""
    if not action:
        return ""
    return f"{action['command']} {action['args']}".strip()


def tool_result_message(content, action=None):
    """把工具結果包成進主對話的 user 訊息（CLI 三種模式、Web、捨棄通知統一用這個）。
    第一行固定 [tool result]（HARNESS_MARKERS 判定、壓縮切點、軌跡都認它），第二行是框架句：明說這是系統執行
    上一輪 action 的結果、不是使用者提供的、不要感謝使用者。

    為什麼寫在每一則裡而不是只寫在 AGENT.md：工具結果只能以 user 角色進入主對話——實測 Ollama 的 gemma4 模板會把
    role=tool 的訊息整個丟掉（模型完全看不到內容）；而 AGENT.md 的 Harness Messages 規則對 4B 模型不夠，實測仍回
    「感謝您提供的語義地圖」。影像分析結果（web_app.flows._run_vision_subsession）用的是同一招。
    action 可給 parse_reply 的 action dict 或指令字串，只取腳本檔名放進框架句方便模型對應。"""
    label = ""
    if isinstance(action, dict) and action.get("command"):
        label = os.path.basename(str(action["command"]).strip())
    elif isinstance(action, str) and action.strip():
        label = os.path.basename(action.strip().split()[0])
    what = f"你上一輪 action（{label}）" if label else "你上一輪 action"
    frame = (f"{TOOL_RESULT_FRAME}以下是系統執行{what}的結果，由系統自動產生、不是使用者提供的（使用者也看到同一份）。"
             "回覆時稱「執行結果」或「系統回傳」，不要感謝使用者、不要說「您提供的」。")
    tail = f"{TOOL_RESULT_FRAME}（以上為系統回傳，不是使用者提供的）"   # 結尾再提醒一次：長輸出時開頭那句離模型太遠
    return f"[tool result]\n{frame}\n{content}\n{tail}"


def is_skill_doc_result(result):
    """result 是否為 run_tool 載入規格文件的回傳（而非腳本執行結果）。"""
    return bool(result) and result.lstrip().startswith(SKILL_DOC_PREFIX)


def is_exempt_result(result, tool_tokens):
    """不套用工具回傳門檻的結果：技能規格文件，以及本身已經是任務導向擷取的結果（result_recall 的輸出，以
    TOOL_SUMMARY_TAG 開頭）——後者再摘要一次只會摘要的摘要、多一次模型呼叫。所有腳本輸出（包括分析型的
    --watch／--duration／語義地圖）都走同一套規則——超過門檻就交給獨立 session 做任務導向擷取。
    CLI／Web 的 ⚠️ 標記與 _content_for_context 共用這個判斷。"""
    return is_skill_doc_result(result) or result.startswith(TOOL_SUMMARY_TAG)
