"""
迴歸測試：自我進化機制成熟度改善（manager.SkillManager.create_skill()）。

驗證範圍：
- 語法驗證會在寫入任何檔案之前擋下壞掉的 code_body。
- 多參數技能（逗號分隔）可以端到端建立並正確執行。
- 規格書的「執行邏輯分支」是依實際程式碼靜態分析產生，不是通用樣板。
- 煙霧測試的訊息一定會出現在回傳結果中（不論成功或失敗傾向）。
- 版本控管：同名技能重建前會備份舊版本，規格書／索引會原地更新而非略過。
- 備份輪替上限（MAX_BACKUPS_PER_FILE）。

本測試會在真正的專案目錄（scripts/、tools/、INDEX.md）建立以 zz_test_selfevo_
為前綴的暫時技能，並在結束時清乾淨，不會影響任何正式技能。
"""
import sys
import os
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from manager import SkillManager

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


mgr = SkillManager()  # 使用真正的專案目錄——所有建立的東西都要清乾淨
TEST_PREFIX = "zz_test_selfevo_"


def cleanup(name):
    for p in [
        os.path.join(mgr.scripts_dir, f"{name}_cmd.py"),
        os.path.join(mgr.tools_dir, f"{name}.md"),
    ]:
        if os.path.exists(p):
            os.remove(p)
    for hist_dir in [os.path.join(mgr.scripts_dir, ".history"), os.path.join(mgr.tools_dir, ".history")]:
        if os.path.isdir(hist_dir):
            for f in os.listdir(hist_dir):
                if f.startswith(f"{name}_cmd.py.") or f.startswith(f"{name}.md."):
                    os.remove(os.path.join(hist_dir, f))
    idx = mgr.index_file
    if os.path.exists(idx):
        with open(idx, encoding="utf-8") as f:
            lines = f.readlines()
        lines = [l for l in lines if f"| {name} |" not in l]
        with open(idx, "w", encoding="utf-8") as f:
            f.writelines(lines)


# =========================================================
# 1. 語法驗證會擋下壞掉的程式碼（縮排／語法錯誤）
# =========================================================
name1 = f"{TEST_PREFIX}badsyntax"
cleanup(name1)
result = mgr.create_skill(name1, "測試語法驗證", "val", 'if val > 10:\nreturn "bad indentation here"')
check("syntax-broken code_body is rejected", result.startswith("❌"))
check("rejection message mentions syntax error", "語法錯誤" in result)
check("no script file was created for the rejected skill", not os.path.exists(os.path.join(mgr.scripts_dir, f"{name1}_cmd.py")))
check("no doc file was created for the rejected skill", not os.path.exists(os.path.join(mgr.tools_dir, f"{name1}.md")))

# =========================================================
# 2. 多參數支援（如 workitem_est 風格，3 個參數）
# =========================================================
name2 = f"{TEST_PREFIX}multiparam"
cleanup(name2)
code = 'return "MATCH" if str(a) == str(b) and int(c) > 0 else "NOMATCH"'
result = mgr.create_skill(name2, "測試多參數", "a,b,c", code)
check("multi-param skill creation succeeds", result.startswith("✅"))

script_path = os.path.join(mgr.scripts_dir, f"{name2}_cmd.py")
check("script file exists", os.path.exists(script_path))
script_src = open(script_path, encoding="utf-8").read()
check("generated function signature has all 3 params", "def execute(a, b, c):" in script_src)

res = subprocess.run([sys.executable, script_path, "x", "x", "5"], capture_output=True, text=True)
check("multi-param script actually runs correctly with 3 real args", res.stdout.strip() == "MATCH")
res2 = subprocess.run([sys.executable, script_path, "x", "y", "5"], capture_output=True, text=True)
check("multi-param script correctly returns NOMATCH branch", res2.stdout.strip() == "NOMATCH")
res3 = subprocess.run([sys.executable, script_path, "only_one_arg"], capture_output=True, text=True)
check("too few args prints usage with all 3 param names", "<a> <b> <c>" in res3.stdout)

