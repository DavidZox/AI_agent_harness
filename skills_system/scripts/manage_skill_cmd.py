# /home/david/AI_agent_harness/skills_system/scripts/manage_skill_cmd.py
import sys
import os

# 強制加入專案根目錄
PROJECT_ROOT = "/home/david/AI_agent_harness"
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from manager import SkillManager
except ImportError:
    print("致命錯誤：找不到 manager.py")
    sys.exit(1)

def main():
    # 我們預期 AI 的輸入格式為：名稱 | 描述 | 參數 | 程式碼
    # 範例：calc_battery | 電量計算 | voltage | v = float(voltage)...
    
    raw_input = " ".join(sys.argv[1:])
    
    if "|" not in raw_input:
        print("錯誤：AI 傳入格式不符（缺少分隔符 '|'）。")
        print(f"原始輸入: {raw_input}")
        return

    try:
        # 使用 | 進行拆分
        parts = [p.strip() for p in raw_input.split("|")]
        
        if len(parts) < 4:
            print("錯誤：參數不足。格式需為：名稱 | 描述 | 參數 | 程式碼")
            return

        name = parts[0]
        description = parts[1]
        params = parts[2]
        code_body = parts[3]

        manager = SkillManager()
        result = manager.create_skill(name, description, params, code_body)
        print(result)
        
    except Exception as e:
        print(f"自動生成技能失敗: {e}")

if __name__ == "__main__":
    main()