"""modify_memory：寫入使用者明確要求記住的經驗記憶。

兩種寫入目標：
- 全域（預設）：附加到專案根目錄的 Memory.md。每一輪都會透過 SkillAgent.load_long_term_memory()
  進入 system prompt 的「Long Term Memory」區塊，適合通用原則、技能之間的取捨、溝通風格、專案經驗。
- 技能綁定（--skill <技能名稱>）：附加到 skills_system/memory/<技能名稱>.md。只在該技能的規格文件被
  `EXECUTE: <技能名稱>` 載入時，由 SkillAgent._load_skill_doc() 自動附在規格文件後面一併進入上下文，
  平常不佔任何上下文（按需載入，與規格文件同一套 Progressive Disclosure 機制）。適合某個技能的用法、
  參數、前置條件、曾發生過的錯誤。

用法：
    modify_memory_cmd.py "問題種類 | 問題描述 | 解決方法或結論" [--skill <技能名稱>]
    modify_memory_cmd.py --move-last-to-skill <技能名稱>     # 把最新一則全域記憶改綁到該技能（使用者同意後才用）
回傳：成功以 [PASS] 開頭、失敗以 [ERROR] 開頭（專案共同慣例）。純本機檔案讀寫，不需要逾時機制。

綁不綁技能：小模型幾乎一律寫全域（評測 memory_skill_routing 實測），所以全域寫入的內容剛好提到一個技能時，
回傳會請模型用一句話問使用者要不要改綁；同意就用 --move-last-to-skill 搬過去。預設留在全域是比較安全的一邊：
「選技能時就要知道的規則」（例如 list_dir 不能看容器內）綁到技能上，選技能的當下反而看不到。

檔案路徑一律由腳本自身位置推算：腳本執行時的 cwd 是 AI 的虛擬工作目錄（可被 change_dir 改變），
與 harness 安裝位置無關，不可用相對路徑。
"""
import os
import re
import sys
from datetime import datetime

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILLS_DIR = os.path.dirname(_SCRIPTS_DIR)           # skills_system/
_PROJECT_ROOT = os.path.dirname(_SKILLS_DIR)
MEMORY_FILE = os.path.join(_PROJECT_ROOT, "Memory.md")
TOOLS_DIR = os.path.join(_SKILLS_DIR, "tools")
SKILL_MEMORY_DIR = os.path.join(_SKILLS_DIR, "memory")

# 單一技能的經驗記憶超過此數量／總長度時，在 [PASS] 訊息附上整併提醒：
# 這些條目每次載入該技能規格都會一併進入上下文，不該無止盡增長。
WARN_ENTRIES = 10
WARN_CHARS = 800

# 記憶條目：全域檔為「[MM-DD HH:MM] 內容」、技能檔為「- [MM-DD HH:MM] 內容」
_ENTRY_RE = re.compile(r"^-?\s*\[\d{2}-\d{2} \d{2}:\d{2}\]\s*(.*)$")

# 記憶工具本身永遠不是記憶的目標：小模型常把「正在使用的工具」誤填成 --skill，這裡直接拒絕。
SELF_NAME = "modify_memory"


def known_skills():
    """SKILLS.md 索引裡的技能名稱（以 tools/<name>.md 是否存在為準）。"""
    if not os.path.isdir(TOOLS_DIR):
        return []
    return sorted(f[:-3] for f in os.listdir(TOOLS_DIR) if f.endswith(".md"))


def mentioned_skills(content):
    """內容中點名到的技能（供全域寫入時提示，不改變寫入位置）。"""
    return [name for name in known_skills() if name != SELF_NAME and name in content]


def parse_args(argv):
    """回傳 (content, skill, move_to)。--skill 可放在內容前或後，也容忍 --skill=名稱 的寫法；
    --move-last-to-skill <名稱> 是另一個動作（搬移最新一則全域記憶），有它時 content 應為空。"""
    skill = None
    move_to = None
    rest = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--move-last-to-skill":
            if i + 1 >= len(argv):
                raise ValueError("--move-last-to-skill 後面必須接技能名稱（SKILLS.md 裡的名稱）")
            move_to = argv[i + 1]
            i += 2
            continue
        if tok == "--skill":
            if i + 1 >= len(argv):
                raise ValueError("--skill 後面必須接技能名稱（SKILLS.md 裡的名稱）")
            skill = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--skill="):
            skill = tok[len("--skill="):]
            i += 1
            continue
        rest.append(tok)
        i += 1
    return " ".join(rest).strip(), skill, move_to


def normalize_skill(name):
    """容忍 AI 帶上 .md 後綴或 tools/ 路徑（例如 tools/view_file.md）。"""
    name = os.path.basename((name or "").strip())
    if name.endswith(".md"):
        name = name[:-3]
    return name


def existing_entries(path):
    """讀出檔案中所有記憶條目的內容（去掉時間戳），用來判斷重複與統計。"""
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = _ENTRY_RE.match(line.strip())
            if m and m.group(1).strip():
                entries.append(m.group(1).strip())
    return entries


def _check_skill(skill, flag="--skill"):
    """技能名稱正規化與檢查：回傳 (技能名稱, 錯誤訊息或 None)。"""
    skill = normalize_skill(skill)
    if skill == SELF_NAME:
        return skill, (
            f"[ERROR] {flag} 不能是 {SELF_NAME} 本身（它只是寫入記憶的工具，不是記憶的主題）。"
            "這則記憶若與某個技能的用法有關，請改綁那個技能；若是通用原則、溝通風格或選技能的規則，"
            "請不加 --skill 重新寫入全域。"
        )
    if not skill or not os.path.exists(os.path.join(TOOLS_DIR, f"{skill}.md")):
        return skill, (
            f"[ERROR] 找不到技能 '{skill}' 的規格文件（skills_system/tools/{skill}.md）。"
            f"{flag} 後面必須是 SKILLS.md 索引裡的技能名稱；若這則記憶不屬於特定技能，請不要加 --skill。"
        )
    return skill, None


