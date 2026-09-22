import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import run_docker, docker_exec

LIST_TIMEOUT_SECONDS = 15   # docker ps -a
EXEC_TIMEOUT_SECONDS = 15   # docker exec 驗證連線


def find_container(display_name):
    """在所有容器（含未運行）中找出真實名稱。回傳 (real_name, error_message)。

    比對順序：完全相同 > 唯一的子字串符合。若有多個容器都包含該字串，
    回傳錯誤並列出候選，不猜第一個——同一部主機常有 prefix 相同的多個容器。
    """
    ok, out, err = run_docker(
        ["docker", "ps", "-a", "--format", "{{.Names}}"],
        LIST_TIMEOUT_SECONDS,
        what="查詢容器清單 (docker ps -a)",
    )
    if not ok:
        return None, err

    names = [n.strip() for n in out.splitlines() if n.strip()]
    if display_name in names:
        return display_name, None

    matches = [n for n in names if display_name in n]
    if not matches:
        existing = "、".join(names[:10]) if names else "（目前沒有任何容器）"
        return None, (
            f"[ERROR] 在 Docker 中找不到名稱包含 '{display_name}' 的容器。"
            f"現有容器：{existing}"
        )
    if len(matches) > 1:
        listed = "\n".join(f"- {m}" for m in matches)
        return None, (
            f"[ERROR] 有 {len(matches)} 個容器名稱都包含 '{display_name}'，無法判斷要進入哪一個，"
            f"請提供更完整的名稱：\n{listed}"
        )
    return matches[0], None


def enter_container(target_name):
    target_name = (target_name or "").strip()
    if not target_name:
        return "[ERROR] 請提供容器名稱（可為部分名稱）。用法: scripts/docker_open_cmd.py <container_name>"

    real_name, err = find_container(target_name)
    if err:
        return err

    ok, out, err = docker_exec(
        real_name,
        "echo '連線成功' && pwd && whoami",
        EXEC_TIMEOUT_SECONDS,
        what=f"連線至容器 '{real_name}'",
    )
    if not ok:
        return err
    return f"[PASS] 已成功連線至 '{real_name}'！\n{out.strip()}"


if __name__ == "__main__":
    try:
        target = sys.argv[1] if len(sys.argv) > 1 else ""
        print(enter_container(target))
    except Exception as e:
        print(f"[ERROR] docker_open 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
