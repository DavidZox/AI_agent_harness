# Agent Skills Index

> 這是輕量索引。每項技能只列出名稱與一行描述；直接 `EXECUTE: [技能名稱]` 即可，系統會自動把對應的 `tools/<name>.md` 規格文件內容當作系統回傳注入上下文（這一輪不會執行）。讀完規格後，下一輪請改用規格文件裡標明的實際腳本路徑（例如 `scripts/cd_cmd.py`）再次 `EXECUTE:`，才會真正執行。

## 系統內建
- [list_dir](tools/list_dir.md) — 查看指定環境語義單元（目錄）下的檔案清單
- [search_text](tools/search_text.md) — 在檔案「內部文字」中過濾特定關鍵字
- [find_file](tools/find_file.md) — 只搜尋「檔案名稱」本身
- [change_dir](tools/change_dir.md) — 切換當前工作目錄（注意：後端需維護 CWD 狀態）
- [view_file](tools/view_file.md) — 查看診斷報告或腳本內容（技能規格文件會由 EXECUTE 自動載入，不用這個）

## 容器化環境
- [docker_est](tools/docker_est.md) — 創造指定映像檔的 container
- [docker_open](tools/docker_open.md) — 進入指定名稱的 container 中
- [docker_runcmd](tools/docker_runcmd.md) — 若要在容器內使用指令時使用

## 多模態資訊
- [stt_engine](tools/stt_engine.md) — 使用者需要進行語音輸入時使用

## ROS2套件
- [ROS2_topic_list](tools/ROS2_topic_list.md) — 在容器內使用查詢 topic 列表
- [ROS2_topic_echo](tools/ROS2_topic_echo.md) — 指定 topic 名稱進行 echo 命令，獲得 topic 詳細資訊
- [ROS2_node_list](tools/ROS2_node_list.md) — 在容器內使用查詢 node 列表
- [ROS2_node_info](tools/ROS2_node_info.md) — 指定 node 名稱進行 info 命令，獲得 node 詳細資訊

## 調度系統
- [workitem_est](tools/workitem_est.md) — 發送工作項目給調度系統時使用

## 記憶修改
- [modify_memory](tools/modify_memory.md) — 寫入經驗記憶

