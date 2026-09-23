# list_dir 經驗記憶

> 使用者要求記住、專屬於此技能的經驗。載入 `list_dir` 規格文件時會自動附在後面；由 `scripts/modify_memory_cmd.py "..." --skill list_dir` 寫入，可直接編輯或刪除過時條目。

- [09-23 23:26] 操作流程規範 | list_dir 的適用範圍 | list_dir 技能只能用於查看本機（Host Machine）的路徑，不能用於容器內部（Container）的路徑
- [09-23 23:31] 操作流程規範 | 查看容器目錄內容 | 必須使用 docker_runcmd 技能，並將 'ls -laF' 等指令作為參數傳遞給容器，嚴禁使用 list_dir 技能。
