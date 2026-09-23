# view_file 經驗記憶

> 使用者要求記住、專屬於此技能的經驗。載入 `view_file` 規格文件時會自動附在後面；由 `scripts/modify_memory_cmd.py "..." --skill view_file` 寫入，可直接編輯或刪除過時條目。

- [05-21 14:01] 操作流程規範 | 執行任何查看檔案內容的指令 (如 cat, view_file) | 必須先向使用者確認，禁止直接執行
- [09-19 19:15] 操作流程規範 | 針對特定資料夾內的檔案操作 (如 view_file, search_text) | 必須先使用 change_dir 切換到該資料夾，再執行後續操作，以確保路徑正確性。
