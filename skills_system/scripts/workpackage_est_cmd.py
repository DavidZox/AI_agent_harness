import json
import sys

try:
    import requests
except ImportError:
    requests = None

API_URL = "http://localhost:8000/plan_sequence"
CONNECT_TIMEOUT_SECONDS = 5    # 建立 TCP 連線的上限
READ_TIMEOUT_SECONDS = 15      # 連上後等待回應的上限
USAGE = "用法: scripts/workitem_est_cmd.py <robot_name> <workstations> <priority> <state> <is_authored>"


def dispatch_task_to_robot(api_url, tasks,
                           connect_timeout=CONNECT_TIMEOUT_SECONDS,
                           read_timeout=READ_TIMEOUT_SECONDS):
    """透過 HTTP POST 發送任務至調度服務；所有失敗都以 [ERROR] 開頭回報原因。"""
    if requests is None:
        return "[ERROR] 缺少 requests 套件，無法發送 HTTP 請求（pip install requests）。"
    try:
        response = requests.post(api_url, json=tasks, timeout=(connect_timeout, read_timeout))
    except requests.exceptions.ConnectTimeout:
        return f"[ERROR] 連線調度系統逾時：{connect_timeout} 秒內無法建立連線到 {api_url}（服務未啟動或網路不通）。"
    except requests.exceptions.ReadTimeout:
        return f"[ERROR] 調度系統回應逾時：已連上 {api_url} 但 {read_timeout} 秒內沒有回應。"
    except requests.exceptions.ConnectionError as e:
        return f"[ERROR] 無法連線到調度系統 {api_url}（服務未啟動或位址錯誤）: {e}"
    except requests.exceptions.RequestException as e:
        return f"[ERROR] HTTP 請求異常: {e}"

    if response.status_code != 200:
        return f"[ERROR] 調度系統回應 HTTP {response.status_code}，任務未被接受: {response.text[:500]}"
    try:
        body = response.json()
    except ValueError:
        body = response.text[:500]
    return f"[PASS] 任務發送成功: {json.dumps(body, ensure_ascii=False)}"


def build_payload(argv):
    """依序解析 5 個參數並組成 payload。回傳 (payload, error_message)。"""
    if len(argv) < 5:
        return None, f"[ERROR] 參數不足，需要 5 個參數，收到 {len(argv)} 個。\n{USAGE}"
    robot_name, workstations_raw, priority_raw, state, is_authored = argv[:5]

    if not robot_name.strip():
        return None, f"[ERROR] robot_name 不可為空。\n{USAGE}"
    stations_list = [s.strip() for s in workstations_raw.split(",") if s.strip()]
    if not stations_list:
        return None, f"[ERROR] workstations 至少需要一個站點（逗號分隔，例如 a0,a8,a6）。\n{USAGE}"
    try:
        priority = int(priority_raw)
    except ValueError:
        return None, f"[ERROR] priority 必須是整數，收到: {priority_raw!r}。\n{USAGE}"

    return [{
        "robot_name": robot_name,
        "workstations": stations_list,
        "priority": priority,
        "state": state,
        "is_authored": is_authored,
    }], None


if __name__ == "__main__":
    try:
        payload, err = build_payload(sys.argv[1:])
        if err:
            print(err)
            sys.exit(0)
        print(dispatch_task_to_robot(API_URL, payload))
    except Exception as e:
        print(f"[ERROR] workitem_est 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
