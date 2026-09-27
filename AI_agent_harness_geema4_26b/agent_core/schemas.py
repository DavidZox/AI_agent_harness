"""給 Ollama format= 的 JSON schema：主對話回覆、滾動摘要、工具摘要、技能草稿。"""


# 🧷 回覆協議：模型每次回覆都是一個 JSON 物件 {thought, reply, action}。執行與否只看 action 欄位，
# reply 裡不論寫了什麼（包括解釋、舉例時抄出來的 `EXECUTE: ...` 字串）都不會被執行——這是「實體隔離」：
# 給人看的文字與給系統執行的指令分開存放，不再用文字比對從回覆裡找指令。以 Ollama 的 format= schema 強制
# 結構（與摘要、make_skill 同一機制），小模型不需要自律「解釋時不要輸出指令」。
# action 為 null 或 {"command": 技能名稱｜規格標明的腳本路徑, "args": 參數字串}；args 是字串而不是物件，
# 因為所有技能腳本都吃位置參數、規格文件的寫法也是位置參數，這樣既有的 tools/*.md 一份都不用改：
# 規格裡的 `EXECUTE: <路徑> <參數>` 範例就對應 command=<路徑>、args=<參數>。
AGENT_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "reply": {"type": "string"},
        "action": {"anyOf": [
            {"type": "null"},
            {
                "type": "object",
                "properties": {"command": {"type": "string"}, "args": {"type": "string"}},
                "required": ["command", "args"],
            },
        ]},
    },
    "required": ["thought", "reply", "action"],
}

# 技能草稿的 JSON schema（Ollama format=）：欄位意義見 SkillAgent._make_skill_prompts，驗證見 _normalize_skill_draft。
MAKE_SKILL_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "category": {"type": "string"},
        "purpose": {"type": "string"},
        "parameters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "example": {"type": "string"},
                },
                "required": ["name", "description", "example"],
            },
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "integer"},
                    "include": {"type": "boolean"},
                    "purpose": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["step_id", "include", "purpose", "args"],
            },
        },
        "success_criteria": {"type": "string"},
        "pitfalls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "category", "purpose", "parameters", "steps", "success_criteria", "pitfalls"],
}

# 融合摘要的 JSON schema：交給 Ollama 的 format= 做結構化輸出，再由 _render_summary_markdown 排版。
SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "key_progress": {"type": "array", "items": {"type": "string"}},
        "results_and_errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "description": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["category", "description", "detail"],
            },
        },
        "user_preferences": {"type": "array", "items": {"type": "string"}},
        "open_items": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["overview", "key_progress", "results_and_errors", "user_preferences", "open_items"],
}

# 任務導向摘要的結構（Ollama format=）：answer 一句話回答這一步的目的、facts 照抄的相關事實、errors 錯誤原文、
# not_covered 原始輸出沒有／被省略而無法確認的部分——讓主模型知道「摘要裡沒有」不等於「輸出裡沒有」。
TOOL_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "facts": {"type": "array", "items": {"type": "string"}},
        "errors": {"type": "array", "items": {"type": "string"}},
        "not_covered": {"type": "string"},
        "suggested_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"question": {"type": "string"}, "keywords": {"type": "string"}},
                "required": ["question", "keywords"],
            },
        },
        "index_hint": {"type": "string"},
        "related_records": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["id", "reason"],
            },
        },
    },
    "required": ["answer", "facts", "errors", "not_covered", "suggested_questions", "index_hint", "related_records"],
}
