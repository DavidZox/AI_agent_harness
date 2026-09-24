"""docker / docker exec 共用層：統一逾時機制與錯誤回報格式。

所有容器相關的技能腳本（docker_est / docker_open / docker_runcmd / ROS2_*）
都透過這裡執行 docker CLI，確保：

1. 每一次呼叫都有逾時上限，不會讓 Agent 無限期卡住。
2. 在容器內執行指令時，優先用容器內的 coreutils `timeout` 包住指令：逾時時
   會對容器內整個程序群組送出 TERM（KILL_AFTER_SECONDS 秒後補 KILL），真正
   終止容器內的程序，而不是只殺掉宿主機這端的 docker exec 客戶端、讓程序變成
   孤兒繼續跑。宿主機端的 subprocess 另設 timeout + HOST_GRACE_SECONDS 作為
   Docker daemon 本身無回應時的後盾。若容器內沒有 `timeout` 指令，自動退回
   只用宿主機端逾時再執行一次。
   注意：`bash -ic`（互動式 shell）會忽略 TERM，但它的子程序會被終止；因此
   組合多個指令時請用 `&&` 串接，被終止的子程序回傳非零就不會繼續執行後面
   的指令。
3. 失敗一律回傳以 `[ERROR]` 開頭的訊息（Agent_Runner._content_for_context 靠
   這個前綴判定成功／失敗），並把常見的 docker / ros2 錯誤翻成可直接採取行動
   的說明，後面附上清理過雜訊的 stderr 與部分 stdout 供診斷。

4. 目標容器（比照當前工作目錄）：harness 以環境變數 TARGET_CONTAINER 傳入目前的目標容器，
   腳本省略 <container_name> 時用它（resolve_container）；成功操作某容器後在輸出末行印
   [TARGET_CONTAINER] <名稱>（with_target_marker），harness 據此同步狀態，見檔尾。

回傳慣例：(ok: bool, stdout: str, error_message: str)
  ok=True  -> stdout 為指令輸出，error_message 為空字串
  ok=False -> error_message 以 [ERROR] 開頭；stdout 可能含逾時／失敗前的部分輸出

腳本由 Agent 以 `python3 scripts/<name>_cmd.py` 執行，sys.path[0] 即為本目錄，
因此各腳本可直接 `import _docker_common`。
"""
import os
import subprocess

# 沒有 tty 時 bash -ic 一定會印出的雜訊，對診斷沒有幫助，一律過濾
_STDERR_NOISE = (
    "cannot set terminal process group",
    "no job control in this shell",
    "tcsetattr: Inappropriate ioctl for device",
)

HOST_GRACE_SECONDS = 10   # 宿主機端 subprocess 逾時 = 容器內逾時 + 此緩衝
KILL_AFTER_SECONDS = 2    # 容器內 timeout 送出 TERM 後多久補 KILL

# ROS2 環境載入 fallback：有些容器的 .bashrc 不會 source ROS 的 setup.bash，
# 導致 `bash -ic` 找不到 ros2；若 ROS_DISTRO 有設且 setup.bash 存在就補 source。
# 三段式：(1) shell 沒有 ros2 就 source /opt/ros/<distro>；(2) 靜默 source 第一個找到的 colcon workspace overlay
# （自訂訊息型別如 fih_rmf_msgs 才 echo 得出來；輸出丟掉，避免 overlay 的 hook 印 banner 污染工具結果）；
# (3) shell 沒有 ROS_DOMAIN_ID 時，從容器內「正在跑的 ROS 節點」的 /proc/<pid>/environ 複製 ROS_DOMAIN_ID／RMW／
# CycloneDDS／discovery 相關變數——診斷工具要看到的是節點所在的那個 domain，不是 shell 預設的 0。
# 實測 fih_rmf_system 容器：節點以 ROS_DOMAIN_ID=98 啟動，但 bash -ic 既沒有 ros2 也沒有 domain，
# 只 source /opt/ros 會讓 ros2 topic list 空白、echo 回 "does not appear to be published yet"。
ROS2_ENV_FALLBACK = (
    'command -v ros2 >/dev/null 2>&1 || '
    '{ [ -n "$ROS_DISTRO" ] && [ -f "/opt/ros/$ROS_DISTRO/setup.bash" ] '
    '&& source "/opt/ros/$ROS_DISTRO/setup.bash"; }; '
    'for _ws in /workspaces/*/install/setup.bash "$HOME"/*_ws/install/setup.bash /root/*_ws/install/setup.bash /opt/*_ws/install/setup.bash; do '
    '[ -f "$_ws" ] && { source "$_ws" >/dev/null 2>&1; break; }; done; '
    'if [ -z "$ROS_DOMAIN_ID" ]; then for _e in /proc/[0-9]*/environ; do '
    'if tr "\\0" "\\n" < "$_e" 2>/dev/null | grep -q "^ROS_DOMAIN_ID="; then '
    'while IFS= read -r _kv; do export "$_kv"; done < <(tr "\\0" "\\n" < "$_e" 2>/dev/null | grep -E "^(ROS_DOMAIN_ID|RMW_IMPLEMENTATION|CYCLONEDDS_URI|ROS_AUTOMATIC_DISCOVERY_RANGE|ROS_LOCALHOST_ONLY|ROS_STATIC_PEERS)="); '
    'break; fi; done; fi; '
)