doc_path = os.path.join(mgr.tools_dir, f"{name2}.md")
doc_src = open(doc_path, encoding="utf-8").read()
check("doc lists all 3 params", "`a`" in doc_src and "`b`" in doc_src and "`c`" in doc_src)

# =========================================================
# 3. 規格書品質：邏輯分支是從真實程式碼分析出來，不是通用樣板
# =========================================================
check("doc's logic section reflects the ACTUAL condition from code_body", "a) == str(b)" in doc_src)
check("doc's logic section shows the actual return value MATCH", '`"MATCH"`' in doc_src or "MATCH" in doc_src)
check("doc no longer contains the old generic boilerplate sentence", "非人工手寫" not in doc_src)

# =========================================================
# 4. 煙霧測試訊息一定會出現
# =========================================================
check("create_skill's return message includes smoke-test result", "煙霧測試" in result)

name3 = f"{TEST_PREFIX}smokefail"
cleanup(name3)
result3 = mgr.create_skill(name3, "測試除以零", "x,y", "return int(x) / int(y)")
check("smoke test note is present regardless of outcome", "煙霧測試" in result3)

# =========================================================
# 5. 版本控管：同名技能重建會備份舊版本
# =========================================================
name4 = f"{TEST_PREFIX}versioned"
cleanup(name4)
r1 = mgr.create_skill(name4, "第一版描述", "v", 'return "v1"')
check("first creation has no backup note (nothing to back up yet)", "已備份至" not in r1)

script4 = os.path.join(mgr.scripts_dir, f"{name4}_cmd.py")
res_v1 = subprocess.run([sys.executable, script4, "x"], capture_output=True, text=True)
check("v1 script behaves as v1", res_v1.stdout.strip() == "v1")

r2 = mgr.create_skill(name4, "第二版描述（已更新）", "v", 'return "v2"')
check("second creation with same name reports a backup was made", "已備份至" in r2)

res_v2 = subprocess.run([sys.executable, script4, "x"], capture_output=True, text=True)
check("script now behaves as v2 (overwritten)", res_v2.stdout.strip() == "v2")

history_dir = os.path.join(mgr.scripts_dir, ".history")
backups = [f for f in os.listdir(history_dir) if f.startswith(f"{name4}_cmd.py.")] if os.path.isdir(history_dir) else []
check("exactly one backup of the v1 script exists in .history/", len(backups) == 1)
if backups:
    backed_up_content = open(os.path.join(history_dir, backups[0]), encoding="utf-8").read()
    check("the backup actually contains the v1 code, not v2", '"v1"' in backed_up_content)

doc4 = open(os.path.join(mgr.tools_dir, f"{name4}.md"), encoding="utf-8").read()
check("doc was regenerated for v2 (not left stale from v1)", "第二版描述" in doc4)
index_content = open(mgr.index_file, encoding="utf-8").read()
check("INDEX.md updated in place (v2 description), not duplicated", index_content.count(f"| {name4} |") == 1 and "第二版描述" in index_content)

# =========================================================
# 6. 備份輪替上限（MAX_BACKUPS_PER_FILE = 5）
# =========================================================
name5 = f"{TEST_PREFIX}rotation"
cleanup(name5)
for i in range(8):
    mgr.create_skill(name5, f"版本 {i}", "v", f'return "v{i}"')
rotation_backups = [f for f in os.listdir(history_dir) if f.startswith(f"{name5}_cmd.py.")]
check("backup rotation caps at MAX_BACKUPS_PER_FILE=5 even after 8 recreations", len(rotation_backups) <= 5)

# =========================================================
# 清理所有測試產生的暫時技能
# =========================================================
for n in [name1, name2, name3, name4, name5]:
    cleanup(n)

print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}"))
sys.exit(1 if failures else 0)
