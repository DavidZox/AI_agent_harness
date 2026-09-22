import os
import sys
import subprocess
import ollama  # 導入官方庫
import shlex
import time

class SkillAgent:
    def __init__(self, model="gemma4:e4b", max_history=10):
        self.model = model
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.base_path = os.path.join(self.script_dir, "skills_system")
        self.index_file = os.path.join(self.base_path, "SKILLS.md")
        self.profile_file = os.path.join(self.script_dir, "AGENT.md")
        self.memory_file = os.path.join(self.script_dir, "Memory.md")

        # 預設工作目錄以程式所在位置為準，不寫死機器特定路徑
        self.current_cwd = self.script_dir
        self.container_cwd = ""

        self.messages = []
        self.max_history = max_history

        if not os.path.exists(self.index_file):
            raise FileNotFoundError(f"找不到技能索引：{self.index_file}")

        # 技能規格文件（OKF：Open Knowledge Format）目錄，每個技能一份 tools/<name>.md
        self.tools_dir = os.path.join(self.base_path, "tools")

        # =========================
        # 🧠 Token Tracker
        # =========================
        self.total_user_tokens = 0
        self.total_ai_tokens = 0
        self.total_tool_tokens = 0

        # =========================
        # 🎯 Sticky Objective
        # =========================
        self.sticky_objective = None

        # =========================
        # 📝 已核准、正在執行中的任務計畫（/plan 模式用）
        # 跟 sticky_objective 一樣塞進 system prompt，而不是放在 self.messages
        # 裡的一般訊息，這樣才不會被 _truncate_memory 的滑動視窗或
        # compress_context_to_file 的壓縮摘要沖掉，整個任務執行期間都在。
        # =========================
        self.current_plan = None

        # =========================
        # 📌 這一輪任務最原始的使用者敘述
        # 給獨立摘要 session（summarize_tool_result）在沒有 sticky_objective
        # 或 current_plan 可用時，當作「原始問題」聚焦摘要內容用，見
        # _build_task_anchor_text。每次使用者送出新任務時更新，/clear 時清空。
        # =========================
        self.current_task = None

    def count_tokens(self, text: str) -> int:
        """
        Ollama Native tokenizer (Gemma 4 e4b)
        """
        try:
            tokens = ollama.tokenize(model=self.model, prompt=text)
            return len(tokens)
        except Exception:
            # fallback
            return len(text) // 4

    def count_context_tokens(self) -> int:
        """
        計算整體 context tokens
        """
        full_text = ""
        for m in self.messages:
            full_text += f"{m['role']}: {m['content']}\n"
        return self.count_tokens(full_text)

    def compress_context_to_file(self, num_to_keep=2):
        history_to_compress = self.messages[1:]
        # 如果對話不足以壓縮，則直接返回
        if len(history_to_compress) <= num_to_keep:
            return
        # 切分：前面是要壓縮的，後面是保留的
        to_compress = history_to_compress[:-num_to_keep]
        to_keep = history_to_compress[-num_to_keep:]
        print(f"\n⏳ 正在壓縮 {len(to_compress)} 筆舊對話...")
        print("\n⏳ 正在壓縮上下文並歸檔...")

        # 1. 系統提示詞：定義「身分」與「嚴格的輸出格式規則」
        system_prompt = f"""你是一位專業的系統分析師。
你的任務是將對話內容總結為結構化的 Markdown 報告。
你必須嚴格遵守以下範本格式進行輸出，不得隨意增刪標題：

# Summary at {time.strftime('%Y-%m-%d_%H-%M-%S')}
這是一個 [簡短概述對話內容] 的流程對話。以下是報告：

---
### 💻 總體情境概述
[總結使用者目標與系統行為，描述當前情境]

### 📝 關鍵進度與目標（Key Progress）
1. [目標操作]
2. [目標路徑/相關參數]
3. [系統執行過的行動]
4. [下一步建議或當前狀態]

### ❌ 執行結果與錯誤（Execution Result & Errors）
| 類別 | 內容描述 | 具體錯誤訊息/結果 |
| :--- | :--- | :--- |
| [類別] | [描述] | [訊息/結果] |
"""

        # 2. 使用者提示詞：只包含要處理的「具體數據」
        user_prompt = f"""以下是需要總結的對話內容：
{to_compress}
"""
        # 使用 Ollama 進行摘要
        res = ollama.chat(model=self.model, 
                        messages=[
                            {'role': 'system', 'content': system_prompt},
                            {'role': 'user', 'content': user_prompt},],
                        options={'temperature': 0.2, 'num_ctx': 12288},
                        think=False
        )
        summary_content = res['message']['content']
        # 🔥 關鍵：重置對話時，保留 System Prompt + 我們想保留的最新對話
        self.messages = [self.messages[0]] + to_keep
        print(f"💾 [系統] 歷史已壓縮並存入。已保留最新的 {num_to_keep} 筆對話。")

        # 確保 logs 目錄存在
        log_dir = os.path.join(self.script_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        
        # 寫入獨立檔案
        timestamp = time.strftime('%Y-%m-%d_%H-%M-%S')
        file_path = os.path.join(log_dir, f"summary_{timestamp}.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"# Summary at {timestamp}\n\n{summary_content}")
            
        self.reset_conversation()
        print(f"💾 [系統] 歷史已壓縮並存入: {os.path.basename(file_path)}")

    def summarize_tool_result(self, result, tool_tokens):
        """比照 compress_context_to_file 的作法：開一個獨立、乾淨的一次性
        session（自己的 system/user prompt，不接觸 self.messages），專門
        把過大的工具回傳內容摘要成精簡版，摘要完就丟棄，不會留在主對話裡。

        這樣主 session 拿到的是「有意義的摘要」而不是單純的成功／失敗判定，
        同時不會因為把原始大量輸出直接塞進主上下文而干擾主 session 的推理。

        這個獨立 session 看不到主對話，因此另外附上 _build_task_anchor_text()
        取得的「使用者原始問題敘述」錨點，讓摘要聚焦在使用者真正在意的地方，
        避免因為不知道任務重點是什麼，而摘掉其實關鍵的資訊。
        """
        anchor = self._build_task_anchor_text()

        system_prompt = """你是一位專業的資料摘要助手。
你的任務是把一段指令執行後的原始輸出，摘要成精簡但保留關鍵資訊的版本，
交給另一個負責決策的 AI 使用，那個 AI 不會看到原始內容，只會看到你的摘要。

你會同時收到「使用者原始的任務／問題敘述」，這是你摘要時的聚焦依據：
跟這個任務有關、會影響下一步決策的資訊要優先保留。

規則：
- 必須保留：成功或失敗、關鍵數值、錯誤訊息、檔案／路徑名稱、數量等會影響下一步決策的資訊
- 可以捨棄：跟使用者任務無關的重複樣板文字、無關的排版細節
- 直接輸出摘要內容，不要加上「以下是摘要」之類的前言，也不要加你自己的建議
- 盡量控制在 200 字以內
"""
        user_prompt = (
            f"使用者原始的任務／問題敘述：\n{anchor}\n\n"
            f"以下是需要摘要的原始工具輸出（原始約 {tool_tokens} tokens）：\n\n{result}"
        )

        res = ollama.chat(
            model=self.model,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            options={'temperature': 0.2, 'num_ctx': 12288},
            think=False,
        )
        summary = res['message']['content'].strip()
        return (
            f"[tool result - AI 摘要]\n{summary}\n\n"
            f"(原始輸出約 {tool_tokens} tokens，完整內容已顯示在剛才的系統回傳訊息中)"
        )

    def load_recent_summary_logs(self, max_files=5):
        """讀取 logs 目錄下最新的幾個壓縮紀錄檔案，作為 Agent 的近期歷史知識"""
        log_dir = os.path.join(self.script_dir, "logs")
        if not os.path.exists(log_dir):
            return "No history logs found."

        # 取得所有 .md 檔案並依時間排序（新到舊）
        files = [os.path.join(log_dir, f) for f in os.listdir(log_dir) if f.endswith(".md")]
        files.sort(reverse=True)

        recent_files = files[:max_files]
        content = ""
        for f_path in recent_files:
            with open(f_path, "r", encoding="utf-8") as f:
                content += f"\n--- 紀錄檔: {os.path.basename(f_path)} ---\n{f.read()}\n"
        return content

    def load_long_term_memory(self, max_lines=30):
        if not os.path.exists(self.memory_file):
            return "No long-term memory."

        try:
            with open(self.memory_file, "r", encoding="utf-8") as f:
                lines = f.readlines()

            memory_lines = lines[-max_lines:]
            return "".join(memory_lines)

        except Exception as e:
            return f"[Memory Load Error] {e}"

    def build_plan_request(self, user_task):
        """/plan 模式用：把使用者的原始任務包裝成「先規劃、別執行」的請求。
        不需要另外把技能索引塞進來，因為 SKILLS.md 已經在系統提示詞裡，
        AI 本來就看得到，這裡只需要下達規劃指令即可。"""
        return f"""請先不要執行任何指令。請依照你目前看到的 SKILLS.md 技能索引，
針對下面的任務規劃出所需的步驟清單，列出來讓使用者確認後才會開始執行。

規則：
- 用條列式（1. 2. 3. ...）列出步驟，簡短清楚即可
- 每個步驟盡量標明會用到的技能名稱（來自 SKILLS.md），以及這步要做什麼
- 如果某步驟不需要任何技能，直接說明要做什麼即可
- 這一輪絕對不要輸出 EXECUTE: 指令，只列出計畫，等待使用者確認

任務：
{user_task}
"""

    def build_plan_revision_request(self, feedback):
        """/plan 模式用：使用者對計畫不滿意時，帶著回饋重新規劃一次。規則同上。"""
        return f"""使用者對你剛才列出的計畫有以下修改意見，請依照意見重新規劃一份新的步驟清單。
規則同上：只列出計畫、不要輸出 EXECUTE: 指令，等待使用者確認。

修改意見：
{feedback}
"""

    def _build_objective_prompt(self):
        """若有設定 sticky objective，組成提醒 AI 優先遵守的區塊；否則回傳空字串。"""
        if not self.sticky_objective:
            return ""

        return f"""
            ## CURRENT PRIMARY OBJECTIVE
            {self.sticky_objective}

            規則:
            - 你必須始終以此任務為最高優先級
            - 除非使用者明確清除 objective
            - 不可自行移除或遺忘
            - 當上下文過長時，優先維持此目標
            """

    def _build_plan_context_prompt(self):
        """若有已核准、正在執行中的任務計畫，組成提醒 AI 依計畫執行的區塊；
        否則回傳空字串。跟 _build_objective_prompt 同一種模式：每次組
        system prompt 都重新塞入，才不會被滑動視窗或壓縮摘要沖掉。"""
        if not self.current_plan:
            return ""

        return f"""
            ## CURRENT APPROVED TASK PLAN
            使用者已核准以下步驟計畫，請依計畫逐步執行：
            {self.current_plan}

            規則:
            - 依計畫逐步執行，一次只做一步，等系統回傳這一步的結果後再進行下一步
            - 除非使用者明確要求變更，否則不可自行更改或遺忘此計畫
            - 不會自動判斷「計畫已完成」而清除這個區塊；即使所有步驟都做完了，
              這裡仍會持續出現，直到使用者輸入 /plan done 手動清除為止
            """

    def _build_task_anchor_text(self):
        """決定要交給獨立摘要 session（summarize_tool_result）當作「使用者
        原始問題敘述」的錨點文字，讓摘要能聚焦在使用者真正在意的事情上，
        而不是對原始輸出做通用、不知道重點是什麼的精簡（容易先丟掉其實
        關鍵的資訊）。

        優先序：
        - sticky_objective：使用者主動設定、最高優先的任務錨點，本來就
          持續保留在 system prompt 裡，直接拿來用即可
        - current_plan：已核准的計畫本身就是原始任務拆解出的步驟清單；
          額外附上目前最新一則 assistant 回應（此回應是 AI 依計畫產生的，
          內容自然反映了目前執行到哪一步），讓摘要 session 能聚焦在「這
          一步」，而不是整份計畫
        - 兩者都沒有：退回這一輪任務使用者最原始輸入的文字（current_task）
        - 兩者都有：兩段一起給，不需要互斥判斷
        """
        parts = []
        if self.sticky_objective:
            parts.append(f"【使用者設定的最高優先 Objective】\n{self.sticky_objective}")

        if self.current_plan:
            last_assistant = next(
                (m['content'] for m in reversed(self.messages) if m['role'] == 'assistant'),
                None,
            )
            plan_section = f"【使用者已核准的任務計畫（依步驟拆解逐步執行）】\n{self.current_plan}"
            if last_assistant:
                plan_section += (
                    "\n\n【AI 剛才針對目前這一步的回應，可看出目前執行到哪一步】\n"
                    f"{last_assistant}"
                )
            parts.append(plan_section)

        if parts:
            return "\n\n".join(parts)

        return self.current_task or "(未取得使用者原始任務敘述)"

    def get_system_prompt(self):

        objective_prompt = self._build_objective_prompt()
        plan_prompt = self._build_plan_context_prompt()

        profile = ""
        if os.path.exists(self.profile_file):
            with open(self.profile_file, "r", encoding="utf-8") as f:
                profile = f.read()

        with open(self.index_file, "r", encoding="utf-8") as f:
            skills = f.read()

        memory_content = self.load_long_term_memory()
        history_summary = self.load_recent_summary_logs()

        status_prompt = f"\n\n## Current Agent State\n- CURRENT_WORKING_DIRECTORY: {self.current_cwd}\nCURRENT_CONTAINER_DIRECTORY: {self.container_cwd}"

        return f"""
                {profile}
                {status_prompt}
                {objective_prompt}
                {plan_prompt}
                ## Long Term Memory
                {memory_content}
                ## Recent Compressed History Summary
                {history_summary}
                ## Available Skills (SKILLS.md)
                {skills}
                """

    def reset_conversation(self):
        self.current_plan = None  # /clear 時一併清掉進行中的計畫，避免舊計畫殘留誤導新任務
        self.current_task = None  # 同上，避免舊任務敘述殘留誤導下一次的摘要 session
        self.messages = [{'role': 'system', 'content': self.get_system_prompt()}]

    def _truncate_memory(self):
        if len(self.messages) > self.max_history + 1:
            print(f"⚠️  [記憶優化] 啟動滑動視窗（保留 {self.max_history} 筆）")
            self.messages = [self.messages[0]] + self.messages[-self.max_history:]

    def ask_ai(self):
        try:
            if self.messages and self.messages[0]['role'] == 'system':
                self.messages[0]['content'] = self.get_system_prompt()

            self._truncate_memory()

            # 關閉 Ollama 的獨立 thinking 模式：此版本 Ollama 會把推理過程放進
            # message.thinking 欄位，而非像舊版把 <thought> 內嵌在 content 裡。
            # 若不關閉，模型有時會把整個決策都留在 thinking 裡，
            # 導致 content 回傳空字串（並非被截斷，而是模型判斷自己已經回答完畢）。
            # num_ctx：若不指定，Ollama 會用內建預設值（4096），而非模型實際支援的上限。
            # 4096 遠小於 TOKEN_THRESHOLD（見檔案下方），代表對話還沒到我們設計的
            # 壓縮門檻，Ollama 就已經在背後截斷最舊的內容，擠壓掉輸出可用的空間。
            # 這裡拉高到超過 TOKEN_THRESHOLD，並保留額外空間給模型的輸出。
            response = ollama.chat(
                model=self.model,
                messages=self.messages,
                options={'temperature': 0.2, 'num_ctx': 12288},
                think=False
            )

            raw_content = response['message']['content'].strip()

            if "<thought>" in raw_content:
                raw_content = raw_content.split("</thought>")[-1].strip()
            elif "...done thinking." in raw_content:
                raw_content = raw_content.split("...done thinking.")[-1].strip()

            return raw_content

        except Exception as e:
            return f"Ollama 連線錯誤: {e}"

    def _extract_execute_payload(self, ai_response):
        """從 AI 回應中取出 EXECUTE: 後面的內容；去除 code fence／註解殘留後為空則回傳 None。"""
        start_marker = "EXECUTE:"
        start_idx = ai_response.find(start_marker)
        payload = ai_response[start_idx + len(start_marker):].strip()

        if "```" in payload:
            payload = payload.split("```")[0].strip()
        if "# ---" in payload:
            payload = payload.split("# ---")[0].strip()

        return payload or None

    def _load_skill_doc(self, skill_name):
        """若 skill_name 對應到 tools/<skill_name>.md，回傳其內容；否則回傳 None。
        容忍 AI 直接照抄索引連結而帶上 .md 後綴（例如 list_dir.md）。"""
        if skill_name.endswith(".md"):
            skill_name = skill_name[:-3]
        doc_path = os.path.join(self.tools_dir, f"{skill_name}.md")
        if not os.path.exists(doc_path):
            return None
        with open(doc_path, "r", encoding="utf-8") as f:
            return f.read()

    def _normalize_script_name(self, raw_token):
        """確保腳本檔名以 _cmd.py 結尾（例如 cd -> cd_cmd.py，find_file.py -> find_file_cmd.py）。"""
        if not raw_token.endswith("_cmd.py") and not raw_token.endswith(".py"):
            return f"{raw_token}_cmd.py"
        if raw_token.endswith(".py") and not raw_token.endswith("_cmd.py"):
            return raw_token.replace(".py", "_cmd.py")
        return raw_token

    def _parse_script_args(self, script_name, remainder):
        """依腳本類型解析參數字串。manage_skill 的參數含 `|` 分隔符，需保留原始字串；
        其餘技能才用 shlex 依空白／引號拆分成參數列表。"""
        if "manage_skill" in script_name:
            return [remainder.strip()] if remainder else []
        try:
            return shlex.split(remainder)
        except Exception:
            return remainder.split()

    def _sync_state_from_tool_output(self, output_text):
        """解析工具輸出中的狀態標記，同步更新目前的工作目錄／容器目錄。"""
        lines = output_text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("[CWD_CHANGED]"):
                self.current_cwd = line.replace("[CWD_CHANGED]", "").strip()
            # 抓取 [CONTAINER_CWD] 標記的下一行作為路徑
            if line.startswith("[CONTAINER_CWD]") and i + 1 < len(lines):
                self.container_cwd = lines[i + 1].strip()

    def run_tool(self, ai_response):
        """解析 AI 回應中的 EXECUTE: 指令並執行，行為分兩種：

        1. 若目標字串對應到 tools/<name>.md 的技能規格文件（代表 AI 用的是
           SKILLS.md 索引裡的技能名稱），直接把規格文件內容當作系統回傳注入
           上下文，不執行任何腳本——這就是按需載入 (Progressive Disclosure)。
           不做技能名稱 -> 腳本檔名的猜測或對照；AI 讀完規格後，下一輪需改用
           規格書中標明的實際腳本路徑（例如 scripts/cd_cmd.py）才會真正執行。
        2. 否則將目標字串視為實際腳本路徑，執行對應的 CLI 腳本並回傳結果。
        """
        if "EXECUTE:" not in ai_response:
            return None

        try:
            payload = self._extract_execute_payload(ai_response)
            if not payload:
                return None

            parts = payload.split(maxsplit=1)
            raw_token = os.path.basename(parts[0])
            remainder = parts[1] if len(parts) > 1 else ""

            skill_doc = self._load_skill_doc(raw_token)
            if skill_doc is not None:
                print(f"📖 Agent 選擇技能索引: {raw_token}（載入規格文件，尚未執行）")
                return f"📘 已載入技能 '{raw_token}' 的規格文件（依此內容才可執行，請使用其中標明的實際腳本路徑）：\n{skill_doc}"

            script_name = self._normalize_script_name(raw_token)
            script_path = os.path.join(self.base_path, "scripts", script_name)

            print(f"🛠️  Agent 啟動工具: {script_name}")
            if not os.path.exists(script_path):
                return f"錯誤：找不到腳本 {script_path}"

            clean_args = self._parse_script_args(script_name, remainder)

            # --- 執行工具 ---
            # 將目前的容器路徑作為環境變數注入，讓 docker_run.py 讀取
            env = os.environ.copy()
            env["CONTAINER_CWD"] = self.container_cwd

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
                output_text = res.stdout.strip()
            else:
                # 腳本異常結束：優先用 stderr，沒有就用 stdout；兩者皆空也要給 AI 一個
                # 明確的失敗訊息，否則空字串會被誤判成「沒有工具需要執行」。
                # 統一補上 [ERROR] 前綴，讓 _content_for_context 能正確判定為失敗。
                output_text = res.stderr.strip() or res.stdout.strip() or "（沒有任何輸出）"
                if not output_text.startswith("[ERROR]"):
                    output_text = f"[ERROR] 腳本 {script_name} 異常結束（exit code {res.returncode}）:\n{output_text}"
            self._sync_state_from_tool_output(output_text)
            return output_text

        except Exception as e:
            return f"解析指令失敗: {e}"

# =========================================================
# 🚀 MAIN LOOP (加入 Token Tracking 顯示)
# =========================================================

def _append_discarded_tool_result(agent):
    """使用者選擇不把工具結果加入上下文時，仍需告知 AI「工具已執行完畢」，
    避免它誤以為指令根本沒被處理而重複嘗試。"""
    agent.messages.append({
        'role': 'user',
        'content': "[tool result]\nTool execution completed, but the result was discarded by user request."
    })

def _run_plan_flow(agent, user_task):
    """/plan 模式：先讓 AI 依 SKILLS.md 規劃步驟、印出來給使用者看，
    使用者核准後才讓 main() 的主迴圈開始真正執行（ask_ai -> run_tool）。

    安全設計：規劃階段自始至終不會呼叫 agent.run_tool()，就算模型不聽話
    在計畫裡夾帶了 EXECUTE: 指令也不會被執行——確認關卡是靠「這個函式
    根本不執行工具」保證的，不依賴模型是否遵守「先不要執行」的指示。

    回傳 True 代表使用者已核准，main() 可以繼續往下進入正常執行迴圈；
    回傳 False 代表使用者取消，本次任務到此為止，不會呼叫任何工具。
    """
    agent.messages.append({'role': 'user', 'content': agent.build_plan_request(user_task)})

    while True:
        plan_msg = agent.ask_ai()
        agent.total_ai_tokens += agent.count_tokens(plan_msg)
        agent.messages.append({'role': 'assistant', 'content': plan_msg})
        print(f"\n📝 AI 規劃的任務計畫:\n{'-'*30}\n{plan_msg}\n{'-'*30}")

        choice = input("\n是否核准此計畫並開始執行？(y=核准 / n=取消 / 直接輸入修改意見=重新規劃): ").strip()

        if choice.lower() == 'y':
            agent.current_plan = plan_msg
            agent.messages.append({
                'role': 'user',
                'content': "[PLAN_CONFIRMED]\n使用者已核准上述計畫，現在開始依計畫執行第一個步驟。"
            })
            return True

        if choice == "" or choice.lower() in ('n', 'no'):
            agent.messages.append({
                'role': 'user',
                'content': "[PLAN_REJECTED]\n使用者取消了上述計畫，本次任務不會執行，請等待使用者的新指示。"
            })
            print("🚫 已取消，本次任務不會執行。")
            return False

        # 其餘輸入視為修改意見，重新規劃一次
        agent.messages.append({'role': 'user', 'content': agent.build_plan_revision_request(choice)})


# 整體上下文超過此 token 數時，自動觸發壓縮並歸檔（見 compress_context_to_file）。
TOKEN_THRESHOLD = 8000

# 單一工具回傳內容的 token 門檻：超過此值時，不會把完整原始內容塞進 AI 的
# 上下文（避免一次搜尋/列目錄的大量輸出把 context 灌爆、干擾推理），而是
# 改用精簡的「成功／失敗」摘要餵給 AI，讓它的推理流程保持穩定；完整內容
# 仍會顯示給使用者（CLI 印出、或 web_console 的系統/工具回傳面板）。
TOOL_RESULT_TOKEN_THRESHOLD = 250

# 單一工具腳本的總逾時（秒）：harness 的最後防線。各腳本自身應設定更短的逾時
# （容器類腳本可調的上限 570 秒就是為了低於這個值），這裡只處理腳本本身卡死
# （例如程序內無法中斷的重運算、讀取無回應的裝置）的情況，避免整個 Agent
# （CLI 與 Web Console 都在同一條執行緒上等待工具）被無限期卡住。
TOOL_EXEC_TIMEOUT = 600

def _content_for_context(result, tool_tokens, agent=None, use_summary=False):
    """決定要餵給 AI 上下文的內容：正常大小就原封不動放進去。超過
    TOOL_RESULT_TOKEN_THRESHOLD 時有兩種精簡方式：

    - use_summary=False（預設）：原本的行為，只回傳成功／失敗的判定，
      不需要額外呼叫模型，穩定、零延遲。
    - use_summary=True 且提供 agent：改用 agent.summarize_tool_result()
      開一個獨立 session 做語意摘要，讓主 session 拿到的不只是成功/失敗，
      還有內容重點。此為可選功能，摘要 session 若失敗會自動退回成功/失敗
      判定，不會讓主 session 的推理流程中斷。
    """
    if tool_tokens <= TOOL_RESULT_TOKEN_THRESHOLD:
        return result

    if use_summary and agent is not None:
        try:
            return agent.summarize_tool_result(result, tool_tokens)
        except Exception as e:
            print(f"⚠️ 摘要 session 執行失敗，改用精簡成功/失敗判定：{e}")

    status = "失敗" if result.lstrip().startswith("[ERROR]") else "成功"
    return (
        f"[tool result - 已精簡]\n"
        f"指令已{status}執行（原始輸出約 {tool_tokens} tokens，超過門檻 "
        f"{TOOL_RESULT_TOKEN_THRESHOLD}）。完整內容已顯示在剛才的系統回傳訊息中，"
        f"未直接加入上下文，以維持推理穩定。"
    )

def main():
    agent = SkillAgent(
        model="gemma4:e4b",
        max_history=30
    )
    agent.reset_conversation()
    auto_mode = False
    hybrid_mode = False  # 👈 新增狀態
    tool_summary_mode = False  # 👈 工具回傳超過門檻時，是否改用獨立 session 做語意摘要
    plan_mode = False  # 👈 開啟後，每個新任務都要先規劃、經使用者核准才會執行

    print("\n" + "="*50)
    print("V6 Robot Agent + Token Tracker 已啟動")
    print("="*50)

    while True:
        try:
            user_msg = input("\n👤 使用者: ")
            
            # --- 基礎指令 ---
            if user_msg.lower() in ['exit', 'quit']:
                break
            if user_msg.lower() == '/clear':
                agent.reset_conversation()
                print("🧹 記憶已清空。")
                continue

            # --- 新增：手動壓縮指令 ---
            if user_msg.lower() == '/compress':
                agent.compress_context_to_file(num_to_keep=2)
                print("🗜️ 歷史已手動壓縮並歸檔。")
                continue
            
            # --- 模式切換指令 ---
            if user_msg.lower() == '/auto on':
                auto_mode = True
                print("🤖 已開啟 Auto Continue 模式")
                continue
            if user_msg.lower() == '/auto off':
                auto_mode = False
                print("🛑 已關閉 Auto Continue 模式")
                continue
            if user_msg.lower() == '/hybrid on':
                hybrid_mode = True
                print("🧬 已開啟 Hybrid Mode")
                continue
            if user_msg.lower() == '/hybrid off':
                hybrid_mode = False
                print("🧬 已關閉 Hybrid Mode")
                continue
            if user_msg.lower() == '/summarize on':
                tool_summary_mode = True
                print("🧠 已開啟工具回傳摘要模式（超過門檻的結果會由獨立 session 摘要後再交給主對話）")
                continue
            if user_msg.lower() == '/summarize off':
                tool_summary_mode = False
                print("🧠 已關閉工具回傳摘要模式（超過門檻的結果改回精簡成功/失敗判定）")
                continue
            if user_msg.lower() == '/plan on':
                plan_mode = True
                print("📝 已開啟 Plan 模式（新任務會先規劃步驟，經你核准後才會執行）")
                continue
            if user_msg.lower() == '/plan off':
                plan_mode = False
                print("📝 已關閉 Plan 模式（恢復直接執行）")
                continue
            if user_msg.lower() == '/plan done':
                if agent.current_plan:
                    agent.current_plan = None
                    print("✅ 已清除目前進行中的計畫（system prompt 不再提醒 AI 依計畫執行）")
                else:
                    print("ℹ️ 目前沒有進行中的計畫")
                continue

            # --- 🎯 OBJECTIVE 設定 ---
            if user_msg.lower() == "objective set":
                print("\n🎯 進入任務錨點設定模式 (輸入 objective end 結束)")
                objective_lines = []
                while True:
                    line = input("OBJECTIVE >> ")
                    if line.lower() == "objective end": break
                    objective_lines.append(line)
                agent.sticky_objective = "\n".join(objective_lines).strip()
                continue
            if user_msg.lower() == "objective show":
                print(f"\n🎯 Current: {agent.sticky_objective or 'None'}")
                continue
            if user_msg.lower() == "objective clear":
                agent.sticky_objective = ""
                print("🧹 已清除 Sticky Objective")
                continue

            # --- 📥 USER TOKEN 計算 ---
            user_tokens = agent.count_tokens(user_msg)
            agent.total_user_tokens += user_tokens
            print(f"📥 User Tokens: {user_tokens}")

            # 記錄這一輪任務最原始的使用者敘述，供獨立摘要 session 在沒有
            # objective／plan 可用時，當作「原始問題」聚焦摘要內容
            # （見 _build_task_anchor_text）
            agent.current_task = user_msg

            # --- 📝 PLAN 模式：先規劃、經使用者核准才進入下面的執行迴圈 ---
            if plan_mode:
                if not _run_plan_flow(agent, user_msg):
                    continue  # 使用者取消了計畫，回到最上層等待新的輸入
            else:
                agent.messages.append({'role': 'user', 'content': user_msg})

            while True:
                # --- 🧠 AI 推論 ---
                ai_msg = agent.ask_ai()
                ai_tokens = agent.count_tokens(ai_msg)
                agent.total_ai_tokens += ai_tokens
                print(f"\n🧠 AI:\n{'-'*30}\n{ai_msg}\n{'-'*30}")
                print(f"📤 AI Tokens: {ai_tokens}")
                agent.messages.append({'role': 'assistant', 'content': ai_msg})

                # --- 🧰 TOOL 執行 ---
                result = agent.run_tool(ai_msg)
                tool_tokens = 0
                if result:
                    tool_tokens = agent.count_tokens(result)
                    agent.total_tool_tokens += tool_tokens
                    print(f"\n🚀 系統回傳:\n{'-'*30}\n{result}\n{'-'*30}")
                    print(f"🧰 Tool Tokens: {tool_tokens}")
                    if tool_tokens > TOOL_RESULT_TOKEN_THRESHOLD:
                        print(f"⚠️ 此工具回傳約 {tool_tokens} tokens，超過門檻 {TOOL_RESULT_TOKEN_THRESHOLD}，"
                              f"加入上下文時將改用精簡摘要。")
                else:
                    print("✅ 無工具需要執行")

                # --- 📊 TOKEN 統計顯示 ---
                full_context = agent.get_system_prompt() + "\n" + "".join([f"{m['role']}: {m['content']}\n" for m in agent.messages])
                print(f"\n📦 Context Tokens: {agent.count_tokens(full_context)}")
                # --- 自動壓縮觸發器 ---
                if agent.count_tokens(full_context) > TOKEN_THRESHOLD:
                    agent.compress_context_to_file(num_to_keep=2)
                    print(f"\n📦 Compressed Context Tokens: {agent.count_tokens(full_context)}")
                    continue # 壓縮後重新循環，確保下一輪 Agent 讀取到更新後的 system prompt
                print(f"📊 Stats | User: {agent.total_user_tokens} | AI: {agent.total_ai_tokens} | Tool: {agent.total_tool_tokens}")

                # --- 模式判定流程 ---
                if not result:
                    break

                # 1. Auto Mode
                if auto_mode:
                    agent.messages.append({'role': 'user', 'content': f"[tool result]\n{_content_for_context(result, tool_tokens, agent=agent, use_summary=tool_summary_mode)}"})
                    print("♻️ Auto Continue 中...")
                    continue

                # 2. Hybrid Mode
                if hybrid_mode:
                    choice = input("\n🤔 Hybrid Mode - 加入上下文？(y/n): ").lower()
                    if choice == 'y':
                        agent.messages.append({'role': 'user', 'content': f"[tool result]\n{_content_for_context(result, tool_tokens, agent=agent, use_summary=tool_summary_mode)}"})
                        continue
                    else:
                        _append_discarded_tool_result(agent)
                        print("🚫 該結果已被略過 (已告知 Agent 執行結束)")
                        # 這裡不使用 break，讓 AI 根據這個「工具執行完畢」的資訊繼續推論
                        continue

                # 3. Manual Mode
                choice = input("\n是否將系統結果加入上下文？(y/n/stop): ").lower()
                if choice == 'y':
                    agent.messages.append({'role': 'user', 'content': f"【系統執行結果】:\n{_content_for_context(result, tool_tokens, agent=agent, use_summary=tool_summary_mode)}"})
                elif choice == 'stop':
                    break
                else:
                    _append_discarded_tool_result(agent)
                    print("👀 已略過")
                    break

        except KeyboardInterrupt:
            print("\n👋 Bye")
            break

if __name__ == "__main__":
    main()