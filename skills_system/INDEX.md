# Agent Skills Index

本索引為 Agent 每次對話都會載入的「輕量清單」。**規格書欄位所連結的 `tools/*.md` 檔案不會自動載入** —
Agent 需先針對想使用的技能輸出 `NEED_TOOL: <技能名稱>`，取得完整規格書後，才可執行對應的 `EXECUTE:` 指令。

## 可用技能清單

| 技能類型 | 技能名稱 | 功能描述 | 規格書 |
| :--- | :--- | :--- | :--- |
| **系統內建** | `list_dir` | 查看指定環境語義單元（目錄）下的檔案清單 | [tools/list_dir.md](tools/list_dir.md) |
| **系統內建** | `search_text` | 在檔案「內部文字」中過濾特定關鍵字 | [tools/search_text.md](tools/search_text.md) |
| **系統內建** | `find_file` | 只搜尋「檔案名稱」本身 | [tools/find_file.md](tools/find_file.md) |
| **系統內建** | `change_dir` | 切換當前工作目錄（注意：後端需維護 CWD 狀態） | [tools/change_dir.md](tools/change_dir.md) |
| **系統內建** | `view_file` | 查看診斷報告或腳本內容 | [tools/view_file.md](tools/view_file.md) |
| **容器化環境** | `docker_est` | 創造指定映像檔的 container | [tools/docker_est.md](tools/docker_est.md) |
| **容器化環境** | `docker_open` | 進入指定名稱的 container 中 | [tools/docker_open.md](tools/docker_open.md) |
| **容器化環境** | `docker_runcmd` | 若要在容器內使用指令時使用 | [tools/docker_runcmd.md](tools/docker_runcmd.md) |
| **多模態資訊** | `stt_engine` | 使用者需要進行語音輸入時使用 | [tools/stt_engine.md](tools/stt_engine.md) |
| **ROS2套件** | `ROS2_topic_list` | 在容器內查詢 topic 列表 | [tools/ROS2_topic_list.md](tools/ROS2_topic_list.md) |
| **ROS2套件** | `ROS2_topic_echo` | 指定 topic 名稱進行 echo，取得詳細資訊 | [tools/ROS2_topic_echo.md](tools/ROS2_topic_echo.md) |
| **ROS2套件** | `ROS2_node_list` | 在容器內查詢 node 列表 | [tools/ROS2_node_list.md](tools/ROS2_node_list.md) |
| **ROS2套件** | `ROS2_node_info` | 指定 node 名稱進行 info，取得詳細資訊 | [tools/ROS2_node_info.md](tools/ROS2_node_info.md) |
| **調度系統** | `workitem_est` | 發送工作項目給調度系統 | [tools/workitem_est.md](tools/workitem_est.md) |
| **記憶修改** | `modify_memory` | 寫入經驗記憶（語意／情節／程序三種長期記憶之一） | [tools/modify_memory.md](tools/modify_memory.md) |
| **自我進化** | `manage_skill` | 建立新技能（腳本 + 規格書 + 索引三者同步生成） | [tools/manage_skill.md](tools/manage_skill.md) |
| **專用運維** | `robot_ping` | 檢查 V4.9 機器人核心與語義層狀態 | [tools/robot_ping.md](tools/robot_ping.md) |
| **專用運維** | `eval_speed` | 評估指定速度是否超出物理邊界限制 | [tools/eval_speed.md](tools/eval_speed.md) |
| **專用運維** | `monitor_motor` | 監測動力組件溫度，評估運作狀態 | [tools/monitor_motor.md](tools/monitor_motor.md) |
| **專用運維** | `check_batch` | 接收逗號分隔的數值字串，檢查是否有數值超過限制 100 | [tools/check_batch.md](tools/check_batch.md) |
