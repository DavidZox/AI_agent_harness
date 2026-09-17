import requests
import json
import sys

def dispatch_task_to_robot(api_url, tasks):
    """
    將工作項目以 HTTP POST 送至調度系統（預設對應 FastAPI 的 /plan_sequence
    端點），用於把 Agent 組好的任務 payload 實際派送出去。

    參數 api_url 為目標端點完整網址（呼叫端固定組成
    "http://localhost:8000/plan_sequence"）；tasks 為 list[dict]，每個
    dict 需含 robot_name、workstations（list）、priority（int）、state、
    is_authored 等欄位，會直接以 requests.post(api_url, json=tasks) 序列化
    成 JSON body 送出（開發測試可先啟動 mock_server.py 模擬此端點）。

    依 HTTP 回應分三種結果：status_code 200 視為成功，回傳附上伺服器回傳
    JSON 內容的成功訊息；非 200（如 4xx/5xx）視為業務邏輯失敗，回傳附狀態碼
    與原始回應文字的失敗訊息；requests 拋出例外（如連線被拒、逾時、DNS
    失敗）則整個攔截下來，回傳連線異常訊息，不會讓例外往外傳播、中斷呼叫端
    的 __main__ 流程。

    回傳值：一律是給人看的結果字串（成功/失敗/例外三選一），不回傳
    boolean 或例外。
    """
    try:
        response = requests.post(api_url, json=tasks)
        if response.status_code == 200:
            return f"✅ 任務發送成功: {json.dumps(response.json())}"
        else:
            return f"❌ 發送失敗 (Status {response.status_code}): {response.text}"
    except Exception as e:
        return f"🚨 連線異常: {str(e)}"

if __name__ == "__main__":
    try:
        # 檢查參數數量 (程式名稱 + 5 個參數)
        if len(sys.argv) < 6:
            print("用法: python3 scripts/workitem_est_cmd.py <robot_name> <workstations> <priority> <state> <is_authored>")
            sys.exit(1)

        # 依序讀取參數
        robot_name = sys.argv[1]
        workstations_raw = sys.argv[2]
        # priority 必須是整數，非數字字串（如 "high"）會在這裡被攔截成 [ERROR]，
        # 而不是讓 ValueError 以未處理例外的形式往外拋出整段 traceback。
        priority = int(sys.argv[3])
        state = sys.argv[4]
        is_authored = sys.argv[5]

        # 將逗號分隔的站點字串轉為列表
        stations_list = [s.strip() for s in workstations_raw.split(",")]

        # 組裝 Payload
        payload = [{
            "robot_name": robot_name,
            "workstations": stations_list,
            "priority": priority,
            "state": state,
            "is_authored": is_authored
        }]

        URL = "http://localhost:8000/plan_sequence"

        # 執行發送並輸出結果供 Agent 讀取
        print(dispatch_task_to_robot(URL, payload))

    except ValueError:
        print(f"[ERROR] priority 必須是整數，收到的是: {sys.argv[3]!r}")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] 執行異常: {e}")
        sys.exit(1)