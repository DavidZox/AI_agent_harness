import tkinter as tk
from tkinter import ttk, messagebox
import subprocess
import os
from PIL import Image, ImageTk
import io
import threading
import ollama
import re
import time

MODEL_NAME = 'gemma4:e4b' 
# 定義兩個實體 PNG 檔案名稱
TEMP_FILE = 'fullscreen_capture.png'
CROP_FILE = 'cropped_capture.png'  # 新增：局部截圖的儲存檔名

# ---- 萬用 Unicode 解碼函數 ----
def decode_unicode_str(text):
    """
    將字串中殘留的 Unicode 逸出序列（形式為反斜線加小寫 u，後面接 4 位
    十六進位數字）還原成真正的文字字元。

    背景：Ollama／Gemma 模型回傳的內容有時不會是正常的 UTF-8 中文字，
    而是還沒被解碼的逸出序列（常見於某些序列化、傳輸過程沒有正確還原
    編碼），若直接塞進 Tkinter Text 元件會顯示成一串看不懂的逸出碼，
    而不是正確的中文。這裡用正規表達式找出所有這種樣式的逸出序列，
    逐一轉換成對應的 Unicode 字元後回傳。

    若傳入的 text 不是字串（例如 None 或其他型別），則原樣直接回傳，
    不做任何轉換。實際的字元還原邏輯委派給內部的 replace() 函式，
    交由 re.sub 對每個比對到的樣式呼叫一次。
    """
    if not isinstance(text, str):
        return text
    pattern = re.compile(r'\\u([0-9a-fA-F]{4})')
    def replace(match):
        """
        re.sub 的替換回呼函式：接收一個正規表達式比對結果 match（對應到
        字串中一段「反斜線加 u 加 4 位十六進位」的逸出序列），取出括號內
        擷取到的 4 碼十六進位文字（match.group(1)），轉成整數後再用
        chr() 還原成對應的 Unicode 字元並回傳，讓 pattern.sub 可以逐一
        把每個逸出序列換成真正的文字字元。
        """
        return chr(int(match.group(1), 16))
    return pattern.sub(replace, text)

class DecodedText(tk.Text):
    def insert(self, index, chars, *tags):
        """
        覆寫 tk.Text 原生的 insert()：在文字真正被插入元件之前，先呼叫
        decode_unicode_str() 把 chars 內殘留、尚未還原的 Unicode 逸出
        序列轉換成正常文字，再交給父類別 tk.Text.insert() 完成實際插入
        動作。如此一來，任何透過這個 DecodedText 元件（prompt_text／
        result_text）顯示的內容都會自動修正編碼問題，不需要在呼叫端
        另外處理。
        """
        decoded_chars = decode_unicode_str(chars)
        super().insert(index, decoded_chars, *tags)

