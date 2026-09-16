import sys
import subprocess
import os

def run_in_container(target, command):
    # 確保我們鎖定的是正確的容器名稱
    container_name = f"{target}"
    
    # 執行指令並同步路徑狀態
    # 1. 進入目標目錄
    # 2. 執行使用者命令
    # 3. 取得執行後的當前目錄 [CONTAINER_CWD]
    full_cmd = f"{command} && pwd"
    
    cmd = ["docker", "exec", container_name, "bash", "-c", full_cmd]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip()
        else:
            return f"[ERROR] 執行失敗: {result.stderr.strip()}"
    except Exception as e:
        return f"[ERROR] 異常: {str(e)}"

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: docker_run.py <container_name> <command>")
        sys.exit(1)
        
    target = sys.argv[1]
    command = " ".join(sys.argv[2:])
    print(run_in_container(target, command))