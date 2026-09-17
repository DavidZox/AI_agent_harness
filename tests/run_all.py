"""
一次跑完 tests/ 目錄下所有 verify_*.py 迴歸測試，並印出整體總結。

用法：
    python3 tests/run_all.py

會依序（非平行，因為都會用到同一個本機 Ollama 服務）以子行程執行每個
verify_*.py，彙整各自的 exit code，最後印出整體 PASS/FAIL 總覽。任何一個
子測試失敗，本腳本最終也會以非 0 狀態碼結束，方便串進 CI 或 pre-commit。
"""
import subprocess
import sys
import os

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

TEST_FILES = [
    "verify_refactor.py",
    "verify_budget_monitor.py",
    "verify_skill_lifecycle.py",
    "verify_heartbeat.py",
    "verify_self_evolution.py",
]

results = {}

for fname in TEST_FILES:
    path = os.path.join(TESTS_DIR, fname)
    print("=" * 60)
    print(f"RUNNING: {fname}")
    print("=" * 60)
    proc = subprocess.run([sys.executable, path])
    results[fname] = proc.returncode

print("\n" + "=" * 60)
print("SUMMARY")
print("=" * 60)
all_passed = True
for fname, code in results.items():
    status = "PASS" if code == 0 else "FAIL"
    if code != 0:
        all_passed = False
    print(f"[{status}] {fname} (exit code {code})")

print("\n" + ("ALL TEST SUITES PASSED" if all_passed else "SOME TEST SUITES FAILED"))
sys.exit(0 if all_passed else 1)
