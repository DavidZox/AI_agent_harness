import sys
import os

# 專案根目錄以本檔案位置動態推導，不再寫死路徑
# (scripts/ -> skills_system/ -> 專案根目錄，共兩層)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from manager import SkillManager
except ImportError:
    print("致命錯誤：找不到 manager.py")
    sys.exit(1)

def main():
    """
    CLI 進入點：解析 Agent 以 `名稱 | 描述 | 參數 | 程式碼` 格式傳入的單一字串，
    轉呼叫 manager.SkillManager.create_skill() 實際建立新技能（同步產生腳本、
    規格書、INDEX 索引三者，詳細邏輯在 manager.py，不在本檔案範圍內）。

    將 sys.argv[1:] 以空白重新接回成 raw_input，再用 "|" 做無上限分割
    （raw_input.split("|")，不是 maxsplit=3）取出欄位：name（技能名稱）、
    description（描述）、params（參數名，可為單一名稱或以逗號分隔的多個名稱）、
    code_body（函式主體邏輯，會被 SkillManager 包上 `def execute(...):` 外殼）。
    只取前 4 個欄位（parts[0]～parts[3]）；若 code_body 本身包含額外的 "|"
    字元，會被切成更多段而只保留第 4 段，後面的內容會被靜默捨棄，需留意。

    若 raw_input 不含 "|"，或拆分後不足 4 個欄位，直接印出格式錯誤訊息並
    return，不會呼叫 SkillManager（避免用不完整的參數建立出殘缺技能）。
    SkillManager.create_skill() 執行過程中拋出的例外會在這裡被攔截並印出失敗
    訊息，不會讓例外往外傳播。

    無回傳值，所有結果（成功訊息或錯誤訊息）皆直接印到 stdout。
    """
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
