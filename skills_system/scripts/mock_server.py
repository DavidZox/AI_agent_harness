from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

@app.post("/plan_sequence")
async def receive_task(request: Request):
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