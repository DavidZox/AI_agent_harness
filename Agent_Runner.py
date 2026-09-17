import os
import sys
import subprocess
import ollama  # 導入官方庫
import re
import shlex
import time

# 技能「顯示名稱」與實際腳本檔名不一致的少數歷史案例；其餘技能（含所有自我進化新建的技能）
# 皆遵循 <名稱>_cmd.py 規則，可直接推導，不需列在此處。
SKILL_NAME_TO_SCRIPT = {
    "list_dir": "ls_cmd.py",
    "search_text": "grep_cmd.py",
    "change_dir": "cd_cmd.py",
    "view_file": "cat_cmd.py",
}

NEED_TOOL_LOOP_LIMIT = 5  # 連續 NEED_TOOL（含重複命中快取）達此次數即強制中斷交還使用者


class SkillAgent:
    def __init__(self, model="gemma4:e4b", max_history=10):
        self.model = model
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.base_path = os.path.join(self.script_dir, "skills_system")
        self.index_file = os.path.join(self.base_path, "INDEX.md")
        self.tools_dir = os.path.join(self.base_path, "tools")
        self.profile_file = os.path.join(self.script_dir, "ROBOT_AGENT.md")

        self.memory_dir = os.path.join(self.script_dir, "memory")
        self.memory_files = {
            "semantic": os.path.join(self.memory_dir, "semantic.md"),
            "episodic": os.path.join(self.memory_dir, "episodic.md"),
            "procedural": os.path.join(self.memory_dir, "procedural.md"),
        }

        self.current_cwd = "/home/david"
        self.container_cwd = ""

        self.messages = []
        self.max_history = max_history

        if not os.path.exists(self.index_file):
            raise FileNotFoundError(f"找不到技能索引：{self.index_file}")

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
        # 📚 On-Demand Skill Loading (NEED_TOOL)
        # =========================
        self.loaded_tools = set()      # 技能顯示名稱（INDEX.md / tools/*.md 用）
        self.loaded_scripts = set()    # 實際腳本檔名（run_tool 解析用）
        self.consecutive_need_tool_count = 0

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
        res = ollama.chat(model=self.model, messages=[
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_prompt}
        ])
        summary_content = res['message']['content']
        # 🔥 關鍵：重置對話時，保留 System Prompt + 我們想保留的最新對話
        self.messages = [self.messages[0]] + to_keep
        print(f"💾 [系統] 歷史已壓縮並存入。已保留最新的 {num_to_keep} 筆對話。")

        # 🔥 壓縮後，先前載入的技能規格書內容已不在對話中，清空快取避免誤判「已載入」
        self.loaded_tools.clear()
        self.loaded_scripts.clear()
        self.consecutive_need_tool_count = 0

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

    def load_long_term_memory(self, max_lines=15):
        """讀取語意／情節／程序三種長期記憶檔案的尾段內容"""
        sections = [
            ("semantic", "### 語意記憶 (Semantic)"),
            ("episodic", "### 情節記憶 (Episodic)"),
            ("procedural", "### 程序記憶 (Procedural)"),
        ]
        blocks = []
        for mem_type, title in sections:
            path = self.memory_files[mem_type]
            if not os.path.exists(path):
                blocks.append(f"{title}\nNo memory yet.")
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                blocks.append(f"{title}\n" + "".join(lines[-max_lines:]).strip())
            except Exception as e:
                blocks.append(f"{title}\n[Memory Load Error] {e}")
        return "\n\n".join(blocks)

    def get_system_prompt(self):

        objective_prompt = ""
        if self.sticky_objective:
            objective_prompt = f"""
            ## CURRENT PRIMARY OBJECTIVE
            {self.sticky_objective}

            規則:
            - 你必須始終以此任務為最高優先級
            - 除非使用者明確清除 objective
            - 不可自行移除或遺忘
            - 當上下文過長時，優先維持此目標
            """

        profile = ""
        if os.path.exists(self.profile_file):
            with open(self.profile_file, "r", encoding="utf-8") as f:
                profile = f.read()

        with open(self.index_file, "r", encoding="utf-8") as f:
            skills = f.read()
            memory_content = self.load_long_term_memory()
            history_summary = self.load_recent_summary_logs()

        loaded_display = sorted(self.loaded_tools) if self.loaded_tools else "None"
        status_prompt = (
            f"\n\n## Current Agent State\n"
            f"- CURRENT_WORKING_DIRECTORY: {self.current_cwd}\n"
            f"- CURRENT_CONTAINER_DIRECTORY: {self.container_cwd}\n"
            f"- LOADED_TOOL_SPECS_THIS_SESSION: {loaded_display}"
        )

        return f"""
                {profile}
                {status_prompt}
                {objective_prompt}
                ## Long Term Memory
                {memory_content}
                ## Recent Compressed History Summary
                {history_summary}
                ## Available Skills (INDEX.md)
                {skills}
                """

    def reset_conversation(self):
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

            response = ollama.chat(
                model=self.model,
                messages=self.messages,
                options={'temperature': 0.2}
            )

            raw_content = response['message']['content'].strip()

            if "<thought>" in raw_content:
                raw_content = raw_content.split("</thought>")[-1].strip()
            elif "...done thinking." in raw_content:
                raw_content = raw_content.split("...done thinking.")[-1].strip()

            return raw_content

        except Exception as e:
            return f"Ollama 連線錯誤: {e}"

    def check_need_tool(self, ai_response):
        """
        解析 `NEED_TOOL: <技能名稱>`，命中時載入 skills_system/tools/<name>.md 全文，
        是本次「隨需載入」重構的核心：技能規格書預設不在 context 裡，只有被明確請求時才載入。

        只看第一行（刻意不沿用 EXECUTE 的 split(maxsplit=1) 寫法 —— 那種寫法在模型於指令後
        面接著輸出額外說明文字、且沒有用 code fence 包起來時，會把說明文字一併吃進參數）。
        回傳 None 代表本次回覆沒有 NEED_TOOL 宣告。
        """
        stripped = ai_response.strip()
        if not stripped:
            return None

        first_line = stripped.splitlines()[0]
        if "NEED_TOOL:" not in first_line:
            return None

        raw_name = first_line.split("NEED_TOOL:", 1)[1].strip()
        raw_name = raw_name.strip("`\"' ")

        # 安全防護：僅允許英數字與底線並取 basename，避免路徑穿越字串（與 grep_cmd.py/find_file_cmd.py
        # 阻擋 "/" 搜尋的既有防護風格一致）
        safe_name = os.path.basename(raw_name)
        if not re.fullmatch(r"[A-Za-z0-9_]+", safe_name):
            return f"[ERROR] 不合法的技能名稱: {raw_name}"

        if safe_name in self.loaded_tools:
            return f"[INFO] 技能 '{safe_name}' 規格書本次對話已載入，無需重複讀取。"

        doc_path = os.path.join(self.tools_dir, f"{safe_name}.md")
        if not os.path.exists(doc_path):
            return f"[ERROR] 找不到技能 '{safe_name}' 的規格書，請確認 INDEX.md 中的技能名稱是否正確。"

        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.loaded_tools.add(safe_name)
        script_name = SKILL_NAME_TO_SCRIPT.get(safe_name, f"{safe_name}_cmd.py")
        self.loaded_scripts.add(script_name)

        return f"[PASS] 已載入技能 '{safe_name}' 規格書：\n{content}"

    def run_tool(self, ai_response):
        if "EXECUTE:" in ai_response:
            try:
                start_marker = "EXECUTE:"
                start_idx = ai_response.find(start_marker)
                full_content = ai_response[start_idx + len(start_marker):].strip()

                if "```" in full_content:
                    full_content = full_content.split("```")[0].strip()
                if "# ---" in full_content:
                    full_content = full_content.split("# ---")[0].strip()
                if not full_content:
                    return None

                parts = full_content.split(maxsplit=1)
                script_name = os.path.basename(parts[0])

                if not script_name.endswith("_cmd.py") and not script_name.endswith(".py"):
                    script_name = f"{script_name}_cmd.py"
                elif script_name.endswith(".py") and not script_name.endswith("_cmd.py"):
                    script_name = script_name.replace(".py", "_cmd.py")

                script_path = os.path.join(self.base_path, "scripts", script_name)

                # 參數處理
                if "manage_skill" in script_name:
                    clean_args = [parts[1].strip()] if len(parts) > 1 else []
                else:
                    remaining_args = parts[1] if len(parts) > 1 else ""
                    try:
                        clean_args = shlex.split(remaining_args)
                    except Exception:
                        clean_args = remaining_args.split()

                print(f"🛠️  Agent 啟動工具: {script_name}")
                if not os.path.exists(script_path):
                    return f"錯誤：找不到腳本 {script_path}"

                # --- 執行工具 ---
                # 將目前的容器路徑作為環境變數注入，讓 docker_run.py 讀取
                env = os.environ.copy()
                env["CONTAINER_CWD"] = self.container_cwd

                res = subprocess.run(
                    [sys.executable, script_path] + clean_args,
                    capture_output=True,
                    text=True,
                    cwd=self.current_cwd,
                    env=env
                )

                output_text = res.stdout.strip() if res.returncode == 0 else res.stderr
                lines = output_text.splitlines()

                # --- 狀態同步邏輯 ---
                for i, line in enumerate(lines):
                    if line.startswith("[CWD_CHANGED]"):
                        self.current_cwd = line.replace("[CWD_CHANGED]", "").strip()

                    # 抓取 [CONTAINER_CWD] 標記的下一行作為路徑
                    if line.startswith("[CONTAINER_CWD]"):
                        if i + 1 < len(lines):
                            self.container_cwd = lines[i + 1].strip()

                # --- 自我進化技能：於父行程中同步註冊 ---
                # manage_skill_cmd.py / manager.py 在子行程中執行，無法直接改動本行程的
                # loaded_tools / loaded_scripts，因此改由父行程根據同一份原始輸入解析新技能名稱。
                if "manage_skill" in script_name and res.returncode == 0 and clean_args:
                    new_name = clean_args[0].split("|", 1)[0].strip()
                    if new_name and re.fullmatch(r"[A-Za-z0-9_]+", new_name):
                        self.loaded_tools.add(new_name)
                        self.loaded_scripts.add(f"{new_name}_cmd.py")

                # --- 未先 NEED_TOOL 就 EXECUTE：軟性提醒，不阻擋 ---
                # NEED_TOOL 是省 token 的機制，不是權限控管，因此絕不因未載入規格書而拒絕執行。
                if script_name not in self.loaded_scripts:
                    output_text = (
                        "[INFO] 提醒：尚未透過 NEED_TOOL 載入此技能的規格書，仍已依既有邏輯執行下方結果。\n"
                        f"{output_text}"
                    )

                return output_text

            except Exception as e:
                return f"解析指令失敗: {e}"

        return None

