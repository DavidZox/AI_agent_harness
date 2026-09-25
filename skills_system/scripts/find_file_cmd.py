import sys
import os
import stat
import subprocess
import shlex
import time

NEWEST_STAT_LIMIT = 300   # 結果超過這麼多個就不逐一 stat（避免拖慢），只給數量


def newest_line(paths):
    """「最新修改：A（時間）；其次：B、C」——只算一般檔案，數值由腳本算、模型照抄。"""
    if len(paths) > NEWEST_STAT_LIMIT:
        return None
    stamped = []
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            stamped.append((st.st_mtime, p))
    if not stamped:
        return None
    stamped.sort(reverse=True)
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
    line = f"最新修改：{stamped[0][1]}（{fmt(stamped[0][0])}）"
    if len(stamped) > 1:
        line += "；其次：" + "、".join(f"{p}（{fmt(t)}）" for t, p in stamped[1:3])
    return line


def execute(args_str, parsed_args=None):
    """args_str 可以是參數清單（harness 已拆好）或整串字串（舊介面）。"""
    if isinstance(args_str, list):
        parsed_args = [a.strip("\"'") for a in args_str if a.strip("\"'")]
        args_str = " ".join(parsed_args)
    if not args_str or args_str.strip() == "":
        return "[ERROR] 缺少參數。請提供要搜尋的檔案名稱與路徑，例如: \"Modelfile\" ."
    
    # --- AI Generated Code / CLI Executor ---
    try:
        if not isinstance(parsed_args, list):
            parsed_args = shlex.split(args_str)
        
        # 移除可能不小心混入的 "find" 字眼
        clean_args = [arg for arg in parsed_args if arg.lower() != 'find']
        
        # 解析參數：通常 Agent 會傳入 ["檔名關鍵字", "路徑"] 或 ["路徑", "檔名關鍵字"]
        # 為了讓 Agent 隨便帶參數都能通，我們自動幫它優化成正規的 find 指令
        target_path = "."
        search_pattern = ""
        
        # 簡單的參數辨識邏輯
        for arg in clean_args:
            if arg.startswith('-'):
                continue # 忽略其他複雜的 flag
            elif os.path.isdir(arg) or arg == ".":
                target_path = arg
            else:
                search_pattern = arg
                
        if not search_pattern or search_pattern.strip() == "":
            # 只給了路徑、沒給關鍵字（實測模型想「列出全部檔案找最新的」時會這樣叫）：指路，不要把路徑當關鍵字亂搜
            return ("[ERROR] 缺少檔名關鍵字：find_file 需要「檔名關鍵字 [路徑]」，例如 \"Modelfile\" .；"
                    "要列出目錄內容或找最新修改的檔案請改用 list_dir（scripts/ls_cmd.py <path> --newest）。")

        # 🚨 物理邊界防護：嚴禁對全系統根目錄 '/' 執行搜尋
        if target_path == "/":
            return "[ERROR] 安全邊界阻斷：嚴禁對全系統根目錄 (/) 執行檔案搜尋，這會消耗大量系統資源。請指定具體目錄。"

        # 重新組合標準的 find 指令
        # -iname 代表「不區分大小寫」搜尋檔名，且前後加上 * 符號實現模糊搜尋
        cmd = ["find", target_path, "-iname", f"*{search_pattern}*"]
        
        # 執行 find
        result = subprocess.run(
            cmd, 
            stdout=subprocess.PIPE, 
            stderr=subprocess.PIPE, 
            text=True, 
            timeout=5  # 檔名搜尋很快，5 秒防護綽綽有餘
        )
        
        if result.returncode == 0:
            output = result.stdout.strip()
            if output:
                paths = output.splitlines()
                head = f"[PASS] 找到 {len(paths)} 個檔名含「{search_pattern}」的項目（搜尋 {target_path}）:"
                nl = newest_line(paths)
                return "\n".join([head] + ([nl] if nl else []) + paths)
            else:
                return f"[PASS] 找不到檔名包含 \"{search_pattern}\" 的檔案（0 個，搜尋 {target_path}）。"
        else:
            return f"[ERROR] Find 執行錯誤: {result.stderr.strip()}"
            
    except subprocess.TimeoutExpired:
        return "[ERROR] 搜尋超時！目錄結構可能過於龐大。"
    except Exception as e:
        return f"[ERROR] 執行異常: {str(e)}"
    # -------------------------

if __name__ == "__main__":
    try:
        print(execute(sys.argv[1:]))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)