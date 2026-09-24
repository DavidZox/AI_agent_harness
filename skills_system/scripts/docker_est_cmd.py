import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import run_docker, with_target_marker

# docker run -d 對本機已有的映像檔幾秒就完成；若需從 registry 拉取會久一點，
# 但仍需要上限，否則沒有網路時 Agent 會卡住。
TIMEOUT_SECONDS = 120


def execute(image_name):
    image_name = (image_name or "").strip()
    if not image_name:
        return "[ERROR] 請提供一個有效的映像檔 (image) 名稱。用法: scripts/docker_est_cmd.py <image_name>"

    # 保留原設計：容器名稱 = 映像檔名稱，方便後續 docker_open / docker_runcmd 使用
    # tail -f /dev/null 讓容器保持運行不會立刻退出
    container_name = image_name
    cmd = ["docker", "run", "-d", "--name", container_name, image_name, "tail", "-f", "/dev/null"]

    ok, out, err = run_docker(cmd, TIMEOUT_SECONDS, what=f"以映像檔 '{image_name}' 建立容器")
    if not ok:
        return err
    container_id = out.strip()[:12]
    # 新建的容器順理成章成為目前的目標容器（比照 docker_open），之後容器技能可省略名稱
    return with_target_marker(f"[PASS] 容器 '{container_name}' 已在背景啟動（ID: {container_id}），並設為目前的目標容器。", container_name)


if __name__ == "__main__":
    try:
        image_arg = sys.argv[1] if len(sys.argv) > 1 else ""
        print(execute(image_arg))
    except Exception as e:
        print(f"[ERROR] docker_est 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