def _append_skill_entry(skill, stamp, content):
    """寫一則技能綁定記憶（檔案不存在時先寫標題）。回傳 (是否真的寫入, 目前條目數, 總字數)。"""
    path = os.path.join(SKILL_MEMORY_DIR, f"{skill}.md")
    if content in existing_entries(path):
        return False, len(existing_entries(path)), 0
    os.makedirs(SKILL_MEMORY_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(
                f"# {skill} 經驗記憶\n\n"
                f"> 使用者要求記住、專屬於此技能的經驗。載入 `{skill}` 規格文件時會自動附在後面；"
                f"由 `scripts/modify_memory_cmd.py \"...\" --skill {skill}` 寫入，可直接編輯或刪除過時條目。\n\n"
            )
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- [{stamp}] {content}\n")
    entries = existing_entries(path)
    return True, len(entries), sum(len(e) for e in entries)


def _size_warning(skill, n, total_chars):
    if n > WARN_ENTRIES or total_chars > WARN_CHARS:
        return (f"\n⚠️ 此技能的經驗記憶已有 {n} 則／約 {total_chars} 字，每次載入規格都會進入上下文，"
                f"建議整併或刪除過時條目（檔案：skills_system/memory/{skill}.md）。")
    return ""


def move_last_to_skill(skill):
    """把 Memory.md 最新（最後一則）的全域條目搬到 skills_system/memory/<skill>.md，保留原本的寫入時間。
    給「全域寫入後問使用者要不要改綁、使用者同意」這一步用。"""
    try:
        skill, err = _check_skill(skill, "--move-last-to-skill")
        if err:
            return err
        if not os.path.exists(MEMORY_FILE):
            return "[ERROR] 目前沒有全域記憶（Memory.md 不存在），沒有可以改綁的條目。"
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
        idx = next((i for i in range(len(lines) - 1, -1, -1)
                    if lines[i].lstrip().startswith("[") and _ENTRY_RE.match(lines[i].strip())), None)
        if idx is None:
            return "[ERROR] Memory.md 裡沒有任何記憶條目，沒有可以改綁的條目。"
        line = lines[idx].strip()
        stamp = line[1:line.index("]")]
        content = _ENTRY_RE.match(line).group(1).strip()
        written, n, total = _append_skill_entry(skill, stamp, content)
        del lines[idx]
        if idx > 0 and idx - 1 < len(lines) and not lines[idx - 1].strip():
            del lines[idx - 1]   # 條目之間的空行一起拿掉
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        where = "已存在於" if not written else "已移到"
        return (f"[PASS] 最新一則全域記憶{where}技能 {skill} 的經驗記憶（全域那一則已移除）：之後只在載入 {skill} 規格時出現，"
                f"平常不佔上下文。\nMemory: [{stamp}] {content}" + _size_warning(skill, n, total))
    except Exception as e:
        return f"[ERROR] 記憶改綁失敗: {e}"


def execute(memory_content, skill=None):
    try:
        if not memory_content or not memory_content.strip():
            return "[ERROR] 記憶內容不可為空。格式：\"問題種類 | 問題描述 | 解決方法或結論\""
        content = " ".join(memory_content.split())  # 壓成單行，避免多行內容破壞條目格式
        now = datetime.now().strftime("%m-%d %H:%M")

        if skill is not None:
            skill, err = _check_skill(skill)
            if err:
                return err
            written, n, total = _append_skill_entry(skill, now, content)
            if not written:
                return f"[PASS] 相同內容已存在於技能 {skill} 的經驗記憶，未重複寫入。"
            return (
                f"[PASS] 成功寫入技能 {skill} 的經驗記憶（目前共 {n} 則），"
                f"之後載入 {skill} 規格文件時會一併帶入，平常不佔上下文。\n"
                f"Memory: [{now}] {content}" + _size_warning(skill, n, total)
            )

        # 全域：Memory.md
        if content in existing_entries(MEMORY_FILE):
            return "[PASS] 相同內容已存在於長期記憶，未重複寫入。"
        if not os.path.exists(MEMORY_FILE):
            with open(MEMORY_FILE, "w", encoding="utf-8") as f:
                f.write("# Long Term Memory\n\n")
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n[{now}] {content}\n")
        msg = (
            "[PASS] 成功寫入長期記憶（全域，每輪常駐 system prompt）\n"
            f"Memory: [{now}] {content}"
        )
        mentioned = mentioned_skills(content)
        if len(mentioned) == 1:
            x = mentioned[0]
            msg += (
                f"\nℹ️ 這則記憶提到技能 {x}。選技能的時候就要知道的規則（例如某技能不能用在容器內）留在全域就好；"
                f"如果它只在使用 {x} 時才需要（參數、前置條件、錯誤處理），可以改綁在 {x}（載入它的規格時才出現、平常不佔上下文）。"
                f"請在 reply 用一句話問使用者要不要改綁；使用者同意就執行 scripts/modify_memory_cmd.py --move-last-to-skill {x}，"
                f"不同意就維持全域，不要自己決定。"
            )
        return msg

    except Exception as e:
        return f"[ERROR] 記憶寫入失敗: {e}"


if __name__ == "__main__":
    try:
        content, skill, move_to = parse_args(sys.argv[1:])
        print(move_last_to_skill(move_to) if move_to is not None else execute(content, skill))
    except Exception as e:
        print(f"[ERROR] {e}")
        sys.exit(1)
