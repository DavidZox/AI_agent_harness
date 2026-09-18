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

        self.current_cwd = "/home/david"
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

    def get_system_prompt(self):

        objective_prompt = self._build_objective_prompt()

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
                ## Long Term Memory
                {memory_content}
                ## Recent Compressed History Summary
                {history_summary}
                ## Available Skills (SKILLS.md)
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

            # 關閉 Ollama 的獨立 thinking 模式：此版本 Ollama 會把推理過程放進
            # message.thinking 欄位，而非像舊版把 <thought> 內嵌在 content 裡。
            # 若不關閉，模型有時會把整個決策都留在 thinking 裡，
            # 導致 content 回傳空字串（並非被截斷，而是模型判斷自己已經回答完畢）。
            response = ollama.chat(
                model=self.model,
                messages=self.messages,
                options={'temperature': 0.2},
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
        """若 skill_name 對應到 tools/<skill_name>.md，回傳其內容；否則回傳 None。"""
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

            res = subprocess.run(
                [sys.executable, script_path] + clean_args,
                capture_output=True,
                text=True,
                cwd=self.current_cwd,
                env=env
            )

            output_text = res.stdout.strip() if res.returncode == 0 else res.stderr
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


# 整體上下文超過此 token 數時，自動觸發壓縮並歸檔（見 compress_context_to_file）。
TOKEN_THRESHOLD = 8000

# 單一工具回傳內容的 token 門檻：超過此值時，不會把完整原始內容塞進 AI 的
# 上下文（避免一次搜尋/列目錄的大量輸出把 context 灌爆、干擾推理），而是
# 改用精簡的「成功／失敗」摘要餵給 AI，讓它的推理流程保持穩定；完整內容
# 仍會顯示給使用者（CLI 印出、或 web_console 的系統/工具回傳面板）。
TOOL_RESULT_TOKEN_THRESHOLD = 250


def _content_for_context(result, tool_tokens):
    """決定要餵給 AI 上下文的內容：正常大小就原封不動放進去；超過
    TOOL_RESULT_TOKEN_THRESHOLD 則改用精簡摘要，同時仍讓 AI 知道指令本身
    是成功還是失敗，避免推理過程被誤導或被大量原始輸出干擾。"""
    if tool_tokens <= TOOL_RESULT_TOKEN_THRESHOLD:
        return result

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
                    agent.messages.append({'role': 'user', 'content': f"[tool result]\n{_content_for_context(result, tool_tokens)}"})
                    print("♻️ Auto Continue 中...")
                    continue

                # 2. Hybrid Mode
                if hybrid_mode:
                    choice = input("\n🤔 Hybrid Mode - 加入上下文？(y/n): ").lower()
                    if choice == 'y':
                        agent.messages.append({'role': 'user', 'content': f"[tool result]\n{_content_for_context(result, tool_tokens)}"})
                        continue
                    else:
                        _append_discarded_tool_result(agent)
                        print("🚫 該結果已被略過 (已告知 Agent 執行結束)")
                        # 這裡不使用 break，讓 AI 根據這個「工具執行完畢」的資訊繼續推論
                        continue

                # 3. Manual Mode
                choice = input("\n是否將系統結果加入上下文？(y/n/stop): ").lower()
                if choice == 'y':
                    agent.messages.append({'role': 'user', 'content': f"【系統執行結果】:\n{_content_for_context(result, tool_tokens)}"})
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