import sys
import subprocess

def execute(image_name):
    if not image_name:
        return "[ERROR] 請提供一個有效的映像檔 (image) 名稱。"

    # 方案一：以背景模式啟動容器 (Detached)
    # --name: 給容器一個名字，方便後續進入
    # tail -f /dev/null: 這是讓容器保持運行且不會立刻退出的技巧
    container_name = f"{image_name}"
    cmd = ["docker", "run", "-d", "--name", container_name, image_name, "tail", "-f", "/dev/null"]
    
    try:
        # 使用 subprocess.run 執行並捕獲錯誤
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            # 成功啟動，直接回傳訊息給 Agent，這會讓 Agent 立刻接續
            return f"[PASS] 容器 '{container_name}' 已在背景啟動。"
        else:
            return f"[ERROR] Docker 啟動失敗: {result.stderr.strip()}"
            
    except FileNotFoundError:
        return "[ERROR] 找不到 docker 指令，請確認是否已安裝 Docker。"
    except Exception as e:
        return f"[ERROR] 執行異常: {str(e)}"

if __name__ == "__main__":
    image_arg = sys.argv[1] if len(sys.argv) > 1 else ""
    # 將結果印出，Agent 系統會捕獲此 print 並正確地顯示並接續下一個任務
    print(execute(image_arg))