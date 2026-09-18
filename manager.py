import os
import textwrap

class SkillManager:
    def __init__(self, base_dir="/home/david/CLI_Ops_Test/skills_system"):
        self.root = base_dir
        self.skills_dir = os.path.join(self.root, "skills")
        self.scripts_dir = os.path.join(self.root, "scripts")
        self.index_file = os.path.join(self.root, "SKILLS.md")
        
        for d in [self.skills_dir, self.scripts_dir]:
            os.makedirs(d, exist_ok=True)
            if "skills" in d and not os.path.exists(os.path.join(d, "__init__.py")):
                with open(os.path.join(d, "__init__.py"), "w") as f: pass

    def _update_markdown(self, name, description, script_path, params):
        rel_path = os.path.relpath(script_path, self.root)
        # 確保 description 是一行，避免破壞表格結構
        clean_desc = description.replace('\n', ' ').strip()
        new_entry = f"| {name} | {clean_desc} | `{rel_path}` | `{params}` |\n"
        
        if os.path.exists(self.index_file):
            with open(self.index_file, "r", encoding="utf-8") as f:
                content = f.read()
            
            if f"| {name} |" in content:
                return 

            # 關鍵修正：確保文件末尾有換行符，防止新行直接貼在舊行屁股後面
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
                indented_lines.append(f"    {line}") # 基礎縮排
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
            
        self._update_markdown(name, description, script_path, params)
        return f"✅ 技能 {name} 已建立並註冊至 SKILLS.md"