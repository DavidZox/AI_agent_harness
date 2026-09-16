import requests
import json
import sys

def dispatch_task_to_robot(api_url, tasks):
    """透過 HTTP POST 發送任務至 FastAPI 服務"""
    try:
        response = requests.post(api_url, json=tasks)
        if response.status_code == 200:
            return f"✅ 任務發送成功: {json.dumps(response.json())}"
        else:
            return f"❌ 發送失敗 (Status {response.status_code}): {response.text}"
    except Exception as e:
        return f"🚨 連線異常: {str(e)}"

if __name__ == "__main__":
    # 檢查參數數量 (程式名稱 + 5 個參數)
    if len(sys.argv) < 6:
        print("用法: python3 scripts/workitem_est_cmd.py <robot_name> <workstations> <priority> <state> <is_authored>")
        sys.exit(1)

    # 依序讀取參數
    robot_name = sys.argv[1]
    workstations_raw = sys.argv[2]
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