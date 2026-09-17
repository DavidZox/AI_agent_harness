import os

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

    def _write_tool_doc(self, name, description, params, code_body):
        """為自我進化技能產生 tools/<name>.md 規格書（若已存在則不覆蓋）"""
        doc_path = os.path.join(self.tools_dir, f"{name}.md")
        if os.path.exists(doc_path):
            return doc_path

        clean_desc = description.replace('\n', ' ').strip()
        preview = code_body.replace('\\n', '\n').strip()
        doc = f"""---
type: Tool
title: {clean_desc}
description: {clean_desc}
version: 1.0.0
dependencies: []
---

# 背景 / 運作原理
此技能由 Agent 透過 `manage_skill` 自我進化機制動態建立，非人工手寫。

# 語法 / 參數規範
* `{params}` (string, required)：呼叫者提供的單一參數。純數字（不含逗號）時會自動轉型為 float，否則以原始字串處理。

# 執行步驟 (Steps)
1. 確認已透過 `NEED_TOOL: {name}` 載入本規格書。
2. 呼叫 `EXECUTE: {name}_cmd.py <{params}>`。
3. 腳本依下方「實作邏輯」處理輸入並回傳結果字串。

# 範例 (Examples)
* `EXECUTE: {name}_cmd.py <{params} 範例值>`

# 實作邏輯 (Generated Logic Preview)
```
{preview}
```

# 異常處理 (Edge Cases)
* 此技能為動態生成、未經人工審核，實際行為請以 `scripts/{name}_cmd.py` 原始碼為準。
* 若參數格式與生成邏輯不符，腳本可能拋出例外並以非 0 狀態碼結束。
"""
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write(doc)
        return doc_path

    def _update_index(self, name, description):
        """將自我進化技能註冊到 INDEX.md（取代舊版對 SKILLS.md 表格的寫入）"""
        clean_desc = description.replace('\n', ' ').strip()
        new_entry = f"| **自我進化** | {name} | {clean_desc} | [tools/{name}.md](tools/{name}.md) |\n"

        content = ""
        if os.path.exists(self.index_file):
            with open(self.index_file, "r", encoding="utf-8") as f:
                content = f.read()

            if f"| {name} |" in content:
                return

            # 確保文件末尾有換行符，防止新行直接貼在舊行屁股後面
            if content and not content.endswith('\n'):
                new_entry = "\n" + new_entry

        with open(self.index_file, "a", encoding="utf-8") as f:
            f.write(new_entry)

    def create_skill(self, name, description, params, code_body):
        script_path = os.path.join(self.scripts_dir, f"{name}_cmd.py")

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

        template = f"""import sys
import os

def execute({params}):
    # 物理邊界防護：只有在「不含逗號」且「看起來像數字」時才自動轉型
    try:
        if isinstance({params}, str) and ',' not in {params}:
            {params} = float({params})
    except (ValueError, TypeError):
        pass

    # --- AI Generated Code ---
{indented_code}
    # -------------------------

if __name__ == "__main__":
    try:
        # 確保 sys.argv[1] 是原始字串進入 execute
        if len(sys.argv) > 1:
            print(execute(sys.argv[1]))
        else:
            print(f"Usage: {{os.path.basename(__file__)}} <{params}>")
    except Exception as e:
        print(f"Error: {{e}}", file=sys.stderr)
        sys.exit(1)
"""
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(template)

        self._write_tool_doc(name, description, params, code_body)
        self._update_index(name, description)
        return f"✅ 技能 {name} 已建立：腳本 (scripts/{name}_cmd.py) + 規格書 (tools/{name}.md) + 索引 (INDEX.md) 皆已註冊"
