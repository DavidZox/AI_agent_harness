from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

@app.post("/plan_sequence")
async def receive_task(request: Request):
    """
    模擬派工排程後端的單一端點，供 `workitem_est_cmd.py::dispatch_task_to_robot()`
    以 HTTP POST 呼叫測試用（本檔案獨立執行，`python mock_server.py` 在本機
    8000 port 啟動一個假的 FastAPI 服務，不需要真正的排程系統就能驗證
    workitem_est 這條技能的請求／回應流程）。

    不驗證、不處理任何實際排程邏輯，單純把收到的 JSON 內容印到終端機供人工
    確認請求內容是否正確，然後回傳固定的成功回應，模擬「已收到並接受」。

    參數 request 為 FastAPI 注入的原始請求物件；回傳一個 dict，
    FastAPI 會自動序列化為 JSON 回應：`{"status": "success", "received": <收到的資料>}`。
    """
    # 接收來自 Agent 的 JSON 內容
    data = await request.json()
    
    print("\n" + "="*30)
    print("📥 收到來自 Agent 的任務請求:")
    print(f"內容: {data}")
    print("="*30 + "\n")
    
    # 回傳成功狀態，模擬調度系統已接收
    return {"status": "success", "received": data}

if __name__ == "__main__":
    # 在 8000 port 運行
    uvicorn.run(app, host="0.0.0.0", port=8000)