def clean_stderr(text):
    lines = [ln for ln in (text or "").splitlines() if not any(n in ln for n in _STDERR_NOISE)]
    return "\n".join(lines).strip()


def tail(text, max_lines=20, max_chars=2000):
    """只保留輸出結尾，避免把大量原始輸出塞回 Agent。"""
    text = (text or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        text = "...(前面省略)...\n" + "\n".join(lines[-max_lines:])
    if len(text) > max_chars:
        text = "...(前面省略)..." + text[-max_chars:]
    return text


def explain_error(stderr, container=None):
    """把常見 docker / ros2 的 stderr 翻成可行動的說明；認不出來就回 None。"""
    s = (stderr or "").lower()
    if "cannot connect to the docker daemon" in s or "is the docker daemon running" in s:
        return "無法連線到 Docker daemon（服務未啟動，或目前使用者沒有 docker 權限）"
    if "permission denied" in s and "docker.sock" in s:
        return "沒有存取 docker socket 的權限（需 root 或加入 docker 群組）"
    if "no such container" in s:
        return f"找不到容器 '{container}'（名稱錯誤或尚未建立；請先用 docker_containers 查看現有容器的完整名稱）"
    if "is not running" in s:
        return f"容器 '{container}' 存在但未在運行中，請先啟動它"
    if "invalid container name" in s:
        return "映像檔名稱含有容器名稱不允許的字元（如 : 或 /），因為本工具以映像檔名稱作為容器名稱，請改用不含 tag／registry 前綴的映像檔"
    if "unable to find image" in s or "pull access denied" in s or "repository does not exist" in s:
        return "本機沒有這個映像檔，且無法從 registry 拉取（名稱／tag 錯誤、未登入或沒有網路；請先用 docker_images 確認本機有哪些映像檔）"
    if "is already in use by container" in s:
        return "同名容器已存在；請直接用 docker_open 進入，或先移除舊容器"
    if "ros2: command not found" in s:
        return "容器內找不到 ros2 指令（ROS2 未安裝，或 shell 環境未載入 /opt/ros/<distro>/setup.bash）"
    if "unable to find node" in s:
        return "找不到指定的 ROS2 節點（請先用 ROS2_node_list 確認名稱，需含命名空間前綴，例如 /nav_node）"
    if "could not determine the type for the passed topic" in s:
        return "找不到指定的 topic 或目前沒有任何 publisher（請先用 ROS2_topic_list 確認名稱）"
    return None


def _fail(what, reason, rc=None, stdout=""):
    msg = f"[ERROR] {what} 失敗" + (f"（exit code {rc}）" if rc is not None else "") + f": {reason}"
    out = tail(stdout)
    if out:
        msg += f"\n--- 部分標準輸出 ---\n{out}"
    return msg


def _partial(e):
    """從 TimeoutExpired 取出已收到的部分 stdout（text 模式下為 str）。"""
    data = e.stdout or ""
    if isinstance(data, bytes):
        data = data.decode(errors="replace")
    return tail(data)


def run_docker(cmd, timeout, what="docker 指令"):
    """執行宿主機端的 docker CLI（不進入容器內 shell），例如 docker run / docker ps。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return False, "", "[ERROR] 找不到 docker 指令，請確認 Docker 已安裝且在 PATH 中。"
    except subprocess.TimeoutExpired:
        return False, "", (
            f"[ERROR] {what} 逾時（超過 {timeout} 秒沒有回應），"
            f"可能是 Docker daemon 無回應，或映像檔拉取過慢。"
        )
    except Exception as e:
        return False, "", f"[ERROR] {what} 執行異常: {e}"

    if r.returncode != 0:
        err = clean_stderr(r.stderr)
        reason = explain_error(err) or err or "沒有任何錯誤訊息"
        if explain_error(err) and err:
            reason += f"\n--- stderr ---\n{tail(err)}"
        return False, r.stdout, _fail(what, reason, r.returncode, r.stdout)
    return True, r.stdout, ""


def docker_exec(container, command, timeout, interactive=False, what=None, timeout_hint=None):
    """在容器內以 bash 執行 shell 指令，含容器內 timeout 與宿主機端後盾逾時。

    interactive=True 時用 `bash -ic`（會載入 .bashrc，ROS2 指令需要）。
    timeout_hint 會附加在逾時訊息後面，讓呼叫端補充「逾時最可能的原因」。
    """
    what = what or f"容器 '{container}' 內的指令"
    shell_flag = "-ic" if interactive else "-c"
    with_inner_timeout = [
        "docker", "exec", container,
        "timeout", "-k", str(KILL_AFTER_SECONDS), str(timeout),
        "bash", shell_flag, command,
    ]
    plain = ["docker", "exec", container, "bash", shell_flag, command]

    for cmd, inner in ((with_inner_timeout, True), (plain, False)):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + HOST_GRACE_SECONDS)
        except FileNotFoundError:
            return False, "", "[ERROR] 找不到 docker 指令，請確認 Docker 已安裝且在 PATH 中。"
        except subprocess.TimeoutExpired as e:
            msg = (
                f"[ERROR] {what} 逾時（超過 {timeout} 秒），宿主機端已強制終止 docker exec；"
                f"容器內的程序可能仍在執行，請自行確認。"
            )
            if timeout_hint:
                msg += f"\n{timeout_hint}"
            partial = _partial(e)
            if partial:
                msg += f"\n--- 逾時前的部分輸出 ---\n{partial}"
            return False, "", msg
        except Exception as e:
            return False, "", f"[ERROR] {what} 執行異常: {e}"

        err = clean_stderr(r.stderr)

        # 容器內沒有 coreutils timeout：退回只用宿主機端逾時再執行一次
        if inner and r.returncode in (126, 127) and "timeout" in err and "executable file not found" in err:
            continue

        if inner and r.returncode == 124:
            msg = f"[ERROR] {what} 逾時（超過 {timeout} 秒），容器內的程序已被終止。"
            if timeout_hint:
                msg += f"\n{timeout_hint}"
            partial = tail(r.stdout)
            if partial:
                msg += f"\n--- 逾時前的部分輸出 ---\n{partial}"
            return False, r.stdout, msg

        if inner and r.returncode == 137:
            return False, r.stdout, _fail(
                what,
                f"程序被 KILL 強制終止（逾時 {timeout} 秒後 TERM 無效而補 KILL，或程序遭系統 OOM 終止）",
                r.returncode, r.stdout,
            )

        if r.returncode != 0:
            explained = explain_error(err, container)
            reason = explained or err or "沒有任何錯誤訊息"
            if explained and err:
                reason += f"\n--- stderr ---\n{tail(err)}"
            return False, r.stdout, _fail(what, reason, r.returncode, r.stdout)

        return True, r.stdout, ""

    return False, "", f"[ERROR] {what} 執行異常: 無法以任何方式執行 docker exec"  # 理論上不會到這


def ros2_exec(container, ros2_command, timeout, timeout_hint=None):
    """在容器內執行 ros2 指令：互動式 shell + ROS2 環境 fallback。"""
    return docker_exec(
        container,
        ROS2_ENV_FALLBACK + ros2_command,
        timeout,
        interactive=True,
        what=f"容器 '{container}' 內的 `{ros2_command}`",
        timeout_hint=timeout_hint,
    )


def pass_or_empty(stdout, empty_hint):
    """成功但沒有輸出時，給 Agent 一個明確的 [PASS] 訊息。
    空字串會被 Agent_Runner 誤判成「沒有工具需要執行」，絕對不能回傳空字串。"""
    text = (stdout or "").strip()
    return text if text else f"[PASS] 指令執行成功，但沒有任何輸出（{empty_hint}）。"


def parse_timeout(value, default, max_seconds, name="timeout"):
    """解析使用者傳入的逾時秒數；回傳 (seconds, error_message_or_None)。"""
    if value is None or str(value).strip() == "":
        return default, None
    try:
        seconds = int(str(value).strip())
    except ValueError:
        return None, f"[ERROR] {name} 必須是整數秒數，收到: {value!r}"
    if not 1 <= seconds <= max_seconds:
        return None, f"[ERROR] {name} 需介於 1～{max_seconds} 秒（上限低於系統總逾時 600 秒），收到: {seconds}"
    return seconds, None


# ---------------------------------------------------------------- 目標容器（比照當前工作目錄）
# harness（Agent_Runner.run_tool）把目前的目標容器放在環境變數 TARGET_CONTAINER 傳給每支腳本，
# 容器技能省略 <container_name> 時就用它——跟工作目錄同一套邏輯（腳本以 current_cwd 執行、相對路徑自然生效）。
# 腳本成功操作某個容器後，在輸出末行印「[TARGET_CONTAINER] <名稱>」（比照 cd 的 [CWD_CHANGED]），harness 的
# _sync_state_from_tool_output 會同步；所以目標容器＝最近一次成功操作的容器，docker_open 則是刻意選定／切換用。
# 標記放末行是為了不破壞 [PASS]／[ERROR] 的開頭判定；與目前目標相同時不印，避免每次都多一行噪音。
TARGET_CONTAINER_ENV = "TARGET_CONTAINER"
TARGET_CONTAINER_MARKER = "[TARGET_CONTAINER]"
NO_TARGET_HINT = ("沒有指定容器，目前也沒有目標容器：請先用 docker_containers 查看名稱，再用 docker_open <名稱> 選定目標容器"
                  "（之後可省略容器名稱），或直接把容器名稱放在第一個參數。")
LIST_TIMEOUT_SECONDS = 15


def current_target_container():
    return (os.environ.get(TARGET_CONTAINER_ENV) or "").strip()


def list_container_names(timeout=LIST_TIMEOUT_SECONDS):
    """所有容器（含未運行）的名稱清單；docker 不可用時回 (None, error)。"""
    ok, out, err = run_docker(["docker", "ps", "-a", "--format", "{{.Names}}"], timeout, what="查詢容器清單 (docker ps -a)")
    if not ok:
        return None, err
    return [n.strip() for n in out.splitlines() if n.strip()], None


def match_container(name, names):
    """名稱比對：完全相同 > 唯一的子字串符合；對不到或多個符合回 None（docker exec 只認完整名稱）。"""
    if name in names:
        return name
    matches = [n for n in names if name in n]
    return matches[0] if len(matches) == 1 else None


def resolve_container(name, usage=None):
    """決定這次要操作的容器，回傳 (container, error)：name 非空就用它（docker exec 只認完整名稱，不做猜測）；
    空 → 目前的目標容器；兩者皆空 → [ERROR] 提示先用 docker_open 選定。"""
    name = (name or "").strip() or current_target_container()
    if name:
        return name, None
    return None, f"[ERROR] {NO_TARGET_HINT}" + (f"\n{usage}" if usage else "")


def with_target_marker(output, container):
    """成功輸出附上目標容器標記（末行），讓 harness 把它設為目前的目標容器；與目前目標相同時不附。"""
    container = (container or "").strip()
    if not container or container == current_target_container():
        return output
    return f"{output.rstrip()}\n{TARGET_CONTAINER_MARKER} {container}"
