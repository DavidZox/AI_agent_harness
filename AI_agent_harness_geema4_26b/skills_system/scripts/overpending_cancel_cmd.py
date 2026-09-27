"""overpending_cancel：刪除卡在 distribute OverPending 逾時區的單站任務——web_console DELETE /api/overpending_tasks/{id}
→ topic cancel_overpending_task。

OverPending 不是終點：任務在 raw 佇列等超過 threshold_ovp_sec（預設 10 秒）才進 OverPending，再過 threshold_recovery_sec
（預設 10 秒）又回流 raw，直到有機器人領走。DELETE 只對「此刻在 OverPending」的任務有效，所以本技能先看快照：任務此刻在
逾時區才刪；不在時回報所在佇列（不是錯誤），加 --wait 秒 會持續收幀，等它進入逾時區立刻刪，再確認消失。
取消整個 work package 是另一個技能 workpackage_cancel。共用層見 _rmf_common.py。
"""
import os
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _rmf_common import MAX_WATCH_SECONDS, _WsError, _http, _ids, _locate_task, parse_seconds, split_url, ws_frames, ws_read_json

USAGE = (
    "用法: scripts/overpending_cancel_cmd.py <任務id> [--wait 秒] [--url 位址]\n"
    "  任務 id 形如 <package>::<輪>::<站>::<序>（workpackage_status 可查）。\n"
    f"  --wait 秒（1～{MAX_WATCH_SECONDS}）：任務此刻不在逾時區時，等它進來再刪；不加就只看一幀、不在就回報。"
)


def parse_args(argv):
    """回傳 (task_id, wait, base_url, error)。"""
    argv, base_url, err = split_url(argv)
    if err:
        return None, None, None, err
    task_id, wait, i = None, None, 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--wait":
            if i + 1 >= len(argv):
                return None, None, None, f"[ERROR] --wait 後面需要秒數。\n{USAGE}"
            wait, err = parse_seconds(argv[i + 1], "--wait")
            if err:
                return None, None, None, err
            i += 2
        elif tok.startswith("--"):
            return None, None, None, f"[ERROR] 不認識的選項 {tok}。\n{USAGE}"
        elif task_id is not None:
            return None, None, None, f"[ERROR] 只接受一個任務 id，收到 {task_id!r} 與 {tok!r}。\n{USAGE}"
        else:
            task_id = tok
            i += 1
    if not task_id:
        return None, None, None, f"[ERROR] 需要 distribute 佇列中的任務 id（workpackage_status 可查）。\n{USAGE}"
    return task_id, wait, base_url, None

def cancel_overpending_mode(base_url, task_id, wait):
    task_id = (task_id or "").strip()
    if not task_id:
        return f"[ERROR] 需要 distribute 佇列中的任務 id（workpackage_status 可查）。\n{USAGE}"
    wait = wait or 0
    where, extra, elapsed = None, "", 0.0
    try:
        for t, data in ws_frames(base_url, wait if wait > 0 else None):
            where, extra = _locate_task(data, task_id)
            elapsed = t
            if where == "OverPending":
                break
            if wait == 0:
                break
    except _WsError as e:
        return str(e)
    if where != "OverPending":
        if where is None:
            return (f"[PASS] 未刪除：{task_id} 目前不在 distribute 的任何佇列（觀察 {elapsed:.1f} 秒）。可能已被機器人領走並執行、"
                    f"id 打錯（工作項 id 形如 <package>::<輪>::<站>::<序>，用 workpackage_status 查），或 distribute 沒在跑。")
        return (f"[PASS] 未刪除：{task_id} 目前在 {where} 佇列{extra}，不在 OverPending 逾時區，此刻無法刪除"
                f"（觀察了 {elapsed:.1f} 秒）。任務在 raw 等超過 threshold_ovp_sec（預設 10 秒）才會進入 OverPending，"
                f"進入後 threshold_recovery_sec（預設 10 秒）內就要刪；請加 --wait 30 讓我等它進逾時區再刪，或改用 workpackage_cancel 取消整個 work package。")
    status, text, err = _http("DELETE", base_url, f"/api/overpending_tasks/{quote(task_id, safe='')}")
    if err:
        return err
    time.sleep(0.6)
    data, ws_err = ws_read_json(base_url)
    if ws_err:
        confirm = f"（無法讀取快照確認：{ws_err[8:]}）"
    else:
        ids = _ids(data, "overpending_tasks")
        confirm = f"快照確認：OverPending 佇列中已不存在 {task_id}" if task_id not in ids else f"快照中 {task_id} 仍在 OverPending 佇列（稍後用 workpackage_status 再確認）"
    waited = f"（等了 {elapsed:.1f} 秒它才進入逾時區）" if elapsed > 0.6 else ""
    return (f"[PASS] 已刪除 OverPending 任務 {task_id}{waited}（HTTP {status}，web_console 已轉發 cancel_overpending_task）\n"
            f"web_console 回應: {text[:300]}\n{confirm}\n注意：只影響已進入 OverPending 佇列的任務；它所屬的 work package 若還在 RUNNING，orchestrtor 不會自動補派，需要的話用 workpackage_cancel 取消整個 work package。")

def main(argv):
    task_id, wait, base_url, err = parse_args(argv)
    if err:
        return err
    return cancel_overpending_mode(base_url, task_id, wait)


if __name__ == "__main__":
    try:
        print(main(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] overpending_cancel 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
