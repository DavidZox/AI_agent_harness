"""workpackage_status：查 work package 執行狀態——一幀快照，或 --watch 秒 觀察一段時間並整理變化。

資料來自 web_console 的 WebSocket /api/ws 快照（"orchestrtor" 鍵＝work package 狀態；其餘＝distribute 佇列、機器人、事件），
站點以 /api/topology 附上語意名稱。一幀只是瞬間；使用者想知道「一段時間內有沒有變化」時用 --watch，會整理站點推進、
OverPending 進出、機器人狀態變化與新增事件，開頭 [PASS][digest] 讓 harness 放寬回傳門檻。共用層見 _rmf_common.py。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _rmf_common import MAX_WATCH_SECONDS, _ids, fetch_topology, parse_seconds, split_url, station_text, ws_collect, ws_read_json

USAGE = (
    "用法: scripts/workpackage_status_cmd.py [task_id] [--watch 秒] [--url 位址]\n"
    "  不帶 task_id：全部 work package、distribute 佇列、機器人、最近事件的一幀快照；帶 task_id 只看它。\n"
    f"  --watch 秒（1～{MAX_WATCH_SECONDS}）：持續收幀並整理這段時間的變化（站點推進、OverPending 進出、機器人狀態、事件）。"
)


def parse_args(argv):
    """回傳 (task_id, watch, base_url, error)。"""
    argv, base_url, err = split_url(argv)
    if err:
        return None, None, None, err
    task_id, watch, i = None, None, 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--watch":
            if i + 1 >= len(argv):
                return None, None, None, f"[ERROR] --watch 後面需要秒數。\n{USAGE}"
            watch, err = parse_seconds(argv[i + 1], "--watch")
            if err:
                return None, None, None, err
            i += 2
        elif tok.startswith("--"):
            return None, None, None, f"[ERROR] 不認識的選項 {tok}。\n{USAGE}"
        elif task_id is not None:
            return None, None, None, f"[ERROR] 只接受一個 task_id，收到 {task_id!r} 與 {tok!r}。\n{USAGE}"
        else:
            task_id = tok
            i += 1
    return task_id, watch or None, base_url, None

# ---------------------------------------------------------------- 一幀快照
def _package_line(p, topo=None):
    cur = p.get("current_item") or {}
    idx, total = p.get("station_index", 0), p.get("station_count") or len(p.get("stations") or [])
    stations = p.get("stations") or []
    station = cur.get("station") or (stations[idx] if idx < len(stations) else "?")
    if p.get("status") == "COMPLETED":
        progress = f"已完成（共 {p.get('iteration', 0)} 輪）"
    else:
        progress = f"第 {idx + 1}/{total} 站 {station_text(station, topo)}"
        if cur:
            robot = f"機器人 {cur.get('robot_id')}" if cur.get("robot_id") else ("⏳ 等待機器人" if cur.get("waiting_for_robot") else "機器人未知")
            progress += f"，工作項 {cur.get('status')}，{robot}"
    loop = f"｜循環 {p.get('loop_count') or '∞'} 輪，目前第 {p.get('iteration', 0) + 1} 輪" if p.get("loop") else ""
    return (f"- {p.get('package_id')} {p.get('status')}：{progress}{loop}｜完成 {p.get('items_done', 0)}/{p.get('items_total', 0)} 項"
            f"｜路線 {' → '.join(station_text(s, topo) for s in stations)}")

def render_status(data, task_id=None, topo=None, head=None):
    orch = data.get("orchestrtor") or {}
    packages = orch.get("packages") or []
    lines = [head or f"[PASS] 目前狀態（web_console /api/ws 快照 distribute #{data.get('step_counter', '?')} / orchestrtor #{orch.get('step_counter', '?')}）"]
    if task_id:
        hits = [p for p in packages if p.get("package_id") == task_id]
        if not hits:
            lines.append(f"orchestrtor 清單中沒有 work package {task_id}：可能未送達、已被刪除，或 orchestrtor 沒在跑"
                         f"（目前清單 {len(packages)} 個{'：' + '、'.join(str(p.get('package_id', '?')) for p in packages[:10]) if packages else ''}）。")
        else:
            lines += [_package_line(p, topo) for p in hits]
        related = [t for key in ("processing_tasks", "raw_tasks", "overpending_tasks", "buffer_tasks")
                   for t in (data.get(key) or []) if str(t.get("id", "")).startswith(f"{task_id}::")]
        if related:
            lines.append("distribute 佇列中相關的工作項：" + "；".join(
                f"{t.get('id')}" + (f" → {t.get('robot')}" if t.get("robot") else "") + (f"（OverPending 已等 {t.get('wait')}s）" if "wait" in t else "")
                for t in related))
    else:
        if not orch:
            lines.append("orchestrtor 快照尚未收到（orchestrtor 可能沒在跑，或 web_console 剛啟動）。")
        else:
            running = sum(1 for p in packages if p.get("status") == "RUNNING")
            lines.append(f"orchestrtor work packages：{len(packages)} 個（RUNNING {running}，COMPLETED {len(packages) - running}）")
            lines += [_package_line(p, topo) for p in packages[:15]]
        ovp, proc, raw = data.get("overpending_tasks") or [], data.get("processing_tasks") or [], data.get("raw_tasks") or []
        lines.append(f"distribute：狀態 {data.get('state', '?')}｜buffer {len(data.get('buffer_tasks') or [])}｜raw {len(raw)}"
                     + (f"（{'、'.join(f'{t.get('id')} 等 {t.get('age')}s' for t in raw[:5])}）" if raw else "")
                     + f"｜processing {len(proc)}" + (f"（{'、'.join(f'{t.get('id')}→{t.get('robot') or '-'}' for t in proc[:5])}）" if proc else "")
                     + f"｜overpending {len(ovp)}" + (f"（{'、'.join(f'{t.get('id')} 已等 {t.get('wait')}s' for t in ovp[:5])}；此刻在逾時區，可用 overpending_cancel 刪除，超過 recovery 秒數會回流 raw）" if ovp else ""))
        robots = data.get("robots") or {}
        if robots:
            lines.append("機器人：" + "；".join(
                f"{rid} {info.get('status', '?')} {info.get('state', '?')}" + (f"（{info.get('last_task_id')}）" if info.get('last_task_id') not in (None, 'None') else "")
                + (f" 目標 {station_text(info['target_id'], topo)}" if info.get("target_id") else "")
                for rid, info in list(robots.items())[:8]))
    for label, events in (("orchestrtor", orch.get("events") or []), ("distribute", data.get("events") or [])):
        if events:
            lines.append(f"最近事件（{label}）：" + " ｜ ".join(events[-3:]))
    return "\n".join(lines)

# ---------------------------------------------------------------- 一段時間的變化
def watch_mode(base_url, seconds, task_id=None, topo=None):
    frames, err = ws_collect(base_url, seconds)
    if err:
        return err
    if not frames:
        return "[ERROR] 觀察期間沒有收到任何快照。"
    changes = []
    prev = None
    events_seen, event_lines = set(), []
    for t, data in frames:
        for src, evs in (("orchestrtor", (data.get("orchestrtor") or {}).get("events") or []), ("distribute", data.get("events") or [])):
            for e in evs:
                if e not in events_seen:
                    events_seen.add(e)
                    event_lines.append(f"{src}: {e}")
        if prev is None:
            prev = data
            continue
        pk_prev = {p.get("package_id"): p for p in (prev.get("orchestrtor") or {}).get("packages") or []}
        pk_now = {p.get("package_id"): p for p in (data.get("orchestrtor") or {}).get("packages") or []}
        for pid, p in pk_now.items():
            if task_id and pid != task_id:
                continue
            q = pk_prev.get(pid)
            if q is None:
                changes.append(f"+{t:.1f}s 新 work package {pid}")
                continue
            if (p.get("station_index"), p.get("iteration")) != (q.get("station_index"), q.get("iteration")):
                changes.append(f"+{t:.1f}s {pid} 第 {q.get('station_index', 0) + 1} 站 → 第 {p.get('station_index', 0) + 1} 站"
                               + (f"（第 {p.get('iteration', 0) + 1} 輪）" if p.get("iteration") != q.get("iteration") else "")
                               + f"，目前 {station_text((p.get('current_item') or {}).get('station'), topo)}")
            elif (p.get("current_item") or {}).get("robot_id") != (q.get("current_item") or {}).get("robot_id"):
                changes.append(f"+{t:.1f}s {pid} 工作項改由 {(p.get('current_item') or {}).get('robot_id') or '（等待機器人）'} 承接")
            if p.get("status") != q.get("status"):
                changes.append(f"+{t:.1f}s {pid} {q.get('status')} → {p.get('status')}")
        for pid in set(pk_prev) - set(pk_now):
            if not task_id or pid == task_id:
                changes.append(f"+{t:.1f}s work package {pid} 從清單消失（被刪除）")
        for key, label in (("overpending_tasks", "OverPending"), ("raw_tasks", "raw"), ("processing_tasks", "processing")):
            a, b = set(_ids(prev, key)), set(_ids(data, key))
            for tid in sorted(b - a):
                changes.append(f"+{t:.1f}s {tid} 進入 {label}")
            for tid in sorted(a - b):
                changes.append(f"+{t:.1f}s {tid} 離開 {label}")
        rp, rn = prev.get("robots") or {}, data.get("robots") or {}
        for rid, info in rn.items():
            before = rp.get(rid) or {}
            for f in ("status", "state"):
                if before.get(f) != info.get(f) and before:
                    changes.append(f"+{t:.1f}s {rid} {f} {before.get(f)} → {info.get(f)}")
        prev = data
    span = frames[-1][0]
    last = frames[-1][1]
    head = f"[PASS][digest] 觀察 {seconds} 秒（收到 {len(frames)} 幀，實際 {span:.1f} 秒）"
    lines = [head, "變化：" + ("" if changes else "沒有任何狀態變化（工作包站點、佇列成員、機器人狀態都相同）")]
    lines += changes[:25]
    if len(changes) > 25:
        lines.append(f"…另有 {len(changes) - 25} 筆變化省略")
    new_events = [e for e in event_lines if not any(e.split(": ", 1)[-1] in (evs or []) for evs in
                  ((frames[0][1].get("orchestrtor") or {}).get("events") or [], frames[0][1].get("events") or []))]
    if new_events:
        lines.append(f"期間新增事件（{len(new_events)} 則）：" + " ｜ ".join(new_events[-6:]))
    # 結尾一行摘要（完整內容用不帶 --watch 的一幀快照再看），避免超過工具回傳門檻被精簡掉
    pk = [p for p in (last.get("orchestrtor") or {}).get("packages") or [] if not task_id or p.get("package_id") == task_id]
    pk_text = "；".join(f"{p.get('package_id')} {p.get('status')} 第 {p.get('station_index', 0) + 1}/{p.get('station_count')} 站 {station_text((p.get('current_item') or {}).get('station'), topo)}"
                        + (f" 機器人 {(p.get('current_item') or {}).get('robot_id')}" if (p.get('current_item') or {}).get('robot_id') else " ⏳") for p in pk[:6]) or "沒有 work package"
    lines.append(f"結束時：{pk_text}｜distribute raw {len(last.get('raw_tasks') or [])}／processing {len(last.get('processing_tasks') or [])}／overpending {len(last.get('overpending_tasks') or [])}"
                 f"（{'、'.join(_ids(last, 'overpending_tasks')[:4])}）" if last.get("overpending_tasks") else
                 f"結束時：{pk_text}｜distribute raw {len(last.get('raw_tasks') or [])}／processing {len(last.get('processing_tasks') or [])}／overpending 0")
    return "\n".join(lines)

def status_mode(base_url, task_id=None, watch=None):
    topo, _ = fetch_topology(base_url)   # 拿不到語意就只顯示代號
    if watch:
        return watch_mode(base_url, watch, task_id, topo)
    data, err = ws_read_json(base_url)
    if err:
        return err
    return render_status(data, task_id, topo)

def main(argv):
    task_id, watch, base_url, err = parse_args(argv)
    if err:
        return err
    return status_mode(base_url, task_id, watch)


if __name__ == "__main__":
    try:
        print(main(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] workpackage_status 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
