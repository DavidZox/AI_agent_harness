import os
import sys
import subprocess
import ollama  # 導入官方庫
import re
import shlex
import time
import threading

# 技能「顯示名稱」與實際腳本檔名不一致的少數歷史案例；其餘技能（含所有自我進化新建的技能）
# 皆遵循 <名稱>_cmd.py 規則，可直接推導，不需列在此處。
SKILL_NAME_TO_SCRIPT = {
    "list_dir": "ls_cmd.py",
    "search_text": "grep_cmd.py",
    "change_dir": "cd_cmd.py",
    "view_file": "cat_cmd.py",
}

NEED_TOOL_LOOP_LIMIT = 5  # 連續 NEED_TOOL（含重複命中快取）達此次數即強制中斷交還使用者
DEFAULT_TOKEN_THRESHOLD = 8000  # 上下文預算閾值預設值，可由 SkillAgent(token_threshold=...) 或 /budget 指令調整
DEFAULT_TOOL_IDLE_EVICTION_TURNS = 6  # 技能規格書連續閒置（未被 EXECUTE）幾輪後從上下文清除
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 300  # 心跳預設間隔（5 分鐘），可由 /heartbeat interval 調整
# 心跳固定唯讀健檢清單：(顯示標籤, 腳本檔名, 固定參數列表)。只跑不需要即時情境參數的唯讀技能，
# 不經過 LLM 推論、不自動寫入記憶——這是刻意的安全邊界，見 ROBOT_AGENT.md 的 Memory Write Protocol
# （記憶寫入必須由使用者明確要求，心跳沒有人在場確認，不適用）。
DEFAULT_HEARTBEAT_CHECKS = [
    ("robot_ping", "robot_ping_cmd.py", []),
]


