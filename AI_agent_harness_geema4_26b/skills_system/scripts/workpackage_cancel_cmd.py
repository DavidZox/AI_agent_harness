"""workpackage_cancel：取消（刪除）一個 work package——web_console DELETE /api/work_packages/{id} → topic cancel_work_package。

orchestrtor 收到後從清單移除、不再派下一站；已送到 distribute 的當前站可能仍會被機器人執行完。送出後讀一幀快照確認。
刪除卡在 OverPending 逾時區的單站任務是另一個技能 overpending_cancel。共用層見 _rmf_common.py。
"""
import os
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _rmf_common import _http, split_url, ws_read_json

USAGE = "用法: scripts/workpackage_cancel_cmd.py <task_id> [--url 位址]（task_id 用 workpackage_status 查）"


def parse_args(argv):
    """回傳 (task_id, base_url, error)。"""
    argv, base_url, err = split_url(argv)
    if err:
        return None, None, err
    bad = [a for a in argv if a.startswith("--")]
    if bad:
        return None, None, f"[ERROR] 不認識的選項 {bad[0]}。\n{USAGE}"
    if len(argv) != 1:
        return None, None, f"[ERROR] 需要 1 個參數 <task_id>，收到 {len(argv)} 個。\n{USAGE}"
    return argv[0], base_url, None

def cancel_mode(base_url, task_id):
    task_id = (task_id or "").strip()
    if not task_id:
        return f"[ERROR] 需要 work package 的 task_id。\n{USAGE}"
    status, text, err = _http("DELETE", base_url, f"/api/work_packages/{quote(task_id, safe='')}")
    if err:
        return err
    time.sleep(0.6)
    data, ws_err = ws_read_json(base_url)
    if ws_err:
        confirm = f"（無法讀取快照確認：{ws_err[8:]}）"
    else:
        ids = [p.get("package_id") for p in (data.get("orchestrtor") or {}).get("packages") or []]
        confirm = ("快照確認：orchestrtor 清單中已不存在 " + task_id) if task_id not in ids else \
                  f"快照中 {task_id} 仍在清單（請求剛送出，稍後用 workpackage_status {task_id} 再確認；若一直存在表示 orchestrtor 沒收到）"
    return (f"[PASS] 已送出取消 work package {task_id} 的請求（HTTP {status}，web_console 已轉發 cancel_work_package）\n"
            f"web_console 回應: {text[:300]}\n{confirm}\n注意：orchestrtor 只會停止派下一站並移除清單；已送到 distribute 的當前站可能仍會被機器人執行完。")

def main(argv):
    task_id, base_url, err = parse_args(argv)
    if err:
        return err
    return cancel_mode(base_url, task_id)


if __name__ == "__main__":
    try:
        print(main(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] workpackage_cancel 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
