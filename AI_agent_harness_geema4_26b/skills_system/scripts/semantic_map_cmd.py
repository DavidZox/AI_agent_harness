"""semantic_map：語義地圖與目前佈局——站點代號、語意名稱、說明（含 VLM 判讀紀錄）、座標、路段，並用同一幀快照把
機器人（座標最近的站、前往的站、所在路段）與 work package 當前派工的站對到每個站點。

資料全部問 web_console：GET /api/topology（拓譜圖 + semantics.yaml）與 WebSocket /api/ws 一幀快照，不讀任何地圖檔，
所以看到的永遠是 web_console 目前載入的場域。不帶參數＝全部站點的總覽；帶關鍵字或站點代號＝
該站／路段的完整說明與連接路段；--stations／--robots 只列站點或機器人清單（/api/stations、/api/robots）。共用層見 _rmf_common.py。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _rmf_common import _clip, _edge_endpoints, _http, fetch_topology, occupancy, split_url, station_text, ws_read_json

USAGE = (
    "用法: scripts/semantic_map_cmd.py [關鍵字或站點代號] [--stations] [--robots] [--url 位址]\n"
    "  不帶參數：全部站點（代號=語意名稱）與目前有機器人／工作包的站點；帶關鍵字或代號：該站／路段的完整說明。\n"
    "  --stations：只列站點清單（含語意名稱）；--robots：只列機器人清單。"
)


def parse_args(argv):
    """回傳 (query, mode, base_url, error)；mode 為 'map'／'stations'／'robots'。"""
    argv, base_url, err = split_url(argv)
    if err:
        return None, None, None, err
    query, flags = None, []
    for tok in argv:
        if tok in ("--stations", "--robots"):
            flags.append(tok)
        elif tok.startswith("--"):
            return None, None, None, f"[ERROR] 不認識的選項 {tok}。\n{USAGE}"
        elif query is not None:
            return None, None, None, f"[ERROR] 只接受一個關鍵字，收到 {query!r} 與 {tok!r}（含空白請用引號包住）。\n{USAGE}"
        else:
            query = tok
    if len(flags) > 1 or (flags and query):
        return None, None, None, f"[ERROR] --stations／--robots 一次只能用一個，且不能與關鍵字並用。\n{USAGE}"
    return query, flags[0][2:] if flags else "map", base_url, None

# ---------------------------------------------------------------- 語義地圖 + 目前佈局
def map_mode(base_url, query=None):
    topo, err = fetch_topology(base_url)
    if err:
        return err
    snapshot, ws_err = ws_read_json(base_url)
    per_station, robot_lines = occupancy(snapshot, topo) if snapshot else ({}, [])
    live_note = "" if snapshot else f"（即時快照讀取失敗：{ws_err[8:] if ws_err else '?'}，以下沒有機器人／工作包位置）"
    nodes, edges = topo["nodes"], topo["edges"]
    sem_nodes = sum(1 for n in nodes if n.get("has_semantic"))
    sem_edges = sum(1 for e in edges if e.get("has_semantic"))
    head = f"[PASS] 語義地圖：{len(nodes)} 站、{len(edges)} 邊，有語意名稱／說明的站 {sem_nodes}、邊 {sem_edges}（web_console /api/topology + 即時快照）{live_note}"

    if not query:
        # 總覽：站點=語意名稱 一行列完、有東西的站點另列；細節用關鍵字查（超過門檻時由 harness 的獨立 session 依任務擷取）
        pairs = [f"{n['name']}={n.get('label')}" if n.get("label") and n.get("label") != n["name"] else n["name"] for n in nodes]
        lines = [head, "站點（代號=語意名稱）：" + "、".join(pairs)]
        busy = [f"{station_text(name, topo)}：{'；'.join(items)}" for name, items in per_station.items()]
        lines.append("目前有機器人或工作包的站點：" + ("；".join(busy) if busy else "沒有"))
        if robot_lines:
            lines.append("機器人：" + "；".join(robot_lines))
        lines.append("用 semantic_map <關鍵字或站點代號> 看該站／路段的完整說明（VLM 判讀紀錄等）。")
        return "\n".join(lines)

    q = query.strip().lower()
    hit_nodes = [n for n in nodes if q == str(n["name"]).lower() or q in str(n.get("label") or "").lower() or q in str(n.get("description") or "").lower()]
    hit_edges = [e for e in edges if q in str(e.get("label") or "").lower() or q in str(e.get("description") or "").lower()
                 or q in {x.lower() for x in _edge_endpoints(e, topo)}]
    if not hit_nodes and not hit_edges:
        return f"{head}\n找不到與「{query}」相關的站點或路段（比對代號、語意名稱、說明）。現有站點：{', '.join(n['name'] for n in nodes[:20])}{'…' if len(nodes) > 20 else ''}"
    lines = [head, f"與「{query}」相關：{len(hit_nodes)} 站、{len(hit_edges)} 路段"]
    for n in hit_nodes[:6]:
        lines.append(f"■ {n['name']}｜語意名稱：{n.get('label') or '—'}｜id {n.get('id')}｜座標 ({n.get('x')}, {n.get('y')})")
        lines.append(f"  說明：{_clip(n.get('description'), 500) or '（尚未設定）'}")
        lines.append(f"  目前：{'；'.join(per_station.get(n['name'], [])) or '沒有機器人在此或前往、沒有工作包派工中'}")
        linked = [e for e in edges if str(e.get("start_id")) == str(n.get("id")) or str(e.get("end_id")) == str(n.get("id"))]
        if linked:
            lines.append("  連接路段：" + "；".join(f"#{e.get('id')} {'→'.join(_edge_endpoints(e, topo))}" + (f"「{e.get('label')}」" if e.get("label") else "") for e in linked[:8]))
    for e in [e for e in hit_edges if e not in []][:6]:
        a, b = _edge_endpoints(e, topo)
        lines.append(f"■ 路段 #{e.get('id')} {a}→{b}｜語意名稱：{e.get('label') or '—'}")
        lines.append(f"  說明：{_clip(e.get('description'), 400) or '（尚未設定）'}")
    if robot_lines:
        lines.append("機器人：" + "；".join(robot_lines))
    return "\n".join(lines)

# ---------------------------------------------------------------- 站點／機器人清單
def list_mode(base_url, what):
    if what == "stations":
        topo, err = fetch_topology(base_url)
        if not err:
            names = [station_text(n["name"], topo) for n in topo["nodes"]]
            if not names:
                return "[PASS] 目前場域沒有任何站點（拓譜圖沒有具名節點）。"
            return f"[PASS] 目前場域的站點（{len(names)}，括號為語意名稱；不帶參數可看說明與目前佈局）：{', '.join(names)}"
        status, text, err2 = _http("GET", base_url, "/api/stations")
        if err2:
            return err
        items = (json.loads(text).get("stations") or []) if text else []
        names = [it.get("id") if isinstance(it, dict) else str(it) for it in items]
        return f"[PASS] 目前場域的站點（{len(names)}）：{', '.join(names)}" if names else "[PASS] 目前場域沒有任何站點。"
    status, text, err = _http("GET", base_url, "/api/robots")
    if err:
        return err
    try:
        names = [str(r) for r in (json.loads(text).get("robots") or [])]
    except (ValueError, AttributeError):
        return f"[ERROR] /api/robots 回應不是預期的 JSON: {text[:200]}"
    return f"[PASS] 目前場域的機器人（{len(names)}）：{', '.join(names)}" if names else "[PASS] 目前場域沒有任何機器人（robots.yaml 未載入或為空）。"

def main(argv):
    query, mode, base_url, err = parse_args(argv)
    if err:
        return err
    if mode == "map":
        return map_mode(base_url, query)
    return list_mode(base_url, mode)


if __name__ == "__main__":
    try:
        print(main(sys.argv[1:]))
    except Exception as e:
        print(f"[ERROR] semantic_map 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