class SkillAgent:
    def __init__(self, model="gemma4:e4b", max_history=10, token_threshold=DEFAULT_TOKEN_THRESHOLD,
                 tool_idle_eviction_turns=DEFAULT_TOOL_IDLE_EVICTION_TURNS,
                 heartbeat_interval=DEFAULT_HEARTBEAT_INTERVAL_SECONDS, heartbeat_checks=None):
        """
        建立一個 SkillAgent 執行個體，初始化本次對話會用到的所有路徑與狀態。

        會計算並固定住專案內各關鍵檔案／目錄的絕對路徑（skills_system 下的
        INDEX.md／tools/、ROBOT_AGENT.md、memory/ 三檔），並在 INDEX.md 不存在時
        直接拋出 FileNotFoundError（技能索引是整個系統運作的前提，不應該悄悄跑
        起來卻沒有任何技能可用）。

        同時初始化四組彼此獨立的狀態：token 統計（user/ai/tool 三種累計數）、
        sticky objective（/objective 設定的核心目標）、隨需載入技能的追蹤集合
        （loaded_tools/loaded_scripts 等，見 check_need_tool()/evict_idle_tools()）、
        以及心跳背景執行緒的旗標與設定（見 start_heartbeat()）。

        參數:
            model: Ollama 模型名稱。
            max_history: _truncate_memory() 滑動視窗保留的最大訊息數。
            token_threshold: enforce_context_budget() 的預算閾值（token 數）。
            tool_idle_eviction_turns: evict_idle_tools() 判定「閒置」的連續輪數門檻。
            heartbeat_interval: 心跳背景執行緒的檢查間隔秒數。
            heartbeat_checks: 心跳固定唯讀健檢清單；為 None 時使用
                DEFAULT_HEARTBEAT_CHECKS。
        """
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

        # =========================
        # 🧹 Skill Lifecycle（技能生命週期清除）
        # =========================
        self.tool_idle_eviction_turns = tool_idle_eviction_turns
        self.turn_counter = 0            # 每次 ask_ai() 呼叫模型前遞增一次，作為「輪次」的統一計數
        self.tool_spec_messages = {}     # 技能名稱 -> 該規格書在 self.messages 中的實際訊息物件參照
        self.tool_load_turn = {}         # 技能名稱 -> 載入當下的 turn_counter（尚未被 EXECUTE 過時的起算點）
        self.tool_last_used_turn = {}    # 腳本檔名 -> 最近一次被 EXECUTE 的 turn_counter

        # =========================
        # 💓 Heartbeat（背景健檢執行緒）
        # =========================
        # 設計原則：這個背景執行緒「絕不」直接碰 self.messages / loaded_tools 等主執行緒也會
        # 讀寫的狀態——只透過 subprocess 執行固定的唯讀腳本，結果單純寫進 heartbeat_log_file。
        # 下一次人真的互動、get_system_prompt() 重新組裝時，才會把心跳結果的檔案內容讀進去
        # 給模型看。這樣兩個執行緒之間完全沒有共享的可變物件，不需要鎖。
        self.heartbeat_interval = heartbeat_interval
        self.heartbeat_checks = list(heartbeat_checks) if heartbeat_checks is not None else list(DEFAULT_HEARTBEAT_CHECKS)
        self.heartbeat_log_file = os.path.join(self.script_dir, "logs", "heartbeat.md")
        self._heartbeat_stop_event = threading.Event()
        self._heartbeat_thread = None

        # =========================
        # 📦 Context Budget Monitor
        # =========================
        self.token_threshold = token_threshold

    def count_tokens(self, text: str) -> int:
        """
        計算一段文字若送進 self.model 會佔用的 token 數。

        優先呼叫 Ollama 官方的原生 tokenizer（對應 Gemma 4 e4b），這是最準確的
        算法；若呼叫失敗（例如模型尚未 pull 下來、Ollama 服務未啟動等任何例外），
        則退回粗略估算 len(text) // 4 作為安全網，確保呼叫端（get_context_token_count()
        等）永遠拿得到一個可用的數字，不會中斷。
        """
        try:
            tokens = ollama.tokenize(model=self.model, prompt=text)
            return len(tokens)
        except Exception:
            # fallback
            return len(text) // 4

    # =========================
    # 📦 Context Budget Monitor（主動量測 + 事前壓縮）
    # =========================
    def get_context_token_count(self) -> int:
        """
        量測『若現在呼叫模型，實際會送出去』的完整 context token 數。
        會先刷新 system prompt 內容，再對 self.messages 逐則計算——
        這與 ask_ai() 真正送給 ollama.chat(messages=self.messages) 的內容一致，
        不會像舊版顯示邏輯那樣把 system prompt 重複算一次。
        """
        if self.messages and self.messages[0]['role'] == 'system':
            self.messages[0]['content'] = self.get_system_prompt()
        full_text = "".join(f"{m['role']}: {m['content']}\n" for m in self.messages)
        return self.count_tokens(full_text)

    def enforce_context_budget(self):
        """
        主動式（proactive）上下文預算檢查：在每次呼叫模型『之前』檢查一次，
        超過 self.token_threshold 就先壓縮再繼續，確保不會送出超標的請求
        （舊版是等模型回覆、結果都算完之後才檢查，等於至少會放行一次超標請求）。

        只會壓縮 self.messages[1:]（動態對話歷史），system prompt（index 0：
        profile / objective / memory / skills index）不受影響——見
        compress_context_to_file()。

        回傳 (was_compressed: bool, token_count_after: int)。
        """
        token_count = self.get_context_token_count()
        if token_count <= self.token_threshold:
            return False, token_count

        print(f"\n📦 [上下文監控] {token_count} tokens 超過閾值 {self.token_threshold}，事前壓縮中...")
        actually_compressed = self.compress_context_to_file(num_to_keep=2)
        token_count_after = self.get_context_token_count()

        if actually_compressed:
            print(f"📦 [上下文監控] 壓縮後 {token_count_after} tokens")
        else:
            print(
                "📦 [上下文監控] 目前沒有可壓縮的動態歷史了——"
                "system prompt（profile/objective/memory/skills index）本身就已接近或超過閾值，"
                "這部分依規則不會被壓縮，將照常送出。若要降低，需精簡 ROBOT_AGENT.md／memory／INDEX.md 本身內容。"
            )
        return actually_compressed, token_count_after

    # =========================
    # 🧹 Skill Lifecycle（技能生命週期清除）
    # =========================
    def evict_idle_tools(self):
        """
        主動式技能生命週期清除：與 enforce_context_budget() 一樣在 ask_ai() 呼叫模型『之前』
        執行，但目標不同——這裡是『精準』清除，只把連續 self.tool_idle_eviction_turns 輪都沒有
        被實際 EXECUTE 的技能規格書，從 self.messages 中單獨移除，不影響其他仍在使用中的技能
        或一般對話內容。這與 _truncate_memory()／compress_context_to_file() 那種『不分青紅皂白
        整批處理』的機制互補：那兩個是通用的安全網，這個是針對『特定技能真的用完了』的精準清理。

        被清除的技能會同時從 loaded_tools / loaded_scripts 除名，之後要再用就必須重新
        NEED_TOOL 載入——這就是「用完就從上下文移除」的實際落地：清除的不只是文字內容，
        agent 對『這個技能是否已知』的認知也一併重置。

        回傳被清除的技能名稱列表（可能為空）。
        """
        evicted = []
        for skill_name in list(self.loaded_tools):
            script_name = SKILL_NAME_TO_SCRIPT.get(skill_name, f"{skill_name}_cmd.py")
            # 從未被 EXECUTE 過的技能，以「載入當下」的 turn 當作起算基準
            baseline_turn = self.tool_last_used_turn.get(script_name, self.tool_load_turn.get(skill_name, self.turn_counter))
            idle_for = self.turn_counter - baseline_turn
            if idle_for < self.tool_idle_eviction_turns:
                continue

            spec_message = self.tool_spec_messages.pop(skill_name, None)
            if spec_message is not None and spec_message in self.messages:
                self.messages.remove(spec_message)

            self.loaded_tools.discard(skill_name)
            self.loaded_scripts.discard(script_name)
            self.tool_load_turn.pop(skill_name, None)
            self.tool_last_used_turn.pop(script_name, None)
            evicted.append(skill_name)

        if evicted:
            print(f"🧹 [技能生命週期] 已清除閒置技能規格書（連續 {self.tool_idle_eviction_turns} 輪未使用）: {evicted}")
        return evicted

    def compress_context_to_file(self, num_to_keep=2):
        """
        將動態對話歷史（self.messages[1:]，不含 system prompt）壓縮成一份摘要，
        歸檔到 logs/summary_<時間戳>.md，只在記憶體中保留最新 num_to_keep 筆原文。

        流程：切出要壓縮的舊訊息與要保留的最新 num_to_keep 筆 → 用一組專門的
        system/user prompt 請 self.model 依固定 Markdown 範本產生摘要 → 重建
        self.messages 為 [system prompt, *to_keep]（刻意不呼叫 reset_conversation()，
        否則會把剛保留下來的 to_keep 又整批蓋掉）→ 清空技能載入追蹤狀態（見
        _clear_tool_tracking()，因為壓縮後舊技能規格書多半已不在對話中）→ 把摘要
        寫入 logs/ 底下的獨立檔案。

        若動態歷史本來就不超過 num_to_keep 筆（例如對話才剛開始），視為沒有東西
        好壓縮，直接回傳 False、不呼叫模型也不寫檔；實際完成壓縮則回傳 True——
        呼叫端（enforce_context_budget()）靠這個真實結果判斷，而不是單純假設
        「呼叫了就一定有效果」。
        """
        history_to_compress = self.messages[1:]
        # 如果對話不足以壓縮，則直接返回
        if len(history_to_compress) <= num_to_keep:
            return False
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
        # （注意：這裡刻意不再呼叫 reset_conversation() —— 舊版在這行之後緊接著呼叫它，
        # 會把剛保留下來的 to_keep 訊息又整個蓋掉，等於「保留最新 N 筆」形同虛設。
        # system prompt 的內容仍會在下一次 ask_ai() / get_context_token_count() 時自動刷新，
        # 不需要在這裡重置。）
        self.messages = [self.messages[0]] + to_keep
        print(f"💾 [系統] 歷史已壓縮並存入。已保留最新的 {num_to_keep} 筆對話。")

        # 🔥 壓縮後，先前載入的技能規格書內容已不在對話中（除非剛好落在 to_keep 裡，但保守起見
        # 一律視為不再可靠），清空追蹤狀態避免誤判「已載入」。turn_counter 則刻意不重置——
        # 它只是單調遞增的相對時間軸，壓縮不代表對話真的重新開始（/clear 才是）。
        self._clear_tool_tracking()

        # 確保 logs 目錄存在
        log_dir = os.path.join(self.script_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)

        # 寫入獨立檔案
        timestamp = time.strftime('%Y-%m-%d_%H-%M-%S')
        file_path = os.path.join(log_dir, f"summary_{timestamp}.md")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(f"# Summary at {timestamp}\n\n{summary_content}")

        print(f"💾 [系統] 歷史已壓縮並存入: {os.path.basename(file_path)}")
        return True

    def load_recent_summary_logs(self, max_files=5):
        """
        讀取 logs/ 目錄下最新的 max_files 個壓縮摘要檔（compress_context_to_file()
        產生的 summary_<時間戳>.md），依檔名時間戳由新到舊排序後串接全文回傳，
        供 get_system_prompt() 組進「Recent Compressed History Summary」區塊，
        讓模型即使歷史已被壓縮清空，也還能看到近期發生過什麼事。目錄不存在時
        回傳提示字串，不拋例外。
        """
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
        """
        讀取 memory/semantic.md、episodic.md、procedural.md 三個長期記憶檔各自的
        最後 max_lines 行，各自標上對應的中文標題後串接成一段文字回傳，供
        get_system_prompt() 組進系統提示詞的「Long Term Memory」區塊。三個檔案
        各自獨立處理——檔案不存在時該區塊顯示「No memory yet.」，讀取例外時顯示
        錯誤訊息，都不會影響其他兩個檔案的讀取或讓整個系統提示詞組裝失敗。
        """
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

    # =========================
    # 💓 Heartbeat（背景健檢執行緒）
    # =========================
    def load_recent_heartbeat_checks(self, max_lines=20):
        """讀取心跳健檢紀錄檔的尾段內容，供 get_system_prompt() 顯示給模型參考。"""
        if not os.path.exists(self.heartbeat_log_file):
            return "No heartbeat checks yet."
        try:
            with open(self.heartbeat_log_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return "".join(lines[-max_lines:]).strip()
        except Exception as e:
            return f"[Heartbeat Load Error] {e}"

    def start_heartbeat(self):
        """啟動背景心跳執行緒。已在執行時回傳 False，成功啟動回傳 True。"""
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return False
        self._heartbeat_stop_event.clear()
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        return True

    def stop_heartbeat(self):
        """停止背景心跳執行緒。本來就沒在跑時回傳 False，成功停止回傳 True。"""
        if self._heartbeat_thread is None or not self._heartbeat_thread.is_alive():
            self._heartbeat_thread = None
            return False
        self._heartbeat_stop_event.set()
        self._heartbeat_thread.join(timeout=5)
        self._heartbeat_thread = None
        return True

    def is_heartbeat_running(self):
        """回傳心跳背景執行緒目前是否存在且存活中（True/False），供狀態列與 /heartbeat status 使用。"""
        return self._heartbeat_thread is not None and self._heartbeat_thread.is_alive()

    def _heartbeat_loop(self):
        """
        心跳背景執行緒的主迴圈（由 start_heartbeat() 以 daemon thread 啟動）。

        用 Event.wait(timeout) 同時實現「睡眠」與「可被立即喚醒中斷」兩種功能：
        正常情況下每隔 heartbeat_interval 秒回傳 False（逾時）就執行一次健檢
        （_run_heartbeat_once()）；一旦 stop_heartbeat() 呼叫 set()，wait() 會
        立刻回傳 True，迴圈馬上結束，不用等到目前這一輪 interval 跑完。健檢過程
        中任何未預期例外都會被攔截並印出，不會讓整個背景執行緒意外崩潰退出。
        """
        while not self._heartbeat_stop_event.wait(self.heartbeat_interval):
            try:
                self._run_heartbeat_once()
            except Exception as e:
                print(f"\n💓 [心跳] 執行健檢時發生未預期例外: {e}")

    def _run_heartbeat_once(self):
        """
        執行一輪固定的唯讀健檢。刻意不呼叫 ask_ai()、不寫入 memory/、不碰 self.messages——
        心跳期間沒有人在場確認，維持與 ROBOT_AGENT.md 一致的『記憶寫入需使用者明確要求』
        『EXECUTE 需經過協議』等原則，只單純把結果記錄下來，留給下次真人互動時參考。
        """
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        lines = [f"\n### 心跳檢查 {timestamp}"]

        for label, script_name, args in self.heartbeat_checks:
            script_path = os.path.join(self.base_path, "scripts", script_name)
            if not os.path.exists(script_path):
                lines.append(f"- **{label}**: [ERROR] 找不到腳本 {script_path}")
                continue
            try:
                res = subprocess.run(
                    [sys.executable, script_path] + list(args),
                    capture_output=True,
                    text=True,
                    cwd=self.current_cwd,
                    timeout=10,
                )
                output = res.stdout.strip() if res.returncode == 0 else res.stderr.strip()
            except Exception as e:
                output = f"[ERROR] 心跳檢查執行異常: {e}"
            lines.append(f"- **{label}**: {output}")

        os.makedirs(os.path.dirname(self.heartbeat_log_file), exist_ok=True)
        with open(self.heartbeat_log_file, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        print(f"\n💓 [心跳] {timestamp} 已完成健檢，結果已寫入 logs/heartbeat.md")

    def get_system_prompt(self):
        """
        組裝完整的 system prompt 全文（存放於 self.messages[0]）。

        每次呼叫都會即時組裝（而不是快取），確保狀態相關的區塊永遠反映最新值：
        ROBOT_AGENT.md 角色設定與協議全文、Current Agent State（工作目錄／容器
        目錄／本次對話已載入的技能規格書清單／心跳運作狀態）、sticky objective
        （若有設定）、長期記憶三檔尾段（load_long_term_memory()）、近期壓縮摘要
        （load_recent_summary_logs()）、近期心跳健檢結果（load_recent_heartbeat_checks()）、
        以及 INDEX.md 技能索引全文。ask_ai() 與 get_context_token_count() 在每次
        呼叫模型前都會用這個方法的回傳值刷新 self.messages[0]。
        """

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
            heartbeat_summary = self.load_recent_heartbeat_checks()

        loaded_display = sorted(self.loaded_tools) if self.loaded_tools else "None"
        heartbeat_state = "運作中" if self.is_heartbeat_running() else "已停止"
        status_prompt = (
            f"\n\n## Current Agent State\n"
            f"- CURRENT_WORKING_DIRECTORY: {self.current_cwd}\n"
            f"- CURRENT_CONTAINER_DIRECTORY: {self.container_cwd}\n"
            f"- LOADED_TOOL_SPECS_THIS_SESSION: {loaded_display}\n"
            f"- HEARTBEAT: {heartbeat_state}（間隔 {self.heartbeat_interval} 秒）"
        )

        return f"""
                {profile}
                {status_prompt}
                {objective_prompt}
                ## Long Term Memory
                {memory_content}
                ## Recent Compressed History Summary
                {history_summary}
                ## Recent Heartbeat Checks
                {heartbeat_summary}
                ## Available Skills (INDEX.md)
                {skills}
                """

    def _clear_tool_tracking(self):
        """
        清空所有『已載入技能』的追蹤狀態。凡是會讓 self.messages 整批被改寫或清空的操作
        （/clear 重置、壓縮）都必須呼叫這個，否則 loaded_tools 會謊報「規格書還在上下文裡」，
        導致 LOADED_TOOL_SPECS_THIS_SESSION 狀態列失真、也讓 run_tool() 的軟性提醒判斷錯誤。
        （這其實是修正了一個既有問題：舊版 /clear 只重置了 messages，從沒清過這些追蹤集合。）
        """
        self.loaded_tools.clear()
        self.loaded_scripts.clear()
        self.consecutive_need_tool_count = 0
        self.tool_spec_messages.clear()
        self.tool_load_turn.clear()
        self.tool_last_used_turn.clear()

    def reset_conversation(self):
        """
        將對話完全重置為初始狀態：self.messages 只剩下一則全新組裝的 system
        prompt，並透過 _clear_tool_tracking() 一併清空所有技能載入追蹤狀態
        （否則 loaded_tools 會謊報規格書還在上下文裡），turn_counter 歸零重新
        起算。對應 REPL 的 /clear 指令。

        刻意不處理心跳背景執行緒——心跳是否運作是獨立於對話內容的關注點，
        /clear 不會、也不應該連帶停止已經在跑的心跳（見 stop_heartbeat()）。
        """
        self.messages = [{'role': 'system', 'content': self.get_system_prompt()}]
        self._clear_tool_tracking()
        self.turn_counter = 0

    def _truncate_memory(self):
        """
        通用的滑動視窗安全網：當 self.messages 筆數超過 max_history + 1（+1 是
        system prompt）時，只保留 system prompt 加上最新 max_history 筆訊息，
        捨棄更早的內容。這是不分內容種類、單純依筆數觸發的粗粒度機制，與
        evict_idle_tools()（針對特定閒置技能規格書的精準清除）及
        compress_context_to_file()（整批摘要歸檔）互補，三者共同構成上下文
        管理的多層防線。
        """
        if len(self.messages) > self.max_history + 1:
            print(f"⚠️  [記憶優化] 啟動滑動視窗（保留 {self.max_history} 筆）")
            self.messages = [self.messages[0]] + self.messages[-self.max_history:]

    def ask_ai(self):
        """
        呼叫模型取得一次回覆，是主迴圈每一輪對話的核心入口。

        依序執行：turn_counter 遞增（作為技能生命週期等機制的統一輪次計數）→
        刷新 system prompt → 通用滑動視窗截斷（_truncate_memory()）→ 精準清除
        閒置技能規格書（evict_idle_tools()）→ 主動式上下文預算檢查與壓縮
        （enforce_context_budget()）→ 才真正呼叫 ollama.chat()。這個順序確保
        送給模型的請求一定是「清理過、且沒有超過預算」的版本，而不是先送出去
        才發現超標。

        回覆內容若包含 <thought>...</thought> 或以 "...done thinking." 結尾的
        推理痕跡，會被去除，只保留正式回覆部分。任何例外（如 Ollama 連線失敗）
        都會被攔截，回傳一則說明錯誤的字串而不是讓呼叫端崩潰。
        """
        try:
            self.turn_counter += 1

            if self.messages and self.messages[0]['role'] == 'system':
                self.messages[0]['content'] = self.get_system_prompt()

            self._truncate_memory()
            self.evict_idle_tools()        # 先做精準清除：把真的閒置的技能規格書單獨移除
            self.enforce_context_budget()  # 再做預算檢查：若還是超標，才動用整批壓縮

            response = ollama.chat(
                model=self.model,
                messages=self.messages,
                options={'temperature': 0.2},
                think=False,  # gemma4:e4b 支援思考模式，若不關閉，答案會被 Ollama 分離到
                              # message.thinking 而非 message.content，導致 content 永遠是空字串
            )

            raw_content = response['message']['content'].strip()

            if "<thought>" in raw_content:
                raw_content = raw_content.split("</thought>")[-1].strip()
            elif "...done thinking." in raw_content:
                raw_content = raw_content.split("...done thinking.")[-1].strip()

            return raw_content

        except Exception as e:
            return f"Ollama 連線錯誤: {e}"

    def _get_index_skill_names(self):
        """讀取 INDEX.md 表格中以反引號包住的技能名稱清單，供 check_need_tool() 驗證用。"""
        with open(self.index_file, "r", encoding="utf-8") as f:
            content = f.read()
        return set(re.findall(r"`([A-Za-z0-9_]+)`", content))

    def check_need_tool(self, ai_response):
        """
        解析 `NEED_TOOL: <技能名稱>`，命中時載入 skills_system/tools/<name>.md 全文，
        是本次「隨需載入」重構的核心：技能規格書預設不在 context 裡，只有被明確請求時才載入。

        只看第一行（刻意不沿用 EXECUTE 的 split(maxsplit=1) 寫法 —— 那種寫法在模型於指令後
        面接著輸出額外說明文字、且沒有用 code fence 包起來時，會把說明文字一併吃進參數）。

        回傳 (顯示文字, 剛被新載入的技能名稱或 None)。第二個值只有在「這次是真的從檔案讀進來的
        全新載入」時才會是技能名稱，命中快取／找不到／不合法名稱等情況一律是 None——呼叫端
        （main()）需要靠這個值決定要不要把訊息物件登記進 tool_spec_messages 供之後清除使用。
        沒有任何 NEED_TOOL 宣告時，第一個值回傳 None。
        """
        stripped = ai_response.strip()
        if not stripped:
            return None, None

        first_line = stripped.splitlines()[0]
        if "NEED_TOOL:" not in first_line:
            return None, None

        raw_name = first_line.split("NEED_TOOL:", 1)[1].strip()
        raw_name = raw_name.strip("`\"' ")

        # 安全防護：僅允許英數字與底線並取 basename，避免路徑穿越字串（與 grep_cmd.py/find_file_cmd.py
        # 阻擋 "/" 搜尋的既有防護風格一致）
        safe_name = os.path.basename(raw_name)
        if not re.fullmatch(r"[A-Za-z0-9_]+", safe_name):
            return f"[ERROR] 不合法的技能名稱: {raw_name}", None

        # 硬性驗證：必須是 INDEX.md 實際列出的技能，避免模型憑空捏造名稱、
        # 或載入到未登記於索引、僅存在孤兒規格書的技能
        if safe_name not in self._get_index_skill_names():
            return f"[ERROR] 技能 '{safe_name}' 未列於 INDEX.md，拒絕載入規格書。請確認技能名稱是否正確。", None

        if safe_name in self.loaded_tools:
            return f"[INFO] 技能 '{safe_name}' 規格書本次對話已載入，無需重複讀取。", None

        doc_path = os.path.join(self.tools_dir, f"{safe_name}.md")
        if not os.path.exists(doc_path):
            return f"[ERROR] 找不到技能 '{safe_name}' 的規格書，請確認 INDEX.md 中的技能名稱是否正確。", None

        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.loaded_tools.add(safe_name)
        script_name = SKILL_NAME_TO_SCRIPT.get(safe_name, f"{safe_name}_cmd.py")
        self.loaded_scripts.add(script_name)
        self.tool_load_turn[safe_name] = self.turn_counter

        return f"[PASS] 已載入技能 '{safe_name}' 規格書：\n{content}", safe_name

    def run_tool(self, ai_response):
        """
        解析模型回覆中的 `EXECUTE: <腳本> <參數>` 指令並實際執行對應腳本，回傳
        執行結果字串；若回覆中沒有 EXECUTE 標記則回傳 None（呼叫端據此判斷這輪
        沒有工具動作）。

        主要步驟：
        1. 取出 EXECUTE: 之後的內容，若模型多輸出了 code fence 或 "# ---" 分隔線，
           一併裁掉；再切出腳本名稱與其餘參數字串。
        2. 腳本名稱正規化——先查 SKILL_NAME_TO_SCRIPT 歷史例外表（如 change_dir →
           cd_cmd.py，與 check_need_tool() 共用同一張表），查無對應才退回
           `<名稱>_cmd.py` 預設規則，並統一補齊／修正 `_cmd.py` 副檔名。
        3. 硬性協議關卡：若正規化後的腳本檔名不在 self.loaded_scripts（代表尚未
           透過 NEED_TOOL 成功載入過對應規格書），直接回傳 `[ERROR]` 拒絕執行，
           不會進到 subprocess 呼叫——NEED_TOOL 不只是省 token 機制，也是唯一
           能驗證「技能真實存在於 INDEX.md」的關卡（見 check_need_tool() 的
           _get_index_skill_names() 檢查）。
        4. 參數切分：`manage_skill` 系列因為參數本身是用 `|` 分隔的單一字串
           （名稱｜描述｜參數名｜程式碼主體），整段原樣當一個參數傳入，不能用
           shlex 再切一次；其餘腳本則用 shlex.split（失敗時退回簡單 split）。
        5. 用 subprocess 執行腳本，帶入目前的 current_cwd 與（透過環境變數
           CONTAINER_CWD 注入的）container_cwd。
        6. 掃描輸出中的 `[CWD_CHANGED]`／`[CONTAINER_CWD]` 標記以同步 Agent 的
           目錄狀態；若是成功的 manage_skill 呼叫，因為新技能是在子行程中建立、
           無法直接改到本行程的記憶體狀態，改由這裡從父行程解析同一份輸入把新
           技能名稱登記進 loaded_tools/loaded_scripts。
        7. 記錄這個腳本這一輪確實被 EXECUTE 過（供 evict_idle_tools() 判斷閒置）。

        任何解析或執行過程中的例外都會被攔截，回傳說明錯誤的字串。
        """
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
                raw_name = os.path.basename(parts[0])
                # 先查歷史例外表（顯示名稱與實際腳本檔名不一致，如 change_dir -> cd_cmd.py），
                # 與 check_need_tool() 使用同一張表，避免兩處判斷各自為政而對不上
                script_name = SKILL_NAME_TO_SCRIPT.get(raw_name, raw_name)

                if not script_name.endswith("_cmd.py") and not script_name.endswith(".py"):
                    script_name = f"{script_name}_cmd.py"
                elif script_name.endswith(".py") and not script_name.endswith("_cmd.py"):
                    script_name = script_name.replace(".py", "_cmd.py")

                # --- 硬性協議關卡：EXECUTE 前必須先 NEED_TOOL 讀過規格書 ---
                # 原本是軟性提醒（仍會放行執行），現改為硬性擋下：NEED_TOOL 不只是省 token
                # 機制，也是「先確認技能真實存在於 INDEX.md 且已核閱規格書」的協議關卡，
                # 未通過一律拒絕執行，不會呼叫 subprocess。
                if script_name not in self.loaded_scripts:
                    return (
                        f"[ERROR] 尚未透過 NEED_TOOL 載入此技能的規格書，依協議禁止直接 EXECUTE '{raw_name}'。"
                        f"請先輸出 NEED_TOOL: <INDEX.md 中列出的技能名稱>。"
                    )

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

                # --- 技能生命週期：記錄這個腳本『這一輪』被實際使用過 ---
                # 不論成功或失敗都算「有在用」，只有真正被晾在一邊、完全沒被 EXECUTE 過的
                # 技能才會被 evict_idle_tools() 判定為閒置。
                self.tool_last_used_turn[script_name] = self.turn_counter

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

                return output_text

            except Exception as e:
                return f"解析指令失敗: {e}"

        return None

# =========================================================
# 🚀 MAIN LOOP (加入 Token Tracking 顯示 + NEED_TOOL 隨需載入)
# =========================================================

def main():
    """
    REPL 主程式進入點：建立一個 SkillAgent、重置對話，然後進入無窮迴圈讀取
    使用者輸入並處理。

    每輪輸入先比對是否命中內建的 slash 指令（/clear、/compress、/auto on|off、
    /hybrid on|off、/budget、/skill_ttl、/heartbeat on|off|status|interval、
    /objective，以及多行版本的 objective set/show/clear）並就地處理、continue
    回主迴圈；否則視為一般訊息，進入內層迴圈：呼叫 ask_ai() 取得模型回覆 →
    依序判斷 check_need_tool()（優先）與 run_tool() → 視結果與目前模式
    （NEED_TOOL 一律自動繼續／auto/hybrid/manual）決定是否把結果加入對話並
    繼續下一輪，或中斷交還使用者輸入。exit/quit 或 Ctrl+C 都會先呼叫
    agent.stop_heartbeat() 再結束，避免心跳背景執行緒殘留。
    """
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
                agent.stop_heartbeat()
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

            # --- 📦 上下文預算閾值查詢／設定 ---
            if user_msg.lower() == '/budget' or user_msg.lower().startswith('/budget '):
                remainder = user_msg[len('/budget'):].strip()
                if not remainder:
                    print(f"📦 目前上下文預算閾值: {agent.token_threshold} tokens")
                else:
                    try:
                        agent.token_threshold = int(remainder)
                        print(f"📦 已設定上下文預算閾值為 {agent.token_threshold} tokens")
                    except ValueError:
                        print("⚠️ 用法錯誤，請輸入整數，例如: /budget 8000（或 /budget 查看目前值）")
                continue

            # --- 🧹 技能閒置逐出門檻查詢／設定 ---
            if user_msg.lower() == '/skill_ttl' or user_msg.lower().startswith('/skill_ttl '):
                remainder = user_msg[len('/skill_ttl'):].strip()
                if not remainder:
                    print(f"🧹 技能閒置逐出門檻: 連續 {agent.tool_idle_eviction_turns} 輪未使用即清除")
                else:
                    try:
                        agent.tool_idle_eviction_turns = int(remainder)
                        print(f"🧹 已設定技能閒置逐出門檻為 {agent.tool_idle_eviction_turns} 輪")
                    except ValueError:
                        print("⚠️ 用法錯誤，請輸入整數，例如: /skill_ttl 6（或 /skill_ttl 查看目前值）")
                continue

            # --- 💓 心跳機制：開關／狀態／間隔設定 ---
            if user_msg.lower() == '/heartbeat on':
                started = agent.start_heartbeat()
                if started:
                    print(f"💓 已啟動心跳機制（每 {agent.heartbeat_interval} 秒執行一次唯讀健檢："
                          f"{', '.join(label for label, _, _ in agent.heartbeat_checks)}）")
                else:
                    print("💓 心跳機制已經在執行中")
                continue
            if user_msg.lower() == '/heartbeat off':
                stopped = agent.stop_heartbeat()
                print("🛑 已停止心跳機制" if stopped else "🛑 心跳機制本來就沒有在跑")
                continue
            if user_msg.lower() == '/heartbeat status':
                state = "運作中" if agent.is_heartbeat_running() else "已停止"
                checks_desc = ", ".join(label for label, _, _ in agent.heartbeat_checks) or "（無）"
                print(f"💓 心跳狀態: {state}｜間隔: {agent.heartbeat_interval} 秒｜健檢項目: {checks_desc}")
                continue
            if user_msg.lower().startswith('/heartbeat interval'):
                remainder = user_msg[len('/heartbeat interval'):].strip()
                if not remainder:
                    print(f"💓 目前心跳間隔: {agent.heartbeat_interval} 秒")
                else:
                    try:
                        agent.heartbeat_interval = int(remainder)
                        print(f"💓 已設定心跳間隔為 {agent.heartbeat_interval} 秒（下一輪心跳生效）")
                    except ValueError:
                        print("⚠️ 用法錯誤，請輸入整數秒數，例如: /heartbeat interval 300")
                continue

            # --- 🎯 OBJECTIVE 單行版本（/objective <文字> 直接設定；/objective 顯示目前值；
            #     /objective clear 清除。多行輸入仍可用下面的 objective set/show/clear） ---
            if user_msg.lower() == '/objective' or user_msg.lower().startswith('/objective '):
                remainder = user_msg[len('/objective'):].strip()
                if not remainder:
                    print(f"🎯 Current: {agent.sticky_objective or 'None'}")
                elif remainder.lower() == 'clear':
                    agent.sticky_objective = ""
                    print("🧹 已清除 Sticky Objective")
                else:
                    agent.sticky_objective = remainder
                    print(f"🎯 已設定核心目標: {remainder}")
                continue

            # --- 🎯 OBJECTIVE 設定（多行輸入版本，適合較長的目標描述） ---
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
                tool_doc, freshly_loaded_name = agent.check_need_tool(ai_msg)
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
                # 壓縮已改為主動式（見 SkillAgent.enforce_context_budget()，在 ask_ai() 呼叫模型
                # 之前就先檢查並壓縮），這裡單純顯示目前狀態，不再重複觸發壓縮判斷。
                print(f"\n📦 Context Tokens: {agent.get_context_token_count()} (閾值 {agent.token_threshold})")
                print(f"📊 Stats | User: {agent.total_user_tokens} | AI: {agent.total_ai_tokens} | Tool: {agent.total_tool_tokens}")

                # --- 模式判定流程 ---
                if not result:
                    break

                # 0. NEED_TOOL：唯讀的規格書載入動作，一律自動繼續，不受 auto/hybrid/manual 影響
                if is_need_tool:
                    spec_message = {
                        'role': 'user',
                        'content': (
                            f"[tool spec]\n{result}\n\n"
                            "[SYSTEM] 已自動載入上方規格書並交還給你，這一輪請直接根據規格書與使用者原始需求"
                            "輸出對應的 EXECUTE: 指令；若參數已齊全，嚴禁反問使用者「接下來要做什麼」。"
                        ),
                    }
                    agent.messages.append(spec_message)
                    if freshly_loaded_name:
                        # 登記訊息物件參照，供 evict_idle_tools() 之後精準移除（而非用索引，
                        # 避免 truncate/compress 造成索引失效）
                        agent.tool_spec_messages[freshly_loaded_name] = spec_message
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
            agent.stop_heartbeat()
            print("\n👋 Bye")
            break

if __name__ == "__main__":
    main()
