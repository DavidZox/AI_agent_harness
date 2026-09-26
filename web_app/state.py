"""全域狀態：單一 SkillAgent 實例、模式開關、待決策的工具結果、待核准的計畫、附件與手動載入的技能規格。
其他模組 import 這裡的物件（都是可變的 dict／物件、沒有重新賦值），所以大家看到的是同一份狀態。"""
import os
import threading
from vision import VisionSession
from agent_core.agent import SkillAgent
from agent_core.config import PARALLEL_CAL_DEFAULT, TOOL_SUMMARY_DEFAULT


# =========================================================
# Agent 狀態（單一使用者、單一 Agent 實例）
# =========================================================

agent = SkillAgent(model=os.environ.get("WEB_CONSOLE_MODEL", "gemma4:e4b"), max_history=None)  # 則數視窗停用，統一以 token 門檻壓縮
agent.reset_conversation()

state = {
    "auto_mode": False,
    "hybrid_mode": False,
    "tool_summary_mode": TOOL_SUMMARY_DEFAULT,  # 超過門檻的工具回傳交給獨立 session 做任務導向摘要（預設開）
    "plan_mode": False,
    "parallel_cal": PARALLEL_CAL_DEFAULT,  # 軟水位壓縮改在背景執行緒做（/parallel_cal on|off）
}
pending = {"result": None, "mode": None, "tokens": None}  # 等待使用者決策的工具結果（hybrid / manual 模式用）
plan_pending = {"active": False, "text": None}  # 等待使用者核准／修改意見的任務計畫（/plan 模式用）
vision_session = VisionSession()  # 📷 尚未送出的影像附件（框選截圖／上傳的檔案），送出新任務時一次消費
lock = threading.Lock()

# 📘 使用者從「/」選單（或 /skill <名稱>）手動載入、尚未送出的技能規格：name -> block。
# 跟 📷 附件同一種生命週期：送出下一個新任務時一次消費（slash 指令、計畫回應、工具決策不會）。
pending_skills = {}
pending_skills_lock = threading.Lock()

DEFAULT_SKILL_PROMPT = "我已手動載入上述技能的規格，請依規格用兩三句話說明它的用途與呼叫方式，然後等待我的指示。"


def load_pending_skill(name):
    """把技能規格加入待送清單。回傳 {name, doc, tokens, pending}；技能不存在時 raise ValueError。"""
    name = (name or "").strip()
    if name.endswith(".md"):
        name = name[:-3]
    block = agent.manual_skill_block(name) if name else None
    if block is None:
        raise ValueError(f"找不到技能 '{name}'，請用 /skills 或「/」選單查看可用名稱。" if name else "用法：/skill <技能名稱>")
    with pending_skills_lock:
        already = name in pending_skills
        pending_skills[name] = block
        pending = list(pending_skills)
    return {"name": name, "doc": block, "tokens": agent.count_tokens(block), "pending": pending, "already": already}


def remove_pending_skill(name):
    with pending_skills_lock:
        pending_skills.pop((name or "").strip(), None)
        return list(pending_skills)


def pending_skill_names():
    with pending_skills_lock:
        return list(pending_skills)


def take_all_pending_skills():
    """送出新任務時一次取走全部待送的技能規格（回傳 [(name, block), ...]）。"""
    with pending_skills_lock:
        items = list(pending_skills.items())
        pending_skills.clear()
    return items