# =========================================================
# 🚀 MAIN LOOP (加入 Token Tracking 顯示 + NEED_TOOL 隨需載入)
# =========================================================

def main():
    agent = SkillAgent(
        model="gemma4:e4b",
        max_history=30
    )
    agent.reset_conversation()
    auto_mode = False
    hybrid_mode = False  # 👈 新增狀態

    print("\n" + "="*50)
    print("V7 Robot Agent + On-Demand Skill Loading 已啟動")
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
            agent.messages.append({'role': 'user', 'content': user_msg})

            while True:
                # --- 🧠 AI 推論 ---
                ai_msg = agent.ask_ai()
                ai_tokens = agent.count_tokens(ai_msg)
                agent.total_ai_tokens += ai_tokens
                print(f"\n🧠 AI:\n{'-'*30}\n{ai_msg}\n{'-'*30}")
                print(f"📤 AI Tokens: {ai_tokens}")
                agent.messages.append({'role': 'assistant', 'content': ai_msg})

                # --- 📚 NEED_TOOL 優先於 EXECUTE 判斷 ---
                # 若模型同一回覆同時輸出兩者（小模型偶爾會如此），本次只處理 NEED_TOOL，
                # EXECUTE 留待下一輪（載入規格書後）再次輸出，避免在還沒讀規格前就執行。
                tool_doc = agent.check_need_tool(ai_msg)
                is_need_tool = tool_doc is not None

                if is_need_tool:
                    result = tool_doc
                    agent.consecutive_need_tool_count += 1
                else:
                    result = agent.run_tool(ai_msg)
                    agent.consecutive_need_tool_count = 0

                # --- 🧰 執行結果／規格書 顯示 ---
                if result:
                    tool_tokens = agent.count_tokens(result)
                    agent.total_tool_tokens += tool_tokens
                    label = "📚 技能規格書" if is_need_tool else "🚀 系統回傳"
                    print(f"\n{label}:\n{'-'*30}\n{result}\n{'-'*30}")
                    print(f"🧰 Tool Tokens: {tool_tokens}")
                else:
                    print("✅ 無工具需要執行")

                # --- 📊 TOKEN 統計顯示 ---
                full_context = agent.get_system_prompt() + "\n" + "".join([f"{m['role']}: {m['content']}\n" for m in agent.messages])
                print(f"\n📦 Context Tokens: {agent.count_tokens(full_context)}")
                # --- 自動壓縮觸發器 ---
                TOKEN_THRESHOLD = 8000 # 根據你的需求調整
                if agent.count_tokens(full_context) > TOKEN_THRESHOLD:
                    agent.compress_context_to_file(num_to_keep=2)
                    print(f"\n📦 Compressed Context Tokens: {agent.count_tokens(full_context)}")
                    continue # 壓縮後重新循環，確保下一輪 Agent 讀取到更新後的 system prompt
                print(f"📊 Stats | User: {agent.total_user_tokens} | AI: {agent.total_ai_tokens} | Tool: {agent.total_tool_tokens}")

                # --- 模式判定流程 ---
                if not result:
                    break

                # 0. NEED_TOOL：唯讀的規格書載入動作，一律自動繼續，不受 auto/hybrid/manual 影響
                if is_need_tool:
                    agent.messages.append({'role': 'user', 'content': f"[tool spec]\n{result}"})
                    if agent.consecutive_need_tool_count > NEED_TOOL_LOOP_LIMIT:
                        print(f"\n🛑 連續 NEED_TOOL 已達 {NEED_TOOL_LOOP_LIMIT} 次，強制中斷並交還使用者確認。")
                        break
                    print("♻️ 已載入規格書，自動繼續...")
                    continue

                # 1. Auto Mode
                if auto_mode:
                    agent.messages.append({'role': 'user', 'content': f"[tool result]\n{result}"})
                    print("♻️ Auto Continue 中...")
                    continue

                # 2. Hybrid Mode
                if hybrid_mode:
                    choice = input("\n🤔 Hybrid Mode - 加入上下文？(y/n): ").lower()
                    if choice == 'y':
                        agent.messages.append({'role': 'user', 'content': f"[tool result]\n{result}"})
                        continue
                    else:
                        # 🔥 關鍵修正：回傳「工具已執行，但結果被隱藏」的訊息給 Agent
                        agent.messages.append({
                            'role': 'user',
                            'content': "[tool result]\nTool execution completed, but the result was discarded by user request."
                        })
                        print("🚫 該結果已被略過 (已告知 Agent 執行結束)")
                        # 這裡不使用 break，讓 AI 根據這個「工具執行完畢」的資訊繼續推論
                        continue

                # 3. Manual Mode
                choice = input("\n是否將系統結果加入上下文？(y/n/stop): ").lower()
                if choice == 'y':
                    agent.messages.append({'role': 'user', 'content': f"【系統執行結果】:\n{result}"})
                elif choice == 'stop':
                    break
                else:
                # 🔥 關鍵修正：回傳「工具已執行，但結果被隱藏」的訊息給 Agent
                    agent.messages.append({
                        'role': 'user',
                        'content': "[tool result]\nTool execution completed, but the result was discarded by user request."
                    })
                    print("👀 已略過")
                    break

        except KeyboardInterrupt:
            print("\n👋 Bye")
            break

if __name__ == "__main__":
    main()
