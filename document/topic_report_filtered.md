# ROS2 通訊介面與摘要報告 (精簡版)

本報告已過濾原始資料，僅保留與「通訊主體（Topic / Action / Service）」以及「備註說明（Note）」相關的欄位資訊。

## Node Interface

| 項目名稱 (Topic / Action / Service / Trigger)   | 功能備註 (Note)                                                                 |
|:--------------------------------------------|:----------------------------------------------------------------------------|
| /speed_limit                                | 向 Nav2 Controller 發布速度上限，由場景或任務動態設定                                         |
| backup                                      | 呼叫 Nav2 BackUp action，執行短距倒退避障                                              |
| dock_robot                                  | 呼叫 Nav2 DockRobot action，執行自動停靠/充電流程                                        |
| drive_on_heading                            | 呼叫 Nav2 DriveOnHeading action，沿當前朝向直線前進指定距離                                 |
| follow_path                                 | 呼叫 Nav2 FollowPath action，跟隨給定路徑移動                                          |
| navigate_to_pose                            | 呼叫 Nav2 NavigateToPose action，自由導航至任意目標姿態                                   |
| spin                                        | 呼叫 Nav2 Spin action，原地旋轉至目標朝向                                               |
| undock_robot                                | 呼叫 Nav2 UndockRobot action，脫離停靠站                                            |
| /local_costmap/clear_entirely_local_costmap | 呼叫 Nav2 service 清除整個 local costmap，用於卡住後的恢復                                 |
| /free_nav_request                           | 將 WebSocket 前端傳入的自由導航座標轉為 ROS2 PoseStamped 發布                               |
| /target_station                             | 將 WebSocket 前端傳入的目標站點 ID 轉發至 TaskAgent                                      |
| /robot_pose_odom                            | 接收機器人位姿並透過 WebSocket 廣播給前端 Dashboard                                        |
| /rosout                                     | 接收系統日誌，透過 WebSocket 轉發至前端供即時監控                                              |
| <var:t>                                     | 動態訂閱 topic，名稱由執行時參數 t 決定，用於轉發任意 topic 至 WebSocket                           |
| <var:t>                                     | 動態訂閱 topic，名稱由執行時參數 t 決定，用於轉發任意 topic 至 WebSocket                           |
| /diagnostic_status                          | 彙整各感測器心跳狀態並定期發布，供系統監控判斷硬體是否正常                                               |
| /imu                                        | 監聽 IMU 資料以確認感測器心跳，異常時更新診斷狀態                                                 |
| /joint_states_in                            | 監聽關節狀態以確認馬達心跳，異常時更新診斷狀態                                                     |
| /odom                                       | 監聽里程計資料以確認底盤心跳，異常時更新診斷狀態                                                    |
| /scan                                       | 監聽前置雷達資料以確認感測器心跳，異常時更新診斷狀態                                                  |
| /scan_b                                     | 監聽後置雷達資料以確認感測器心跳，異常時更新診斷狀態                                                  |
| /current_station                            | 機器人當前最近站點資訊，包含站點 ID、類型、距離目標距離及抵達狀態                                          |
| /exit_node_result                           | 站點退出節點查詢結果，返回站點後方可安全駛離的路網節點資訊                                               |
| /free_nav_goal                              | 下達給 RouteExecutor 的自由導航目標，包含目標座標及終點朝向 yaw                                   |
| /nearest_node_result                        | 自由導航中最近路網中繼節點的查詢結果，供 UI 或上層系統顯示路由策略                                         |
| /path_station_sequence                      | 完整的導航站點序列（含虛擬路點），供 RouteExecutor 與 UI 追蹤當前導航計畫                              |
| /path_warning                               | 偏離路徑警告，當機器人距最近邊超過閾值時觸發，提供偏離距離資訊                                             |
| /planned_path                               | 規劃完成的路網節點 ID 序列，依序為機器人需走訪的節點，發給 RouteExecutor 執行                            |
| /pre_nav_spin                               | 觸發 LocalPlanner 執行 pre-nav spin，機器人面向目標後再執行自由導航                             |
| /route_mission                              | 下達給 RouteExecutor 的路網導航任務，為經角度平滑後的節點序列                                      |
| /route_mission_coords                       | 配合 /route_mission 發布各節點座標資訊，供 RouteExecutor 計算終點 spin yaw                   |
| /virtual_waypoints                          | 擴增 A* 計算出的虛擬路點序列，供 RouteExecutor 執行路網外的多跳導航                                 |
| /base_status                                | 接收 LocalPlanner 回報的 pre-spin 結果（SPIN_DONE / SPIN_FAIL）                      |
| /free_nav_request                           | 接收 RViz 2D Goal Pose 發出的自由導航目標，包含目標位置與朝向 yaw                                |
| /global_costmap/costmap                     | 接收 Nav2 global costmap，用於擴增 A* 計算虛擬路點時的障礙物代價查詢                              |
| /request_exit_node                          | 接收站點退出節點查詢請求，依站點 ID 查找後方可安全退出的路網節點                                          |
| /request_path                               | 接收上層發出的算路請求，包含起點與終點節點 ID，觸發路徑規劃                                             |
| /robot_pose_odom                            | 接收機器人在 map frame 下的姿態，用於站點偵測、自動起點推算及偏離計算                                    |
| /route_status                               | 接收 RouteExecutor 回報的路網段執行結果，決定後續的自由導航或序列清空                                  |
| <var:joint_output_topic>                    | 將關節狀態訊息的 header.stamp 校正為 ROS 時間後重新發布                                       |
| <var:laser_output_topic>                    | 將雷射掃描訊息的 header.stamp 校正為 ROS 時間後重新發布                                       |
| <var:odom_output_topic>                     | 將里程計訊息的 header.stamp 校正為 ROS 時間後重新發布                                        |
| <var:joint_input_topic>                     | 接收原始關節狀態，修正 header 後轉發                                                      |
| <var:laser_input_topic>                     | 接收原始雷射掃描，修正 header 後轉發                                                      |
| <var:odom_input_topic>                      | 接收原始里程計，修正 header 後轉發                                                       |
| initialpose                                 | 啟動時自動發布預設初始位姿給 AMCL，省去手動在 RViz 設定 2D Pose Estimate                          |
| /base_status                                | 執行 pre-nav spin 後回報結果，GlobalPlanner 收到 SPIN_DONE 才發 /free_nav_goal          |
| /lp_follow_status                           | 執行 FollowPath 完成後回報結果，RouteExecutor 依此推進下一路段或重試                             |
| /lp_state                                   | 發布 LocalPlanner 當前執行狀態，供 Dashboard 監控顯示                                     |
| /local_backup_trigger                       | 接收 TaskAgent 下達的局部倒退指令，執行短距 BackUp action 避障                                |
| /local_finetune                             | 接收 TaskAgent 下達的站點微調指令，進行精確對位                                               |
| /planned_route_path                         | 接收 RouteExecutor 傳來的路徑 poses，執行 Spin + FollowPath 完成路段導航                    |
| /pre_nav_spin                               | 接收 GlobalPlanner/RouteExecutor 下達的旋轉指令，完成後發布 /base_status SPIN_DONE         |
| /speed_limit_req                            | 接收速度限制請求，限制 FollowPath 執行時的最大線速度                                            |
| cmd_stop                                    | 接收緊急停止指令，立即中止所有 LocalPlanner 執行中的動作                                         |
| /clicked_point                              | 接收 RViz Publish Point 工具點擊的地圖座標，用於地圖標注或節點查詢                                 |
| /clicked_point                              | 接收 RViz Publish Point 工具點擊的地圖座標，用於地圖標注或節點查詢                                 |
| /cmd_stop                                   | 透過 FastAPI REST 接收停止指令後轉發至 ROS2，觸發全系統緊急停止                                   |
| /finetune_cmd                               | 透過 FastAPI REST 接收微調指令後轉發，驅動 TaskAgent 執行站點精確對位                             |
| /target_station                             | 透過 FastAPI REST 接收目標站點請求後轉發至 TaskAgent 觸發導航任務                               |
| /lp_follow_status                           | 模擬 LocalPlanner FollowPath 完成，供整合測試不需真實 LP 即可驗證 RE 邏輯                       |
| /planned_route_path                         | 接收路徑後模擬執行，延遲後發布 DONE 以觸發 RouteExecutor 推進下一段                                |
| backup                                      | 模擬 Nav2 BackUp action server，接受 goal 後直接回傳成功                                |
| compute_and_track_route                     | 模擬路網計算 action server，回傳假路徑 feedback 供測試使用                                   |
| dock_robot                                  | 模擬停靠 action server，直接回傳成功                                                   |
| drive_on_heading                            | 模擬直線行駛 action server，直接回傳成功                                                 |
| follow_path                                 | 模擬路徑跟隨 action server，直接回傳成功                                                 |
| navigate_to_pose                            | 模擬自由導航 action server，直接回傳成功                                                 |
| spin                                        | 模擬旋轉 action server，直接回傳成功                                                   |
| undock_robot                                | 模擬脫靠 action server，直接回傳成功                                                   |
| <var:odom_topic>                            | 接收里程計並廣播 odom→base_footprint TF，供 Nav2 定位使用                                 |
| /cmd_vel                                    | 依系統狀態仲裁輸出自動或手動速度指令，最終傳給底盤驅動                                                 |
| /cmd_vel                                    | 依系統狀態仲裁輸出自動或手動速度指令，最終傳給底盤驅動                                                 |
| /cmd_vel_auto                               | 接收 Nav2 Controller 自動導航速度指令，由仲裁器決定是否轉發                                      |
| /cmd_vel_auto                               | 接收 Nav2 Controller 自動導航速度指令，由仲裁器決定是否轉發                                      |
| /cmd_vel_manual                             | 接收遙控/手動操作速度指令，系統進入手動模式時優先輸出                                                 |
| /cmd_vel_manual                             | 接收遙控/手動操作速度指令，系統進入手動模式時優先輸出                                                 |
| /system_status                              | 接收系統運行模式，決定仲裁邏輯（自動/手動/緊急停止）                                                 |
| /system_status                              | 接收系統運行模式，決定仲裁邏輯（自動/手動/緊急停止）                                                 |
| /robot_pose_odom                            | map frame 下的完整 Odometry，格式與 /odom 相同，twist 來自原始 /odom                       |
| /odom                                       | 訂閱原始 odom 取得 twist（線速度 + 角速度），合併至 /robot_pose_odom                          |
| /robot_state                                | 彙整機器人全狀態（位姿、站點、路由狀態、序列）並定期廣播給前端 Dashboard                                   |
| /current_station                            | 接收當前站點資訊，納入 /robot_state 廣播                                                 |
| /path_station_sequence                      | 接收導航站點序列，納入 /robot_state 廣播供前端顯示進度                                          |
| /robot_pose_odom                            | 接收機器人位姿，納入 /robot_state 廣播                                                  |
| /route_status                               | 接收路網執行結果，納入 /robot_state 廣播                                                 |
| /planned_route_path                         | 將 ComputeAndTrackRoute feedback 的路徑 poses 序列化後發給 LocalPlanner 執行 FollowPath |
| /pre_nav_spin                               | 觸發 LocalPlanner 執行 pre-nav spin，目標朝向為虛擬路點接近方向                               |
| /route_executor_state                       | 回報 RouteExecutor 當前執行狀態，供 UI 或 Debug 工具監控導航階段                               |
| /route_status                               | 回報路網導航最終結果給 GlobalPlanner，作為任務完成或失敗的信號                                      |
| /base_status                                | 接收 LocalPlanner 回報的 pre-spin 結果，驅動虛擬路點狀態機前進                                 |
| /free_nav_goal                              | 接收 GlobalPlanner 發布的自由導航目標，包含 NavigateToPose 的目標座標與終點朝向                     |
| /lp_follow_status                           | 接收 LocalPlanner 回報的 FollowPath 執行結果，決定是否推進到下一段或重試                           |
| /robot_pose_odom                            | 接收機器人位姿，用於虛擬路點 pre-spin 的接近方向計算                                             |
| /route_mission                              | 接收 GlobalPlanner 下達的路網導航任務（節點序列），覆蓋當前任務並重新規劃段落                              |
| /route_mission_coords                       | 接收 GlobalPlanner 發布的節點座標對應表，用於計算終點 spin yaw                                 |
| /virtual_waypoints                          | 接收 GlobalPlanner 擴增 A* 計算出的多跳虛擬路點序列，逐點執行 NavigateToPose                     |
| cmd_stop                                    | 接收緊急停止指令，立即取消所有進行中的路網與自由導航任務                                                |
| compute_and_track_route                     | 向 Nav2 請求路網路徑計算並追蹤，feedback 回傳路徑 poses 後轉交 LocalPlanner 執行                  |
| navigate_to_pose                            | 向 Nav2 請求自由導航至任意目標座標，用於路網外的自由導航及虛擬路點到達                                      |
| ?                                           | 由場景注入器動態發布，topic 名稱與訊息型別依測試腳本配置                                             |
| /local_backup_trigger                       | 障礙物偵測後觸發 LocalPlanner 執行短距倒退，執行完後繼續任務                                       |
| /local_finetune                             | 抵達站點後觸發 LocalPlanner 執行精確對位微調                                               |
| /navigation_display                         | 發布 TaskAgent 任務狀態訊息，供 Dashboard UI 顯示導航進度與提示                                |
| /request_exit_node                          | 向 GlobalPlanner 查詢指定站點的後方退出節點，取得後進行離站規劃                                     |
| /request_path                               | 向 GlobalPlanner 下達算路請求，觸發路徑規劃並發布 /route_mission                             |
| /task_agent/debug_state                     | 發布 TaskAgent 內部狀態與任務步驟，供開發人員 debug 監控                                       |
| /base_status                                | 監控機器人 pre-spin 狀態，確認旋轉完成後才推進下一任務步驟                                          |
| /current_station                            | 追蹤機器人當前站點，判斷是否抵達目標站點以結束任務                                                   |
| /exit_node_result                           | 接收退出節點查詢結果後觸發離站算路                                                           |
| /finetune_cmd                               | 接收來自 Bridge 的微調指令，轉發給 LocalPlanner 執行精確對位                                   |
| /planned_path                               | 接收 GlobalPlanner 規劃完成的節點序列，確認算路成功後繼續任務流程                                    |
| /route_status                               | 接收路網執行結果，DONE 後觸發站點抵達確認，FAIL 後執行重試或中止                                       |
| /target_station                             | 接收使用者或 Bridge 下達的目標站點 ID，啟動導航任務                                             |
| /base_status                                | 監控 pre-spin 狀態供 Dashboard 顯示                                                |
| /current_station                            | 監控當前站點資訊，即時顯示於前端地圖 UI                                                       |
| /path_warning                               | 監控偏離警告，異常時於 Dashboard 顯示警示                                                  |
| /planned_path                               | 監控規劃路徑，將節點序列顯示於前端地圖                                                         |
| /robot_pose_odom                            | 監控機器人位姿，即時更新前端地圖上的機器人圖示                                                     |
| /route_status                               | 監控路網執行結果，顯示任務完成或失敗提示                                                        |

