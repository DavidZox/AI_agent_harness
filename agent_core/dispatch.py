"""DispatchMixin：技能載入與派發——兩階段揭露（技能名稱回規格、腳本路徑才執行）、run_tool、_exec_script。"""
import os
import shlex
import subprocess
import sys
from .config import SKILL_DOC_PREFIX, SKILL_LOADED_MARKER, TOOL_EXEC_TIMEOUT
from .protocol import action_text, parse_agent_reply


class DispatchMixin:

    def _skill_memory_entries(self, skill_name):
        """技能綁定的經驗記憶條目（skills_system/memory/<skill>.md 裡以「- [」開頭的行）。
        沒有檔案或沒有條目時回傳空 list。"""
        path = os.path.join(self.skill_memory_dir, f"{skill_name}.md")
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return [line.rstrip() for line in f if line.lstrip().startswith("- [")]
        except OSError:
            return []

    def list_skills(self):
        """解析 SKILLS.md 索引：每個技能的名稱、一行描述、所屬分類（## 標題），以及是否有綁定的經驗記憶。
        只列出 tools/<name>.md 真的存在的技能。Web Console 的「/」選單與 CLI 的 /skills 都用這個。"""
        skills = []
        category = "其他"
        try:
            with open(self.index_file, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return skills
        for line in lines:
            if line.startswith("## "):
                category = line[3:].strip()
                continue
            if not line.startswith("- ["):
                continue
            name = line[3:line.index("]")] if "]" in line else ""
            if not name or not os.path.exists(os.path.join(self.tools_dir, f"{name}.md")):
                continue
            desc = line.split("—", 1)[1].strip() if "—" in line else ""
            skills.append({
                "name": name,
                "description": desc,
                "category": category,
                "has_memory": bool(self._skill_memory_entries(name)),
            })
        return skills

    def manual_skill_block(self, skill_name):
        """使用者手動按需載入技能規格時，要附在下一則使用者訊息後面的區塊（含該技能的經驗記憶）。
        內容等同 AI 自己 EXECUTE 技能名稱後系統回傳的規格，讓 AI 可以直接依其中的腳本路徑執行、
        省掉一輪「先載規格」。技能不存在回傳 None。"""
        doc = self._load_skill_doc(skill_name)
        if doc is None:
            return None
        name = skill_name[:-3] if skill_name.endswith(".md") else skill_name
        return (
            f"{SKILL_LOADED_MARKER}\n"
            f"{SKILL_DOC_PREFIX} '{name}' 的規格文件（使用者從選單手動載入，等同你以 action.command 填技能名稱後"
            f"系統回傳的規格；依此內容才可執行：action.command 填其中標明的實際腳本路徑、args 填參數）：\n{doc.rstrip()}"
        )

    def _load_skill_doc(self, skill_name):
        """若 skill_name 對應到 tools/<skill_name>.md，回傳其內容；否則回傳 None。
        容忍 AI 直接照抄索引連結而帶上 .md 後綴（例如 list_dir.md）。
        若該技能有綁定的經驗記憶（memory/<skill_name>.md），附在規格文件後面一起回傳：
        這些是使用者要求記住、專屬於此技能的修正，只在真的用到此技能時才進入上下文。"""
        if skill_name.endswith(".md"):
            skill_name = skill_name[:-3]
        doc_path = os.path.join(self.tools_dir, f"{skill_name}.md")
        if not os.path.exists(doc_path):
            return None
        with open(doc_path, "r", encoding="utf-8") as f:
            doc = f.read()
        entries = self._skill_memory_entries(skill_name)
        if not entries:
            return doc
        memory_block = (
            f"# 經驗記憶（使用者曾要求記住、專屬於 {skill_name} 的 {len(entries)} 則經驗，使用此技能時必須遵守）\n"
            + "\n".join(entries)
        )
        return f"{doc.rstrip()}\n\n{memory_block}\n"

    def _normalize_script_name(self, raw_token):
        """確保腳本檔名以 _cmd.py 結尾（例如 cd -> cd_cmd.py，find_file.py -> find_file_cmd.py）。"""
        if not raw_token.endswith("_cmd.py") and not raw_token.endswith(".py"):
            return f"{raw_token}_cmd.py"
        if raw_token.endswith(".py") and not raw_token.endswith("_cmd.py"):
            return raw_token.replace(".py", "_cmd.py")
        return raw_token

    def _parse_script_args(self, script_name, remainder):
        """把 action.args 字串以 shlex 依空白／引號拆成參數列表（含空白的參數用雙引號包住）。
        空字串參數（模型常寫成 `. ""`）一律丟掉：沒有任何腳本需要空的位置參數，留著只會變成 [ERROR] 找不到路徑。"""
        try:
            parts = shlex.split(remainder)
        except Exception:   # 引號不成對（實測模型會送 """）：退回空白切分並去掉殘留的引號
            parts = [a.strip("\"'") for a in remainder.split()]
        return [a for a in parts if a != ""]

    def _sync_state_from_tool_output(self, output_text):
        """解析工具輸出中的狀態標記，同步更新目前的工作目錄／容器目錄。"""
        lines = output_text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("[CWD_CHANGED]"):
                self.current_cwd = line.replace("[CWD_CHANGED]", "").strip()
            # 抓取 [CONTAINER_CWD] 標記的下一行作為路徑
            if line.startswith("[CONTAINER_CWD]") and i + 1 < len(lines):
                self.container_cwd = lines[i + 1].strip()
            # 目標容器：容器技能成功操作某容器後印在末行（見 _docker_common.with_target_marker）
            if line.startswith("[TARGET_CONTAINER]"):
                self.target_container = line[len("[TARGET_CONTAINER]"):].strip()

    def run_tool(self, ai_response):
        """依 AI 回覆 JSON 的 action 欄位執行（ai_response 可以是原始 JSON 字串或 parse_reply 的結果）。
        action 為 null／回覆不是合法 JSON → 不執行、回傳 None；reply 裡的文字完全不看。
        有 action 時行為分兩種：

        1. 若 command 對應到 tools/<name>.md 的技能規格文件（代表 AI 用的是
           SKILLS.md 索引裡的技能名稱），直接把規格文件內容當作系統回傳注入
           上下文，不執行任何腳本——這就是按需載入 (Progressive Disclosure)。
           不做技能名稱 -> 腳本檔名的猜測或對照；AI 讀完規格後，下一輪需改用
           規格書中標明的實際腳本路徑（例如 scripts/cd_cmd.py）才會真正執行。
        2. 否則將 command 視為實際腳本路徑，以 args 為參數執行對應的 CLI 腳本並回傳結果。
        """
        parsed = ai_response if isinstance(ai_response, dict) else parse_agent_reply(ai_response)
        action = parsed.get("action")
        self.last_result_id = self.last_result_file = None   # 只有真的執行腳本（_record_trajectory）才會有存檔
        if not action:
            return None

        try:
            payload = action_text(action)
            parts = payload.split(maxsplit=1)
            raw_token = os.path.basename(parts[0])
            remainder = parts[1] if len(parts) > 1 else ""

            skill_doc = self._load_skill_doc(raw_token)
            if skill_doc is not None:
                n_memory = len(self._skill_memory_entries(raw_token[:-3] if raw_token.endswith(".md") else raw_token))
                memory_note = f"，附 {n_memory} 則技能經驗記憶" if n_memory else ""
                print(f"📖 Agent 選擇技能索引: {raw_token}（載入規格文件{memory_note}，尚未執行）")
                # 回給模型的這段話是兩階段流程裡最關鍵的一句：實測 JSON 協議下小模型載完規格容易停下來解釋規格或
                # 反問使用者（reply 欄位本身就在邀請它聊天），所以這裡直接下達下一輪該做的事。帶了參數時再明說那些參數
                # 沒有被執行——實測模型用「docker_open <容器>」載完規格就當作容器已切換，直接做下一步。
                # 實測（2026-09-25 情境稽核）：只寫「繼續使用者原本的任務」時，模型會跳過這一步直接做下一步（載完 change_dir
                # 規格就去 ls），所以要明說「下一輪先把這一個技能真正執行一次，之後才做下一步」。
                if remainder.strip():
                    next_step = (f"你附的參數「{remainder.strip()}」也沒有被執行、狀態沒有改變。下一輪請先把這個技能真正執行一次："
                                 f"action.command 填規格標明的實際腳本路徑（scripts/...）、args 填同樣的參數，執行完看到結果後才做下一步；")
                else:
                    next_step = ("下一輪請先把這個技能真正執行一次——action.command 填規格標明的實際腳本路徑（scripts/...）、"
                                 "args 填參數，執行完看到結果後才做使用者任務的下一步；")
                return (f"{SKILL_DOC_PREFIX} '{raw_token}' 的規格文件（由系統提供）。這一輪只是載入規格、還沒有執行任何東西：{next_step}"
                        f"不要向使用者解釋規格內容，也不要詢問是否要執行。\n{skill_doc}")

            script_name = self._normalize_script_name(raw_token)
            if script_name == "result_recall_cmd.py":
                # 偽技能：result_recall 沒有實體 scripts/result_recall_cmd.py（要呼叫 ollama.chat，只能在本
                # process 內做；核心邏輯都在 agent_core，見 doc/架構說明.md），必須在存在性檢查前攔截，
                # 否則會落入下面「找不到腳本」的分支。規格文件走一般的兩階段揭露，不受影響。
                return self._result_recall(remainder)
            script_path = os.path.join(self.base_path, "scripts", script_name)

            print(f"🛠️  Agent 啟動工具: {script_name}")
            if not os.path.exists(script_path):
                # 幾乎都是模型跳過「先載入規格」直接猜腳本檔名（例如 list_dir_cmd.py，實際是 ls_cmd.py）。
                # 給可直接行動的指引：猜的名稱裡若含有某個技能名稱，就建議先 EXECUTE 那個技能取得規格。
                stem = script_name[:-len("_cmd.py")] if script_name.endswith("_cmd.py") else script_name
                candidates = [
                    f[:-3] for f in sorted(os.listdir(self.tools_dir))
                    if f.endswith(".md") and (f[:-3] in stem or stem in f[:-3])
                ] if os.path.isdir(self.tools_dir) else []
                hint = (
                    f"這個名稱看起來是技能 {', '.join(candidates)}，請先把 action.command 填成 `{candidates[0]}`（args 留空）"
                    f"載入規格文件，再依規格標明的實際腳本路徑執行。"
                    if candidates else
                    "腳本路徑只能從規格文件取得，不可自行推測：請先以 action.command 填入 SKILLS.md 裡的技能名稱載入規格。"
                )
                return f"[ERROR] 找不到腳本 {script_name}。{hint}"

            clean_args = self._parse_script_args(script_name, remainder)
            # 軌跡記錄用：執行「前」的狀態
            cwd_before, container_before, target_before = self.current_cwd, self.container_cwd, self.target_container
            output_text = self._exec_script(script_name, script_path, clean_args)
            self._sync_state_from_tool_output(output_text)  # 逾時訊息裡不會有狀態標記，掃過去是安全的 no-op
            self._record_trajectory(script_name, clean_args, output_text, cwd_before, container_before, target_before)
            return output_text

        except Exception as e:
            return f"解析指令失敗: {e}"

    def _exec_script(self, script_name, script_path, clean_args):
        """執行一支腳本、回傳 output_text；不記軌跡、不同步狀態（呼叫端負責）——run_tool() 的正常派發，
        與 summarize_tool_result() 的自動追問（直接跑 result_grep_cmd.py，跳過整個 run_tool，不印
        「Agent 啟動工具」這種暗示模型主動執行的訊息）共用同一個執行原語。"""
        env = os.environ.copy()
        env["CONTAINER_CWD"] = self.container_cwd
        env["TARGET_CONTAINER"] = self.target_container  # 容器技能省略容器名稱時的預設（比照 cwd）
        env["TOOL_RESULTS_DIR"] = self.results_dir()     # result_list／result_grep／result_view 的存檔目錄
        env["HARNESS_SESSION"] = self.session_id         # 讓 result_grep 16 優先解析成目前 session 的 #16
        try:
            res = subprocess.run(
                [sys.executable, script_path] + clean_args,
                capture_output=True,
                text=True,
                cwd=self.current_cwd,
                env=env,
                timeout=TOOL_EXEC_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return (
                f"[ERROR] 工具 {script_name} 執行逾時（超過 {TOOL_EXEC_TIMEOUT} 秒），已被系統強制終止。"
                f"這是 harness 的最後防線，各腳本自身應有更短的逾時；若經常觸發請檢查該腳本。"
            )
        if res.returncode == 0:
            return res.stdout.strip()
        # 腳本異常結束：優先用 stderr，沒有就用 stdout；兩者皆空也要給 AI 一個
        # 明確的失敗訊息，否則空字串會被誤判成「沒有工具需要執行」。
        # 統一補上 [ERROR] 前綴，讓 _content_for_context 能正確判定為失敗。
        output_text = res.stderr.strip() or res.stdout.strip() or "（沒有任何輸出）"
        if not output_text.startswith("[ERROR]"):
            output_text = f"[ERROR] 腳本 {script_name} 異常結束（exit code {res.returncode}）:\n{output_text}"
        return output_text
