import os
import sys
import ast
import shutil
import subprocess
import time

MAX_BACKUPS_PER_FILE = 5  # 每個檔案最多保留幾份歷史備份，避免 .history/ 無限增長


class SkillManager:
    def __init__(self, base_dir=None):
        self.root = base_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills_system")
        self.skills_dir = os.path.join(self.root, "skills")
        self.scripts_dir = os.path.join(self.root, "scripts")
        self.tools_dir = os.path.join(self.root, "tools")
        self.index_file = os.path.join(self.root, "INDEX.md")

        for d in [self.skills_dir, self.scripts_dir, self.tools_dir]:
            os.makedirs(d, exist_ok=True)
            if d == self.skills_dir and not os.path.exists(os.path.join(d, "__init__.py")):
                with open(os.path.join(d, "__init__.py"), "w") as f: pass

    # =========================
    # 🧪 語法驗證 + 從程式碼靜態萃取執行分支
    # =========================
    def _analyze_code(self, param_names, code_body):
        """
        用 ast.parse() 驗證生成的程式碼語法（不執行，只解析），同時順手把所有
        return 敘述連同其觸發條件萃取出來，取代規格書裡『通用樣板文字』的部分。

        回傳 (is_valid, branch_lines_or_error)：
        - is_valid=False 時第二個值是語法錯誤訊息，呼叫端應中止建立、不覆蓋任何檔案
          ——這是直接回應「縮排/語法壞掉也要等真正 EXECUTE 時才會爆炸」的具體修法。
        - is_valid=True 時第二個值是條列字串 list（可能是空list，代表程式碼沒有明顯分支）。
        """
        indented = "\n".join(f"    {l}" if l.strip() else "" for l in code_body.replace('\\n', '\n').splitlines())
        src = f"def execute({', '.join(param_names)}):\n{indented}\n"

        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            return False, f"第 {e.lineno} 行附近語法錯誤: {e.msg}"

        branch_lines = []

        def describe(node):
            try:
                return ast.unparse(node)
            except Exception:
                return "<運算式>"

        def describe_return_value(value_node, conditions):
            # 三元運算式（return "High" if val > 100 else "Low"）是 ROBOT_AGENT.md 教的標準單行
            # 寫法，屬於運算式而非 if 敘述，AST 上是 IfExp 而非 If——特別拆解成兩條分支，
            # 否則會整句被當成一個不透明的運算式，規格書品質提升有限。遞迴處理是為了涵蓋
            # 巢狀三元運算式（如 "A" if x else ("B" if y else "C")）。
            if isinstance(value_node, ast.IfExp):
                cond_text = describe(value_node.test)
                describe_return_value(value_node.body, conditions + [f"若 {cond_text}"])
                describe_return_value(value_node.orelse, conditions + [f"若非 {cond_text}"])
                return
            value_text = describe(value_node) if value_node is not None else "None"
            cond_desc = "、".join(conditions) if conditions else "（預設／無額外條件）"
            branch_lines.append(f"* {cond_desc} → 回傳 `{value_text}`")

        def walk(stmts, conditions):
            for node in stmts:
                if isinstance(node, ast.If):
                    cond_text = describe(node.test)
                    walk(node.body, conditions + [f"若 {cond_text}"])
                    if node.orelse:
                        walk(node.orelse, conditions + [f"若非 {cond_text}"])
                elif isinstance(node, ast.Return):
                    describe_return_value(node.value, conditions)
                else:
                    pass  # 賦值、迴圈等其他敘述暫不細分，避免規格書過度冗長

        walk(tree.body[0].body, [])
        return True, branch_lines

    # =========================
    # 🕰️ 版本控管：覆蓋前備份
    # =========================
    def _backup_existing_file(self, file_path):
        """覆蓋前備份既有檔案到同目錄下的 .history/，只保留最近 MAX_BACKUPS_PER_FILE 份。"""
        if not os.path.exists(file_path):
            return None

        backup_dir = os.path.join(os.path.dirname(file_path), ".history")
        os.makedirs(backup_dir, exist_ok=True)
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        base_name = os.path.basename(file_path)
        backup_path = os.path.join(backup_dir, f"{base_name}.{timestamp}.bak")
        shutil.copy2(file_path, backup_path)

        prefix = f"{base_name}."
        existing = sorted(
            f for f in os.listdir(backup_dir) if f.startswith(prefix) and f.endswith(".bak")
        )
        for stale in existing[:-MAX_BACKUPS_PER_FILE]:
            try:
                os.remove(os.path.join(backup_dir, stale))
            except OSError:
                pass

        return backup_path

    # =========================
    # 🧯 最佳努力執行煙霧測試（非阻斷性）
    # =========================
    def _smoke_test_script(self, script_path, param_names):
        """
        用預留值（"1"）實際跑一次剛產生的腳本。非阻斷性——結果只當警示訊息附在
        回傳訊息裡，不會讓 create_skill() 失敗：有些「跑失敗」其實是腳本正確拒絕了
        不合理的測試輸入，不代表程式本身壞掉，所以不能直接當作硬性判準。
        """
        placeholder_args = ["1" for _ in param_names]
        try:
            res = subprocess.run(
                [sys.executable, script_path] + placeholder_args,
                capture_output=True, text=True, timeout=5, cwd=self.root,
            )
            if res.returncode != 0:
                err = res.stderr.strip()[:200]
                return f"⚠️ 煙霧測試：以預留值 {placeholder_args} 執行回傳非 0 狀態碼（{err}）——可能是預留值不符合此技能的合理輸入，僅供參考。"
            out = res.stdout.strip()[:200]
            return f"✅ 煙霧測試：以預留值 {placeholder_args} 實際執行成功，輸出: {out}"
        except subprocess.TimeoutExpired:
            return "⚠️ 煙霧測試逾時（5 秒）——若此技能本來就需要較長執行時間可忽略。"
        except Exception as e:
            return f"⚠️ 煙霧測試執行異常: {e}"

    def _write_tool_doc(self, name, description, param_names, code_body, branch_lines):
        """
        產生／更新 tools/<name>.md 規格書。刻意不再『已存在就跳過』——技能重建時規格書
        也要跟著更新，否則參數/邏輯改了規格書卻停在舊版本，比沒有規格書更誤導人。
        """
        doc_path = os.path.join(self.tools_dir, f"{name}.md")
        clean_desc = description.replace('\n', ' ').strip()
        preview = code_body.replace('\\n', '\n').strip()
        params_desc = "、".join(f"`{p}`" for p in param_names)
        args_hint = " ".join(f"<{p}>" for p in param_names)

        if branch_lines:
            logic_section = "\n".join(branch_lines)
        else:
            logic_section = "（未偵測到明確的條件分支，實際行為請參考下方原始碼）"

        doc = f"""---
type: Tool
title: {clean_desc}
description: {clean_desc}
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
此技能由 Agent 透過 `manage_skill` 自我進化機制動態建立。

# 語法 / 參數規範
* 參數（依序）：{params_desc}。純數字（不含逗號）字串會自動轉型為 float，否則以原始字串處理。

# 執行步驟 (Steps)
1. 確認已透過 `NEED_TOOL: {name}` 載入本規格書。
2. 呼叫 `EXECUTE: {name}_cmd.py {args_hint}`。
3. 腳本依下方「執行邏輯分支」處理輸入並回傳結果字串。

# 範例 (Examples)
* `EXECUTE: {name}_cmd.py {args_hint}`

# 執行邏輯分支（依原始碼靜態掃描自動產生）
{logic_section}

# 實作邏輯 (Generated Logic Preview)
```
{preview}
```

# 異常處理 (Edge Cases)
* 上方「執行邏輯分支」為靜態掃描結果，如與實際行為不符請以 `scripts/{name}_cmd.py` 原始碼為準。
* 若參數數量與「語法/參數規範」不符，腳本會印出用法提示。
* 此技能為動態生成、僅通過語法驗證與一次煙霧測試，未經人工完整審核。
"""
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write(doc)
        return doc_path

    def _update_index(self, name, description):
        """
        將自我進化技能註冊到 INDEX.md。若該技能已有索引列，直接原地更新內容
        （而非略過）——理由與 _write_tool_doc 相同：重建技能時描述可能已經改變。
        """
        clean_desc = description.replace('\n', ' ').strip()
        new_line = f"| **自我進化** | {name} | {clean_desc} | [tools/{name}.md](tools/{name}.md) |"

        content = ""
        if os.path.exists(self.index_file):
            with open(self.index_file, "r", encoding="utf-8") as f:
                content = f.read()

        lines = content.splitlines()
        for i, line in enumerate(lines):
            if f"| {name} |" in line:
                lines[i] = new_line
                with open(self.index_file, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                return

        entry = new_line + "\n"
        if content and not content.endswith('\n'):
            entry = "\n" + entry
        with open(self.index_file, "a", encoding="utf-8") as f:
            f.write(entry)

    def create_skill(self, name, description, params, code_body):
        # 支援多參數：params 可以是單一名稱（"val"）或以逗號分隔的多個名稱
        # （"robot_name,workstations,priority,state,is_authored"）。
        param_names = [p.strip() for p in params.split(',') if p.strip()]
        if not param_names:
            return f"❌ 建立技能 {name} 失敗：參數名稱不可為空"

        # --- 語法驗證（先做，不寫任何檔案）---
        is_valid, result = self._analyze_code(param_names, code_body)
        if not is_valid:
            return (
                f"❌ 建立技能 {name} 失敗：{result}\n"
                f"已中止建立，未覆蓋任何現有檔案。請修正 code_body（檢查 \\n 與 4 空格縮排是否正確）後重新呼叫。"
            )
        branch_lines = result

        script_path = os.path.join(self.scripts_dir, f"{name}_cmd.py")
        doc_path = os.path.join(self.tools_dir, f"{name}.md")

        # --- 版本控管：覆蓋前備份既有腳本與規格書 ---
        script_backup = self._backup_existing_file(script_path)
        self._backup_existing_file(doc_path)

        # 1. 將字面上的 \n 替換為真正的換行符
        # 2. 將整段代碼按行拆分，並為每一行統一加上 4 個空格的基礎縮排
        raw_lines = code_body.replace('\\n', '\n').splitlines()

        # 這裡不要用 .strip()，否則會殺掉模型辛苦寫下的縮排空格
        indented_lines = []
        for line in raw_lines:
            if line.strip():
                indented_lines.append(f"    {line}")  # 基礎縮排
            else:
                indented_lines.append("")

        indented_code = "\n".join(indented_lines)
        params_signature = ", ".join(param_names)
        args_hint = " ".join(f"<{p}>" for p in param_names)

        # 每個參數各自嘗試數值自動轉型（沿用既有邏輯：純數字字串才轉，含逗號的維持
        # 字串交給程式自行 .split(',')）
        coercion_block = "\n".join(
            f"""    try:
        if isinstance({p}, str) and ',' not in {p}:
            {p} = float({p})
    except (ValueError, TypeError):
        pass"""
            for p in param_names
        )

        argv_unpack = ", ".join(f"sys.argv[{i + 1}]" for i in range(len(param_names)))

        template = f"""import sys
import os

def execute({params_signature}):
    # 物理邊界防護：每個參數各自嘗試轉型（純數字、不含逗號才轉 float）
{coercion_block}

    # --- AI Generated Code ---
{indented_code}
    # -------------------------

if __name__ == "__main__":
    try:
        if len(sys.argv) > {len(param_names)}:
            print(execute({argv_unpack}))
        else:
            print(f"Usage: {{os.path.basename(__file__)}} {args_hint}")
    except Exception as e:
        print(f"Error: {{e}}", file=sys.stderr)
        sys.exit(1)
"""
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(template)

        # --- 最佳努力煙霧測試（非阻斷性，結果附在回傳訊息裡）---
        smoke_note = self._smoke_test_script(script_path, param_names)

        self._write_tool_doc(name, description, param_names, code_body, branch_lines)
        self._update_index(name, description)

        backup_note = ""
        if script_backup:
            rel = os.path.relpath(script_backup, self.root)
            backup_note = f"（原有腳本已備份至 {rel}）"

        return (
            f"✅ 技能 {name} 已建立/更新：腳本 (scripts/{name}_cmd.py) + 規格書 (tools/{name}.md) "
            f"+ 索引 (INDEX.md) 皆已同步{backup_note}\n{smoke_note}"
        )