## Topic Connections

| 項目名稱 (Topic / Action / Service / Trigger)   | 功能備註 (Note)                                                                 |
|:--------------------------------------------|:----------------------------------------------------------------------------|
| /base_status                                | 接收 LocalPlanner 回報的 pre-spin 結果（SPIN_DONE / SPIN_FAIL）                      |
| /clicked_point                              |                                                                             |
| /cmd_stop                                   |                                                                             |
| /cmd_vel                                    |                                                                             |
| /cmd_vel_auto                               |                                                                             |
| /cmd_vel_manual                             |                                                                             |
| /current_station                            | 機器人當前最近站點資訊，包含站點 ID、類型、距離目標距離及抵達狀態                                          |
| /diagnostic_status                          |                                                                             |
| /exit_node_result                           | 站點退出節點查詢結果，返回站點後方可安全駛離的路網節點資訊                                               |
| /finetune_cmd                               |                                                                             |
| /free_nav_goal                              | 下達給 RouteExecutor 的自由導航目標，包含目標座標及終點朝向 yaw                                   |
| /free_nav_request                           | 接收 RViz 2D Goal Pose 發出的自由導航目標，包含目標位置與朝向 yaw                                |
| /global_costmap/costmap                     | 接收 Nav2 global costmap，用於擴增 A* 計算虛擬路點時的障礙物代價查詢                              |
| /imu                                        |                                                                             |
| /joint_states_in                            |                                                                             |
| /local_backup_trigger                       |                                                                             |
| /local_finetune                             |                                                                             |
| /lp_follow_status                           | 接收 LocalPlanner 回報的 FollowPath 執行結果，決定是否推進到下一段或重試                           |
| /lp_state                                   |                                                                             |
| /navigation_display                         |                                                                             |
| /nearest_node_result                        | 自由導航中最近路網中繼節點的查詢結果，供 UI 或上層系統顯示路由策略                                         |
| /odom                                       |                                                                             |
| /path_station_sequence                      | 完整的導航站點序列（含虛擬路點），供 RouteExecutor 與 UI 追蹤當前導航計畫                              |
| /path_warning                               | 偏離路徑警告，當機器人距最近邊超過閾值時觸發，提供偏離距離資訊                                             |
| /planned_path                               | 規劃完成的路網節點 ID 序列，依序為機器人需走訪的節點，發給 RouteExecutor 執行                            |
| /planned_route_path                         | 將 ComputeAndTrackRoute feedback 的路徑 poses 序列化後發給 LocalPlanner 執行 FollowPath |
| /pre_nav_spin                               | 觸發 LocalPlanner 執行 pre-nav spin，機器人面向目標後再執行自由導航                             |
| /request_exit_node                          | 接收站點退出節點查詢請求，依站點 ID 查找後方可安全退出的路網節點                                          |
| /request_path                               | 接收上層發出的算路請求，包含起點與終點節點 ID，觸發路徑規劃                                             |
| /robot_pose_odom                            | map frame 下的完整 Odometry，格式與 /odom 相同，twist 來自原始 /odom                       |
| /robot_state                                |                                                                             |
| /rosout                                     |                                                                             |
| /route_executor_state                       | 回報 RouteExecutor 當前執行狀態，供 UI 或 Debug 工具監控導航階段                               |
| /route_mission                              | 下達給 RouteExecutor 的路網導航任務，為經角度平滑後的節點序列                                      |
| /route_mission_coords                       | 配合 /route_mission 發布各節點座標資訊，供 RouteExecutor 計算終點 spin yaw                   |
| /route_status                               | 回報路網導航最終結果給 GlobalPlanner，作為任務完成或失敗的信號                                      |
| /scan                                       |                                                                             |
| /scan_b                                     |                                                                             |
| /speed_limit                                |                                                                             |
| /speed_limit_req                            |                                                                             |
| /system_status                              |                                                                             |
| /target_station                             |                                                                             |
| /task_agent/debug_state                     |                                                                             |
| /virtual_waypoints                          | 擴增 A* 計算出的虛擬路點序列，供 RouteExecutor 執行路網外的多跳導航                                 |
| <var:joint_input_topic>                     |                                                                             |
| <var:joint_output_topic>                    |                                                                             |
| <var:laser_input_topic>                     |                                                                             |
| <var:laser_output_topic>                    |                                                                             |
| <var:odom_input_topic>                      |                                                                             |
| <var:odom_output_topic>                     |                                                                             |
| <var:odom_topic>                            |                                                                             |
| <var:t>                                     |                                                                             |
| ?                                           |                                                                             |
| cmd_stop                                    | 接收緊急停止指令，立即取消所有進行中的路網與自由導航任務                                                |
| initialpose                                 |                                                                             |