class ScreenshotSniper:
    def __init__(self, root):
        """
        建立主視窗與應用程式狀態的初始化入口（單圖簡化版）。

        root 是 Tkinter 的根視窗，這裡設定視窗標題（含「WSL2 Double
        Save」字樣，對應 on_button_release 會把裁切結果額外存成
        CROP_FILE 這個行為）與視窗尺寸（480x680）。狀態初始化與多圖
        版本的差異在於這裡只保留單張裁切圖片：
        - full_screen_img：目前擷取到的整張螢幕截圖（PIL Image）。
        - snipped_image：目前唯一一張裁切結果（單數，非清單），
          每次框選都會被新的裁切結果覆蓋，而不是像多圖版本那樣
          累加進清單。
        - start_x / start_y：框選裁切時滑鼠按下當下的畫布座標。
        最後呼叫 setup_ui() 建立畫面元件。
        """
        self.root = root
        self.root.title("Gemma 4 Vision Sniper (WSL2 Double Save)")
        self.root.geometry("480x680")
        
        self.full_screen_img = None  
        self.snipped_image = None    
        self.start_x = None
        self.start_y = None
        
        self.setup_ui()

    def setup_ui(self):
        """
        建立主視窗的 UI 版面配置與元件（單圖簡化版，比多圖版本少了
        Clear 與 Run Inference 兩顆按鈕）。

        - 按鈕列：只有 Capture Screen（觸發 trigger_capture 截圖）
          與 Crop Image（觸發 open_crop_canvas 開啟框選視窗，初始為
          disabled，需先截圖成功才會啟用）。這個版本裡框選完成後會
          直接自動觸發推論（見 on_button_release），所以不需要獨立
          的 Run Inference 按鈕，也因為每次框選就會取代前一張圖，
          不需要額外的 Clear 按鈕。
        - Prompt 區塊：可編輯的 DecodedText，預先填入預設提示詞
          （判斷黑線是否通過藍色區域），其餘註解掉的字串是先前測試
          用的替代提示詞。
        - Preview 區塊：顯示目前擷取／框選圖片縮圖的 Label。
        - Gemma 4 Inference Result 區塊：顯示模型回覆內容的
          DecodedText。
        這裡只負責建立元件與版面、綁定按鈕的 command callback。
        """
        btn_frame = ttk.Frame(self.root, padding=10)
        btn_frame.pack(fill=tk.X)
        
        self.snap_btn = tk.Button(
            btn_frame, 
            text=" 📸 Capture Screen (截取全螢幕) ", 
            command=self.trigger_capture,
            font=("Sans", 10, "bold"),
            bg="#0078d7",
            fg="white",
            relief=tk.FLAT,
            padx=8,
            pady=5
        )
        self.snap_btn.pack(side=tk.LEFT, padx=5)
        
        self.crop_btn = tk.Button(
            btn_frame, 
            text=" ✂️ Crop Image (開始框選區域) ", 
            command=self.open_crop_canvas,
            font=("Sans", 10, "bold"),
            bg="#28a745",
            fg="white",
            relief=tk.FLAT,
            padx=8,
            pady=5,
            state=tk.DISABLED
        )
        self.crop_btn.pack(side=tk.LEFT, padx=5)
        
        prompt_frame = tk.LabelFrame(self.root, text=" Prompt (提示詞) ", font=("Sans", 11, "bold"), padx=10, pady=5)
        prompt_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.prompt_text = DecodedText(prompt_frame, height=3, font=("Sans", 10), relief=tk.SOLID, bd=1)
        self.prompt_text.pack(fill=tk.X)

        default_prompt = "圖中黑線是否有通過藍色區域，若是有請簡潔回答有，反之回答沒有，不需要解釋畫面其他細節。"
        # default_prompt = "圖中的tb1、tb2和tb3是自主移動機器人，著重輸出每台機器人當前所在的線段顏色，每台機器人都要有輸出，不需要解釋畫面其他細節。"
        # default_prompt = "圖中白色部分是目前SLAM技術掃出的可行駛區域部分，幫我判斷目前白色部分外圍輪廓是否已經有輻射狀的狀況，若是有請簡潔回答是沒有回答否，不需要解釋畫面其他細節。"
        # default_prompt = "圖中有箭頭的線代表規劃的軌跡方向，目前只能7到1，不能1到7，若是目前畫面中有違反規則的話，請簡潔回答有違反反之則回答沒有違反，不需要解釋畫面其他細節。"

        self.prompt_text.insert(tk.END, default_prompt)
        
        preview_frame = tk.LabelFrame(self.root, text=" Preview (畫面預覽) ", font=("Sans", 11, "bold"), padx=10, pady=5)
        preview_frame.pack(fill=tk.X, padx=10, pady=5)
        
        self.preview_label = tk.Label(preview_frame, text="[ No Image Captured / 尚未截取畫面 ]", anchor="center", font=("Sans", 10), height=8, bg="#f0f0f0")
        self.preview_label.pack(fill=tk.BOTH, expand=True)
        
        result_frame = tk.LabelFrame(self.root, text=" 🤖 Gemma 4 Inference Result (推論結果) ", font=("Sans", 11, "bold"), padx=10, pady=5)
        result_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        
        self.result_text = DecodedText(result_frame, wrap=tk.WORD, height=12, font=("Sans", 10), relief=tk.SOLID, bd=1)
        self.result_text.pack(fill=tk.BOTH, expand=True)

    def capture_via_wsl_hybrid(self):
        """
        透過「WSL 呼叫 Windows PowerShell」的混合方式擷取整個螢幕
        畫面（與多圖版本邏輯相同）。

        1. 算出 TEMP_FILE 在 Linux 端的絕對路徑，若舊檔案存在就先
           刪除，避免殘留上一次截圖。
        2. 嘗試用 wslpath -w 把 Linux 路徑轉成 Windows 路徑；若失敗
           則改用 WSL_DISTRO_NAME 環境變數手動組出一個以
           wsl.localhost 開頭、對應目前發行版的 UNC 網路路徑格式
           當作備援。
        3. 組出 PowerShell 指令：載入 System.Drawing 與
           System.Windows.Forms，透過 P/Invoke 呼叫 user32.dll 的
           SetProcessDPIAware 避免高 DPI 縮放造成擷取尺寸失真，再用
           SystemInformation.VirtualScreen 取得完整虛擬螢幕範圍，
           建立對應大小的 Bitmap 並用 CopyFromScreen 把畫面內容
           複製進去，最後存成 PNG 到轉換好的 Windows 路徑。
        4. 呼叫 powershell.exe 執行（隱藏 stdout/stderr）。
        5. 若輸出檔案確實出現在 Linux 路徑上，用 PIL 開啟並轉成 RGB
           模式後回傳；任何步驟失敗都會被捕捉、印出錯誤訊息，並
           回傳 None。
        """
        linux_file_path = os.path.abspath(TEMP_FILE)
        
        if os.path.exists(linux_file_path):
            try:
                os.remove(linux_file_path)
            except:
                pass

        try:
            result = subprocess.run(['wslpath', '-w', linux_file_path], capture_output=True, text=True, check=True)
            win_temp_path = result.stdout.strip()
        except Exception as e:
            wsl_distro = os.environ.get('WSL_DISTRO_NAME', 'Ubuntu')
            win_temp_path = f"\\\\wsl.localhost\\{wsl_distro}{linux_file_path}".replace('/', '\\')

        ps_command = (
            "[Reflection.Assembly]::LoadWithPartialName('System.Drawing') | Out-Null; "
            "[Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
            "$type = Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();' -Name 'User32' -Namespace 'Win32' -PassThru; "
            "[Win32.User32]::SetProcessDPIAware() | Out-Null; "
            
            "$screen = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$bmp = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height; "
            "$graphics = [System.Drawing.Graphics]::FromImage($bmp); "
            "$graphics.CopyFromScreen($screen.Left, $screen.Top, 0, 0, $bmp.Size); "
            f"$bmp.Save('{win_temp_path}', [System.Drawing.Imaging.ImageFormat]::Png); "
            "$graphics.Dispose(); $bmp.Dispose();"
        )
        
        try:
            subprocess.run(["powershell.exe", "-Command", ps_command], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(linux_file_path):
                img = Image.open(linux_file_path)
                return img.convert("RGB")
        except Exception as e:
            print(f"WSL Hybrid Capture Error: {e}")
        return None

    def trigger_capture(self):
        """
        「Capture Screen」按鈕的 callback，負責截圖流程的前後置
        動作。

        先 iconify 縮小自己的視窗避免拍到自己，sleep(0.5) 等待
        Windows 完成縮小動畫，再呼叫 capture_via_wsl_hybrid() 擷取
        整個螢幕，最後 deiconify 還原視窗。

        若截圖失敗（回傳 None），跳出包含 TEMP_FILE 檔名的錯誤訊息
        並直接 return；成功則存進 self.full_screen_img、更新預覽
        縮圖，並啟用 Crop Image 按鈕（需要先有完整截圖才能框選）。
        這個版本沒有圖片數量的狀態列，因此不像多圖版本那樣需要
        另外呼叫 update_status()。
        """
        self.root.iconify() 
        time.sleep(0.5)     
        captured_img = self.capture_via_wsl_hybrid()
        self.root.deiconify() 
        
        if captured_img is None:
            messagebox.showerror("Error", f"無法抓取畫面或無法將檔案儲存至當前路徑！\n檔名: {TEMP_FILE}")
            return

        self.full_screen_img = captured_img
        self.display_preview(self.full_screen_img)
        self.crop_btn.config(state=tk.NORMAL)

    def open_crop_canvas(self):
        """
        開啟全螢幕的裁切框選視窗，讓使用者用滑鼠拖曳出想要裁切的
        區域（邏輯與多圖版本相同）。

        若尚未有 full_screen_img，直接 return 防呆。建立一個無邊框
        （overrideredirect）、覆蓋整個螢幕、永遠置頂（-topmost）的
        Toplevel 當作框選遮罩，把 full_screen_img 縮放成與視窗相同
        的像素尺寸後畫到 Canvas 上當背景。

        綁定 ButtonPress-1／B1-Motion／ButtonRelease-1 分別對應
        on_button_press／on_move_press／on_button_release 處理拖曳
        選取的三個階段，並綁定 Escape 鍵呼叫 close_canvas_safely()
        取消框選。最後用 grab_set() 讓遮罩視窗獨佔輸入焦點，避免
        操作誤觸到背後的主視窗。
        """
        if self.full_screen_img is None:
            return
            
        self.snip_win = tk.Toplevel(self.root)
        
        win_w = self.snip_win.winfo_screenwidth()
        win_h = self.snip_win.winfo_screenheight()
        
        self.snip_win.overrideredirect(True) 
        self.snip_win.geometry(f"{win_w}x{win_h}+0+0")
        self.snip_win.attributes("-topmost", True) 
        
        resized_full_img = self.full_screen_img.resize((win_w, win_h), Image.Resampling.LANCZOS)
        self.bg_image_tk = ImageTk.PhotoImage(resized_full_img)
        
        self.canvas = tk.Canvas(self.snip_win, cursor="crosshair", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.bg_image_tk)
        
        self.canvas.bind("<ButtonPress-1>", self.on_button_press)
        self.canvas.bind("<B1-Motion>", self.on_move_press)
        self.canvas.bind("<ButtonRelease-1>", self.on_button_release)
        
        self.snip_win.bind("<Escape>", lambda e: self.close_canvas_safely())
        
        self.rect = None

        self.snip_win.update()
        self.snip_win.focus_force()
        self.snip_win.grab_set()

    def close_canvas_safely(self):
        """
        安全關閉框選裁切用的 Toplevel 遮罩視窗：先 grab_release()
        解除 open_crop_canvas() 設下的模態輸入獨佔，再 destroy()
        銷毀視窗，避免殘留的 grab 導致主視窗之後收不到輸入事件。
        同時被 Escape 鍵與框選完成後的流程呼叫，統一負責收尾。
        """
        if self.snip_win:
            self.snip_win.grab_release()
            self.snip_win.destroy()

    def on_button_press(self, event):
        """
        框選畫布滑鼠「按下」事件（ButtonPress-1）的處理函式。

        記錄按下當下的畫布座標為拖曳起點 self.start_x／
        self.start_y，並建立一個起始大小為 0（四個角座標相同）的
        紅色外框矩形存入 self.rect，作為後續 on_move_press 更新
        拖曳範圍的基礎。
        """
        self.start_x = event.x
        self.start_y = event.y
        self.rect = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline='red', width=2)

    def on_move_press(self, event):
        """
        框選畫布滑鼠「拖曳中」事件（B1-Motion）的處理函式，拖曳
        期間會連續觸發。取得目前滑鼠座標，若 self.rect 已存在，
        就用 canvas.coords() 把矩形從固定起點延伸到目前游標位置，
        呈現即時變化的橡皮筋選取框效果。
        """
        cur_x, cur_y = (event.x, event.y)
        if self.rect:
            self.canvas.coords(self.rect, self.start_x, self.start_y, cur_x, cur_y)

    def on_button_release(self, event):
        """
        框選畫布滑鼠「放開」事件（ButtonRelease-1）的處理函式：把
        拖曳範圍換算成實際裁切座標，完成裁切後自動啟動一次模型
        推論（這是與多圖版本最大的行為差異——這裡不需要使用者
        另外按「Run Inference」）。

        先用 min/max 把起點與終點正規化成 (x1, y1)-(x2, y2)，記下
        遮罩視窗當時的顯示尺寸，並呼叫 close_canvas_safely() 關閉
        遮罩視窗（無論框選是否有效都會先關閉）。

        只有框選範圍長寬都大於 5 像素才視為有效：把畫布座標依視窗
        尺寸與原始截圖尺寸的比例換算回原始解析度座標，對
        full_screen_img 裁切後取代 self.snipped_image（單張圖片，
        覆蓋前一次結果，與多圖版本 append 進清單的做法不同）。接著
        嘗試把這張裁切圖另存成 CROP_FILE（cropped_capture.png，
        對應「Double Save」：全螢幕圖與裁切圖各自落地一份），存檔
        失敗只會印出錯誤訊息、不會中斷流程。最後更新預覽縮圖，並
        直接呼叫 run_inference_thread() 自動開始推論，不需要額外
        的使用者操作。
        """
        end_x, end_y = (event.x, event.y)
        x1, y1 = min(self.start_x, end_x), min(self.start_y, end_y)
        x2, y2 = max(self.start_x, end_x), max(self.start_y, end_y)
        
        win_w = self.snip_win.winfo_width()
        win_h = self.snip_win.winfo_height()
        
        self.close_canvas_safely()
        
        if x2 - x1 > 5 and y2 - y1 > 5:
            orig_w, orig_h = self.full_screen_img.size
            rx1 = int(x1 * orig_w / win_w)
            ry1 = int(y1 * orig_h / win_h)
            rx2 = int(x2 * orig_w / win_w)
            ry2 = int(y2 * orig_h / win_h)
            
            # 1. 局部切圖
            self.snipped_image = self.full_screen_img.crop((rx1, ry1, rx2, ry2))
            
            # 【關鍵修改】2. 將切出來的局部截圖即時存檔至 Linux 當前目錄
            try:
                linux_crop_path = os.path.abspath(CROP_FILE)
                self.snipped_image.save(linux_crop_path, format='PNG')
                print(f"[SUCCESS] 局部截圖已儲存至: {linux_crop_path}")
            except Exception as e:
                print(f"[ERROR] 無法儲存局部截圖: {e}")

            # 3. 更新 UI 預覽並送交 Gemma 4 推論
            self.display_preview(self.snipped_image) 
            self.run_inference_thread()

    def display_preview(self, img_to_show):
        """
        將指定的 PIL Image 縮圖後顯示在 Preview 區塊的 Label 上。

        先 copy() 一份再呼叫 thumbnail()（in-place 操作）縮放至
        最大 440x180，避免動到呼叫端仍持有的原始圖片物件；轉成
        PhotoImage 後設定給 preview_label 並清除提示文字，同時把
        img_tk 額外存一份參照到 self.preview_label.image，避免這個
        PhotoImage 因為沒有 Python 變數持續參照而被垃圾回收，導致
        畫面顯示後很快變空白。
        """
        preview_img = img_to_show.copy()
        preview_img.thumbnail((440, 180)) 
        img_tk = ImageTk.PhotoImage(preview_img)
        self.preview_label.config(image=img_tk, text="")
        self.preview_label.image = img_tk

    def run_inference_thread(self):
        """
        啟動背景執行緒呼叫模型推論，避免呼叫 Ollama 期間卡住
        Tkinter 主執行緒。這個版本是在 on_button_release 裁切完成
        後被自動呼叫（而不是綁在獨立按鈕上），因此不像多圖版本
        那樣需要檢查是否已有圖片——呼叫到這裡時 self.snipped_image
        一定已經被設定好。

        先清空 result_text 並顯示「Gemma 4 Thinking (模型思考中)
        ...」的暫時提示，再用 threading.Thread 建立 daemon 執行緒
        執行 send_to_gemma()，避免推論期間整個視窗失去回應。
        """
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, "Gemma 4 Thinking (模型思考中)...\n")
        threading.Thread(target=self.send_to_gemma, daemon=True).start()

    def send_to_gemma(self):
        """
        在背景執行緒中呼叫 Ollama 執行 Gemma 4 視覺模型推論（由
        run_inference_thread 啟動）。

        若 self.snipped_image 仍是 None（理論上不太會發生，因為
        只有裁切成功才會呼叫到這裡，屬於防呆判斷），直接 return
        不送出請求。

        否則把這張裁切圖編碼成 PNG 位元組（透過記憶體中的
        BytesIO），讀出 prompt_text 目前的提示詞並去除前後空白，
        呼叫 ollama.chat() 送出單一張圖片給模型（對應 images 參數
        只帶一個元素，與多圖版本一次送多張不同）。

        推論成功時透過 self.root.after(0, ...) 把 update_result()
        排程回主執行緒執行並帶上模型回覆內容，因為 Tkinter 元件
        不是執行緒安全的，不能在背景執行緒直接操作 UI。若發生例外
        （Ollama 未啟動、模型未下載等），一樣透過 root.after 顯示
        整理過的錯誤訊息。
        """
        if self.snipped_image is None:
            return
            
        try:
            img_byte_arr = io.BytesIO()
            self.snipped_image.save(img_byte_arr, format='PNG')
            img_bytes = img_byte_arr.getvalue()
            
            user_prompt = self.prompt_text.get("1.0", tk.END).strip()
            
            response = ollama.chat(
                model=MODEL_NAME,
                messages=[{
                    'role': 'user',
                    'content': user_prompt,
                    'images': [img_bytes]
                }]
            )
            
            self.root.after(0, self.update_result, response['message']['content'])
            
        except Exception as e:
            error_msg = f"Error: {str(e)}\nPlease check if Ollama is running and {MODEL_NAME} is downloaded."
            self.root.after(0, self.update_result, error_msg)

    def update_result(self, text):
        """
        在 Tkinter 主執行緒上更新 Gemma Result 區塊的顯示內容（由
        send_to_gemma 透過 root.after 排程呼叫）。清空 result_text
        後插入新的 text（模型回覆或錯誤訊息），因為 result_text 是
        DecodedText，插入時會自動修正內容中殘留、尚未還原的
        Unicode 逸出序列。
        """
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, text)

if __name__ == "__main__":
    root = tk.Tk()
    app = ScreenshotSniper(root)
    root.mainloop()