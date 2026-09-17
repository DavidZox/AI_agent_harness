import sys
import subprocess

def execute(image_name):
    """
    以背景模式（detached）從指定的映像檔（image）建立並啟動一個新的
    Docker 容器，是「幫 Agent 準備一個可以進去操作的容器」的第一步
    （est = establish）。

    作法：容器名稱直接沿用 image_name（同名），並用
    `tail -f /dev/null` 當容器的前景程序——因為 Docker 容器只要
    主程序結束就會跟著退出，這個指令本身不會結束，純粹是讓容器
    保持存活，方便之後用 docker_open_cmd／docker_runcmd_cmd 之類
    的指令 exec 進去操作。

    參數：
        image_name：要啟動的 Docker 映像檔名稱／tag，同時也會被
            當成新容器的名稱。

    回傳：
        - image_name 為空：回傳 [ERROR]，要求提供有效映像檔名稱。
        - docker run 成功（returncode == 0）：回傳 [PASS] 訊息，
          告知容器已在背景啟動。
        - docker run 失敗（returncode != 0，例如映像檔不存在、
          或同名容器已存在造成 name 衝突）：回傳 [ERROR] 並附上
          docker 的 stderr 內容。
        - 找不到 docker 指令本身（環境沒裝 Docker）：回傳專屬的
          [ERROR] 提示訊息。
        - 其他未預期例外：回傳通用的 [ERROR] 訊息，附上例外內容，
          不會讓例外往外拋出。
    """
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