## Actions

| 項目名稱 (Topic / Action / Service / Trigger)   | 功能備註 (Note)                                                |
|:--------------------------------------------|:-----------------------------------------------------------|
| backup                                      |                                                            |
| compute_and_track_route                     | 向 Nav2 請求路網路徑計算並追蹤，feedback 回傳路徑 poses 後轉交 LocalPlanner 執行 |
| dock_robot                                  |                                                            |
| drive_on_heading                            |                                                            |
| follow_path                                 |                                                            |
| navigate_to_pose                            | 向 Nav2 請求自由導航至任意目標座標，用於路網外的自由導航及虛擬路點到達                     |
| spin                                        |                                                            |
| undock_robot                                |                                                            |

## Services

| 項目名稱 (Topic / Action / Service / Trigger)   | 功能備註 (Note)   |
|:--------------------------------------------|:--------------|
| /local_costmap/clear_entirely_local_costmap |               |

## Callbacks

| 項目名稱 (Topic / Action / Service)   |
|:----------------------------------|
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| /robot_pose_odom                  |
| /rosout                           |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| /base_status                      |
| /global_costmap/costmap           |
| /free_nav_request                 |
| /request_exit_node                |
| /request_path                     |
| /robot_pose_odom                  |
| /route_status                     |
| —                                 |
| <var:joint_input_topic>           |
| <var:laser_input_topic>           |
| <var:odom_input_topic>            |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| cmd_stop                          |
| /local_finetune                   |
| /local_backup_trigger             |
| /planned_route_path               |
| /pre_nav_spin                     |
| /speed_limit_req                  |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| /clicked_point                    |
| /clicked_point                    |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| /planned_route_path               |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| <var:odom_topic>                  |
| —                                 |
| —                                 |
| /cmd_vel_auto                     |
| /cmd_vel_auto                     |
| /system_status                    |
| /system_status                    |
| /cmd_vel_manual                   |
| /cmd_vel_manual                   |
| —                                 |
| /odom                             |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| /current_station                  |
| /robot_pose_odom                  |
| /route_status                     |
| /path_station_sequence            |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| /base_status                      |
| /robot_pose_odom                  |
| cmd_stop                          |
| /free_nav_goal                    |
| /lp_follow_status                 |
| /route_mission                    |
| /route_mission_coords             |
| /virtual_waypoints                |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| —                                 |
| /base_status                      |
| /current_station                  |
| /exit_node_result                 |
| /finetune_cmd                     |
| /planned_path                     |
| /route_status                     |
| /target_station                   |
| —                                 |
| —                                 |
| /base_status                      |
| /robot_pose_odom                  |
| /planned_path                     |
| /route_status                     |
| /current_station                  |
| /path_warning                     |

