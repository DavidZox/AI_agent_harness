import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _docker_common import (
    docker_exec,
    pass_or_empty,
    resolve_container,
    with_target_marker,
)

TIMEOUT_SECONDS = 15
USAGE = (
    "用法: scripts/ROS2_domain_id_cmd.py [container_name] [new_domain_id]\n"
    "  - 若未填寫 new_domain_id，則為查詢 ROS_DOMAIN_ID。\n"
    "  - 若填寫 new_domain_id（0-232 的整數），則將 ROS_DOMAIN_ID 修改/設定為該值。\n"
    "  - container_name 可省略＝目前的目標容器。"
)

# 載入 ROS 2 工作區環境變數的前置指令
SOURCE_ENV_CMD = "[ -f install/setup.bash ] && source install/setup.bash"


def parse_cli(argv):
    """
    解析命令列參數，回傳 (container, new_domain_id, error_message)。
    支援情境：
      1. []                      -> (target_container, None, None)
      2. ["42"]                  -> (target_container, 42, None)
      3. ["my_container"]        -> ("my_container", None, None)
      4. ["my_container", "42"]  -> ("my_container", 42, None)
    """
    args = [a.strip() for a in argv if a.strip()]
    container_arg = ""
    new_id = None

    if len(args) == 0:
        container_arg = ""
    elif len(args) == 1:
        if args[0].isdigit():
            container_arg = ""
            new_id = int(args[0])
        else:
            container_arg = args[0]
    elif len(args) == 2:
        container_arg = args[0]
        if not args[1].isdigit():
            return None, None, f"[ERROR] ROS_DOMAIN_ID 必須為整數 (0-232)，收到: {args[1]!r}\n{USAGE}"
        new_id = int(args[1])
    else:
        return None, None, f"[ERROR] 參數過多。\n{USAGE}"

    if new_id is not None and not (0 <= new_id <= 232):
        return None, None, f"[ERROR] ROS_DOMAIN_ID 超出有效範圍 (0-232)，收到: {new_id}\n{USAGE}"

    container, err = resolve_container(container_arg, USAGE)
    return container, new_id, err


def get_domain_id(container):
    """先 source install/setup.bash 再查詢容器內的 ROS_DOMAIN_ID"""
    cmd = f'bash -c "{SOURCE_ENV_CMD} && echo $ROS_DOMAIN_ID"'
    ok, out, err = docker_exec(container, cmd, TIMEOUT_SECONDS, what="查詢 ROS_DOMAIN_ID")
    if not ok:
        return err
    
    val = out.strip()
    if not val:
        val = "0 (未設定)"
    
    body = f"[PASS] 容器 '{container}' 當前的 ROS_DOMAIN_ID 為: {val}"
    return with_target_marker(body, container)


def set_domain_id(container, new_id):
    """修改容器內的 ROS_DOMAIN_ID 環境變數（更新 ~/.bashrc 並在當前 session 設定）"""
    # 1. 將 export 寫入 ~/.bashrc（避免未來開啟新 shell 時失效）
    bashrc_cmd = (
        f"bash -c \"sed -i '/export ROS_DOMAIN_ID=/d' ~/.bashrc && "
        f"echo 'export ROS_DOMAIN_ID={new_id}' >> ~/.bashrc\""
    )
    ok, out, err = docker_exec(container, bashrc_cmd, TIMEOUT_SECONDS, what="更新 ~/.bashrc 中的 ROS_DOMAIN_ID")
    if not ok:
        return err

    # 2. 載入 setup.bash 並驗證變更
    check_cmd = f"bash -c \"{SOURCE_ENV_CMD} && export ROS_DOMAIN_ID={new_id} && echo $ROS_DOMAIN_ID\""
    ok_chk, out_chk, err_chk = docker_exec(container, check_cmd, TIMEOUT_SECONDS, what="驗證 ROS_DOMAIN_ID")
    if not ok_chk:
        return err_chk

    body = f"[PASS] 成功將容器 '{container}' 的 ROS_DOMAIN_ID 修改為: {out_chk.strip()}"
    return with_target_marker(body, container)


def run_domain_id(container, new_id=None):
    if not container:
        return f"[ERROR] 未能確定目標容器。\n{USAGE}"

    if new_id is None:
        return get_domain_id(container)
    else:
        return set_domain_id(container, new_id)


if __name__ == "__main__":
    try:
        container, new_id, err = parse_cli(sys.argv[1:])
        if err:
            print(err)
            sys.exit(0)
        print(run_domain_id(container, new_id))
    except Exception as e:
        print(f"[ERROR] ROS2_domain_id 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)