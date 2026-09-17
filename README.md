# AI_agent_harness
自訂義開發的 AI_agent_harness 架構

## 架構概覽

- `Agent_Runner.py`：主迴圈（Ollama + gemma4:e4b），解析 `NEED_TOOL:` / `EXECUTE:` 兩種指令。
- `ROBOT_AGENT.md`：Agent 角色設定與執行協議（系統提示詞主體）。
- `memory/`：長期記憶，依語意（semantic）／情節（episodic）／程序（procedural）三種類型分檔存放。
- `skills_system/INDEX.md`：技能索引，每次對話都會載入，只含名稱與一行描述。
- `skills_system/tools/<name>.md`：各技能的完整規格書，僅在 Agent 輸出 `NEED_TOOL: <name>` 時才會被讀入對話。
- `skills_system/scripts/*.py`：各技能實際執行的腳本，由 `EXECUTE: <script> <args>` 呼叫。
- `manager.py` + `scripts/manage_skill_cmd.py`：自我進化機制，動態建立新技能（同步產生腳本、規格書、索引列）。

## 執行方式

```
python Agent_Runner.py
```

REPL 內建指令：`/auto on|off`、`/hybrid on|off`、`/compress`、`objective set|show|clear`、`exit`/`quit`。
