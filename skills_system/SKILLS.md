# Agent Skills Index

> 輕量索引：`action.command` 填技能名稱取得該技能的規格文件（這一輪不執行），下一輪改填規格文件標明的腳本路徑（如 `scripts/cd_cmd.py`）、參數放 `args` 才會真正執行；文件裡 `EXECUTE: 路徑 參數` 的範例即 command 與 args。
>
> **共同回傳慣例**：成功以 `[PASS]` 開頭；失敗（含逾時）以 `[ERROR]` 開頭並附原因與建議。各技能有自己的逾時上限（見規格），系統另有 600 秒總逾時。收到 `[ERROR]` 請依訊息修正參數或回報使用者，不要原樣重試。
>
> **目標容器**：容器技能的 `<container_name>` 可省略＝Current Agent State 的 CURRENT_TARGET_CONTAINER（`docker_open` 選定／切換；成功操作過的容器會自動成為目標）。已有目標時直接用，不要再問使用者要看哪個容器。

## 系統內建
- [list_dir](tools/list_dir.md) — 查看指定環境語義單元（目錄）下的檔案清單
- [search_text](tools/search_text.md) — 在檔案「內部文字」中過濾特定關鍵字
- [find_file](tools/find_file.md) — 只搜尋「檔案名稱」本身
- [change_dir](tools/change_dir.md) — 切換當前工作目錄（注意：後端需維護 CWD 狀態）
- [view_file](tools/view_file.md) — 查看診斷報告或腳本內容（技能規格文件會由 EXECUTE 自動載入，不用這個）

## 容器化環境
- [docker_containers](tools/docker_containers.md) — 列出目前所有容器（名稱、狀態、映像檔），可只看運行中或以關鍵字過濾；要進入容器前先用這個確認完整名稱
- [docker_images](tools/docker_images.md) — 列出本機所有映像檔（名稱、tag、大小），可用關鍵字過濾；建立容器前先用這個確認映像檔存在
- [docker_est](tools/docker_est.md) — 創造指定映像檔的 container
- [docker_open](tools/docker_open.md) — 進入指定名稱的 container 並設為目標容器（之後容器技能可省略名稱；要換容器再用一次）
- [docker_runcmd](tools/docker_runcmd.md) — 若要在容器內使用指令時使用

## 多模態資訊
- [stt_engine](tools/stt_engine.md) — 使用者需要進行語音輸入時使用
- [image_inspect](tools/image_inspect.md) — 對指定路徑的影像檔（截圖、相機快照）依提示詞做視覺模型分析，回傳文字描述

## ROS2套件
- [ROS2_topic_list](tools/ROS2_topic_list.md) — 在容器內使用查詢 topic 列表
- [ROS2_topic_echo](tools/ROS2_topic_echo.md) — 讀取指定 topic 的一筆訊息，或 --duration 擷取一段時間整理欄位變化與頻率
- [ROS2_node_list](tools/ROS2_node_list.md) — 在容器內使用查詢 node 列表
- [ROS2_node_info](tools/ROS2_node_info.md) — 指定 node 名稱進行 info 命令，獲得 node 詳細資訊

## 調度系統
- [workpackage_send](tools/workpackage_send.md) — 對任務協調器 orchestrtor 發送多站點 work package（站點可用語意名稱；可設 N 輪或無限循環）
- [workpackage_status](tools/workpackage_status.md) — 查 work package 執行狀態：一幀快照，或 --watch 觀察一段時間並整理變化
- [workpackage_cancel](tools/workpackage_cancel.md) — 取消（刪除）一個 work package，orchestrtor 停止派下一站
- [overpending_cancel](tools/overpending_cancel.md) — 刪除卡在 distribute OverPending 逾時區的單站任務；不在逾時區時可等它進來再刪
- [semantic_map](tools/semantic_map.md) — 語義地圖：站點代號／語意名稱／說明／路段，對應目前機器人與工作包位置；也可只列站點或機器人

## 記憶修改
- [modify_memory](tools/modify_memory.md) — 寫入經驗記憶：預設全域常駐；加 `--skill <技能名稱>` 綁定該技能，只在載入其規格時出現
