"""搜尋並進入技能目錄（make_skill 於 2026-09-24 22:58 依實際操作軌跡自動產生的組合技能）

當使用者要求尋找特定關鍵字相關的技能資料夾，並進入該資料夾進行後續操作時使用。
依序執行下列既有技能的腳本，任一步回 [ERROR] 即停止；命令列位置參數依 PARAMS 順序代入 STEPS 的 {名稱} 佔位符。
這支檔案只是資料，可直接編輯 PARAMS / STEPS；執行邏輯在同目錄的 _composite.py。
草擬模型：gemma4:e4b；來源軌跡步驟：[26, 27, 28, 29]
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# 正式位置為 skills_system/scripts/；草稿位於 skills_system/drafts/<name>/ 時往上兩層找 scripts/
_SCRIPTS_DIR = _HERE if os.path.exists(os.path.join(_HERE, "_composite.py")) \
    else os.path.join(os.path.dirname(os.path.dirname(_HERE)), "scripts")
sys.path.insert(0, _SCRIPTS_DIR)
from _composite import run_composite

NAME = "try_deep_search"
PARAMS = [
    {
        "name": "search_keyword",
        "description": "需要搜尋的關鍵字。",
        "example": "skill"
    },
    {
        "name": "search_path",
        "description": "開始搜尋的目錄路徑。",
        "example": "."
    },
    {
        "name": "target_dir",
        "description": "需要切換進入的目標目錄路徑。",
        "example": "AI_agent_harness/skills_system/"
    }
]
STEPS = [
    {
        "purpose": "列出當前目錄的內容，了解檔案結構。",
        "skill": "list_dir",
        "script": "ls_cmd.py",
        "args": [
            "{search_path}"
        ],
        "source_step": 26
    },
    {
        "purpose": "搜尋包含特定關鍵字的檔案，定位目標技能文件。",
        "skill": "find_file",
        "script": "find_file_cmd.py",
        "args": [
            "{search_keyword}",
            "{search_path}"
        ],
        "source_step": 27
    },
    {
        "purpose": "切換到找到的技能目錄，準備後續操作。",
        "skill": "change_dir",
        "script": "cd_cmd.py",
        "args": [
            "{target_dir}"
        ],
        "source_step": 28
    },
    {
        "purpose": "再次列出當前目錄的內容，確認已進入正確的技能目錄。",
        "skill": "list_dir",
        "script": "ls_cmd.py",
        "args": [
            "{search_path}"
        ],
        "source_step": 29
    }
]

if __name__ == "__main__":
    print(run_composite(NAME, PARAMS, STEPS, sys.argv[1:], scripts_dir=_SCRIPTS_DIR))
