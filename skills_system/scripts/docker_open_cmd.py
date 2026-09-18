import sys
import subprocess
import json

def get_real_container_name(display_name):
    """
    查詢所有容器，找出名稱或別名中包含該字串的真實名稱
    """
    # 執行 docker inspect，查詢所有容器的詳細資訊
    cmd = ["docker", "ps", "-a", "--format", "{{.Names}}"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        names = result.stdout.strip().split('\n')
        
        # 尋找是否有名稱完全符合或包含 display_name
        for name in names:
            if display_name in name:
                return name
        return None
    except:
        return None

def enter_container(target_name):
    # 1. 偵測真實名稱
    real_name = get_real_container_name(target_name)
    
    if not real_name:
        return f"[ERROR] 在 Docker 中找不到名稱包含 '{target_name}' 的容器。請確認容器是否已啟動。"
    
    # 2. 使用找到的真實名稱執行
    cmd = ["docker", "exec", real_name, "bash", "-c", "echo '連線成功'; pwd; whoami"]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return f"[PASS] 已成功連線至 '{real_name}'！\n{result.stdout.strip()}"
        else:
            return f"[ERROR] 連線失敗: {result.stderr.strip()}"
    except Exception as e:
        return f"[ERROR] 執行異常: {str(e)}"

if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    print(enter_container(target))