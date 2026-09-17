# AI_agent_harness
自訂義開發的 AI_agent_harness 架構

## 架構概覽

- `Agent_Runner.py`：主迴圈（Ollama + gemma4:e4b），解析 `NEED_TOOL:` / `EXECUTE:` 兩種指令。
- `ROBOT_AGENT.md`：Agent 角色設定與執行協議（系統提示詞主體）。
- `memory/`：長期記憶，依語意（semantic）／情節（episodic）／程序（procedural）三種類型分檔存放。
- `skills_system/INDEX.md`：技能索引，每次對話都會載入，只含名稱與一行描述。
- `skills_system/tools/<name>.md`：各技能的完整規格書，僅在 Agent 輸出 `NEED_TOOL: <name>` 時才會被讀入對話，且會在連續閒置數輪後自動從上下文清除（見下方「技能生命週期」）。
- `skills_system/scripts/*.py`：各技能實際執行的腳本，由 `EXECUTE: <script> <args>` 呼叫。
- `manager.py` + `scripts/manage_skill_cmd.py`：自我進化機制，動態建立新技能（同步產生腳本、規格書、索引列）。

## 上下文管理

- **上下文預算監控**：`SkillAgent.enforce_context_budget()` 在每次呼叫模型**之前**（於 `ask_ai()` 內）主動檢查 token 數，超過 `token_threshold`（預設 8000，可用 `/budget` 查看/設定）就觸發壓縮。只壓縮動態對話歷史（`messages[1:]`），system prompt（profile／objective／memory／skills index）不受影響。
- **技能生命週期清除**：`SkillAgent.evict_idle_tools()` 同樣在每次呼叫模型前執行，把連續 `tool_idle_eviction_turns`（預設 6 輪，可用 `/skill_ttl` 查看/設定）都沒有被實際 `EXECUTE` 的技能規格書，從對話中精準移除（而不是像壓縮那樣整批處理），並讓該技能從「已載入」狀態除名——之後要再用需要重新 `NEED_TOOL`。

## 執行方式

```
python Agent_Runner.py
```

REPL 內建指令：`/auto on|off`、`/hybrid on|off`、`/compress`、`/budget [數字]`、`/skill_ttl [數字]`、`/objective [文字|clear]`、`objective set|show|clear`（多行版本）、`exit`/`quit`。
