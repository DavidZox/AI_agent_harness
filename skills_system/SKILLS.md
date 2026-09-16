# Agent Skills Index

## 可用 CLI 工具清單

| 技能類型 | 技能名稱 | 功能描述 | 核心指令/腳本路徑 | 參數範例 |
| :--- | :--- | :--- | :--- | :--- |
| **系統內建** | `list_dir` | 查看指定環境語義單元（目錄）下的檔案清單 | `scripts/ls_cmd.py` | `-la /opt/ros` |
| **系統內建** | `search_text`| 在檔案「內部文字」中過濾特定關鍵字。 | `scripts/grep_cmd.py` | `"ERROR" .` |
| **系統內建** | `find_file` | 只搜尋「檔案名稱」本身。 | `scripts/find_file_cmd.py`| `"Modelfile" .` |
| **系統內建** | `change_dir` | 切換當前工作目錄（注意：後端需維護 CWD 狀態） | `scripts/cd_cmd.py` | `/home/robot/workspace` |
| **系統內建** | `view_file`  | 查看診斷報告或腳本內容 | `scripts/cat_cmd.py` | `config.yaml` |
| **容器化環境** | `docker_est` | 創造指定映像檔的container | `scripts/docker_est_cmd.py` | `image_name` |
| **容器化環境** | `docker_open` | 進入指定名稱的container中 | `scripts/docker_open_cmd.py` | `container_name` |
| **容器化環境** | `docker_runcmd` | 若要在容器內使用指令時使用 | `scripts/docker_runcmd_cmd.py` | `container_name cmd_name` |
| **多模態資訊** | `stt_engine` | 使用者需要進行語音輸入時使用 | `scripts/stt_engine_cmd.py` | `` |
| **ROS2套件** | `ROS2_topic_list` | 在容器內使用查詢topic列表 | `scripts/ROS2_topic_list_cmd.py` | `container_name` |
| **ROS2套件** | `ROS2_topic_echo` | 指定topic名稱進行echo命令，獲得topic詳細資訊 | `scripts/ROS2_topic_echo_cmd.py` | `container_name topic_name` |
| **ROS2套件** | `ROS2_node_list` | 在容器內使用查詢node列表 | `scripts/ROS2_node_list_cmd.py` | `container_name` |
| **ROS2套件** | `ROS2_node_info` | 指定node名稱進行info命令，獲得node詳細資訊 | `scripts/ROS2_node_info_cmd.py` | `container_name node_name` |
| **調度系統** | `workitem_est` | 發送工作項目給調度系統時使用 | `scripts/workitem_est_cmd.py` | `robot_name(如："tb1") workstations(如："a0,a8,a6,a4") priority(如：10) state(如："duty") is_authored(如："yes")` |
| **記憶修改** | `modify_memory`  | 寫入經驗記憶 | `scripts/modify_memory_cmd.py` | `Reflect` |
| **專用運維** | `robot_ping` | 檢查 V4.9 機器人核心與語義層狀態 | `scripts/robot_ping_cmd.py` | (無參數) |
| **專用運維** | `eval_speed` | 評估指定速度是否超出物理邊界限制 | `scripts/eval_speed_cmd.py` | `0.5` |
| **專用運維** | `monitor_motor`| 監測動力組件溫度，評估運作狀態 | `scripts/monitor_motor_cmd.py` | `75` |
| **專用運維** | `check_batch`| 接收逗號分隔的數值字串，檢查是否有數值超過限制 100 | `scripts/check_batch_cmd.py` | `98,102,45` |