## Orphan Topics

| 項目名稱 (Topic / Action / Service / Trigger)   | 功能備註 (Note)   |
|:--------------------------------------------|:--------------|
| /cmd_stop                                   |               |
| /cmd_vel                                    |               |
| /diagnostic_status                          |               |
| /lp_state                                   |               |
| /navigation_display                         |               |
| /nearest_node_result                        |               |
| /robot_state                                |               |
| /route_executor_state                       |               |
| /speed_limit                                |               |
| /task_agent/debug_state                     |               |
| <var:joint_output_topic>                    |               |
| <var:laser_output_topic>                    |               |
| <var:odom_output_topic>                     |               |
| ?                                           |               |
| initialpose                                 |               |
| /clicked_point                              |               |
| /cmd_vel_auto                               |               |
| /cmd_vel_manual                             |               |
| /global_costmap/costmap                     |               |
| /imu                                        |               |
| /joint_states_in                            |               |
| /odom                                       |               |
| /rosout                                     |               |
| /scan                                       |               |
| /scan_b                                     |               |
| /speed_limit_req                            |               |
| /system_status                              |               |
| <var:joint_input_topic>                     |               |
| <var:laser_input_topic>                     |               |
| <var:odom_input_topic>                      |               |
| <var:odom_topic>                            |               |
| <var:t>                                     |               |
| cmd_stop                                    |               |

