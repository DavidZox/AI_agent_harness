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

TEMP_FILE = 'fullscreen_capture.png'


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
        建立主視窗與應用程式狀態的初始化入口。

        root 是 Tkinter 的根視窗（tk.Tk 實例），這裡設定視窗標題與
        尺寸（520x760），並初始化後續流程會用到的狀態：
        - full_screen_img：目前擷取到的整張螢幕截圖（PIL Image），
          未截圖前為 None。
        - snipped_images：使用者框選出來、準備送給模型的多張裁切
          圖片清單（多圖版本的核心差異：用 list 累積，而非單張圖片）。
        - start_x / start_y：框選裁切區域時滑鼠按下當下的畫布座標，
          在拖曳過程中作為矩形的固定起點。
        最後呼叫 setup_ui() 實際建立所有畫面元件。
        """
        self.root = root

        self.root.title("Gemma 4 Vision Multi-Image Sniper")
        self.root.geometry("520x760")

        self.full_screen_img = None

        # ===== 改成多張圖 =====
        self.snipped_images = []

        self.start_x = None
        self.start_y = None

        self.setup_ui()

    def setup_ui(self):
        """
        建立整個主視窗的 UI 版面配置與元件，包含：
        - 按鈕列：Capture Screen（觸發 trigger_capture 截圖）、
          Add Crop（觸發 open_crop_canvas 開啟框選視窗，初始為
          disabled，需先截圖成功才會啟用）、Clear（觸發 clear_images
          清空已加入的圖片）、Run Inference（觸發 run_inference_thread
          開始推論）。
        - Prompt 區塊：可編輯的 DecodedText，預先填入預設提示詞
          （「解釋一下這張圖」），使用者送出推論前可自行修改內容。
        - Preview 區塊：顯示目前擷取／框選圖片縮圖的 Label。
        - 狀態列：顯示目前已加入的裁切圖片張數（status_label）。
        - Gemma Result 區塊：顯示模型回覆內容的 DecodedText。
        這裡只負責建立元件與版面、綁定按鈕的 command callback，不包含
        任何截圖或推論的實際邏輯。
        """

        btn_frame = ttk.Frame(self.root, padding=10)
        btn_frame.pack(fill=tk.X)

        # ===== 全螢幕截圖 =====
        self.snap_btn = tk.Button(
            btn_frame,
            text=" 📸 Capture Screen ",
            command=self.trigger_capture,
            font=("Sans", 10, "bold"),
            bg="#0078d7",
            fg="white",
            relief=tk.FLAT,
            padx=8,
            pady=5
        )
        self.snap_btn.pack(side=tk.LEFT, padx=5)

        # ===== Crop =====
        self.crop_btn = tk.Button(
            btn_frame,
            text=" ✂️ Add Crop ",
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

        # ===== 清空圖片 =====
        self.clear_btn = tk.Button(
            btn_frame,
            text=" 🗑 Clear ",
            command=self.clear_images,
            font=("Sans", 10, "bold"),
            bg="#dc3545",
            fg="white",
            relief=tk.FLAT,
            padx=8,
            pady=5
        )
        self.clear_btn.pack(side=tk.LEFT, padx=5)

        # ===== 推論 =====
        self.run_btn = tk.Button(
            btn_frame,
            text=" 🤖 Run Inference ",
            command=self.run_inference_thread,
            font=("Sans", 10, "bold"),
            bg="#6f42c1",
            fg="white",
            relief=tk.FLAT,
            padx=8,
            pady=5
        )
        self.run_btn.pack(side=tk.LEFT, padx=5)

        # ===== Prompt =====
        prompt_frame = tk.LabelFrame(
            self.root,
            text=" Prompt ",
            font=("Sans", 11, "bold"),
            padx=10,
            pady=5
        )
        prompt_frame.pack(fill=tk.X, padx=10, pady=5)

        self.prompt_text = DecodedText(
            prompt_frame,
            height=5,
            font=("Sans", 10),
            relief=tk.SOLID,
            bd=1
        )
        self.prompt_text.pack(fill=tk.X)

        default_prompt = (
            "解釋一下這張圖\n"
        )
        #default_prompt = (
        #    "這些圖是時間獨立的，圖中白色部分是目前SLAM技術掃出的可行駛區域部分\n"
        #    "請綜合所有圖片判斷：\n"
        #    "這些圖的差異是什麼，請簡短解釋差異點\n"
        #   "不需要解釋其他內容。"
        #)

        self.prompt_text.insert(tk.END, default_prompt)

        # ===== 預覽 =====
        preview_frame = tk.LabelFrame(
            self.root,
            text=" Preview ",
            font=("Sans", 11, "bold"),
            padx=10,
            pady=5
        )
        preview_frame.pack(fill=tk.X, padx=10, pady=5)

        self.preview_label = tk.Label(
            preview_frame,
            text="[ No Image ]",
            anchor="center",
            font=("Sans", 10),
            height=8,
            bg="#f0f0f0"
        )

        self.preview_label.pack(fill=tk.BOTH, expand=True)

        # ===== 狀態 =====
        self.status_label = tk.Label(
            self.root,
            text="目前已加入圖片數量：0",
            font=("Sans", 10, "bold"),
            fg="blue"
        )
        self.status_label.pack(pady=5)

        # ===== 結果 =====
        result_frame = tk.LabelFrame(
            self.root,
            text=" Gemma Result ",
            font=("Sans", 11, "bold"),
            padx=10,
            pady=5
        )

        result_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.result_text = DecodedText(
            result_frame,
            wrap=tk.WORD,
            height=12,
            font=("Sans", 10),
            relief=tk.SOLID,
            bd=1
        )

        self.result_text.pack(fill=tk.BOTH, expand=True)

    # ==========================================================
    # 擷取全螢幕
    # ==========================================================
    def capture_via_wsl_hybrid(self):
        """
        透過「WSL 呼叫 Windows PowerShell」的混合方式擷取整個螢幕
        畫面。

        因為程式跑在 WSL（Linux 環境）裡，沒辦法直接存取 Windows 的
        螢幕，所以改用呼叫 Windows 端的 PowerShell 來完成截圖，再把
        結果存到雙方都能看到的檔案路徑，最後從 Linux 這側讀回來：
        1. 先算出 TEMP_FILE 在 Linux 端的絕對路徑，若舊檔案還在就先
           刪除，避免讀到上一次截圖留下的殘檔。
        2. 用 wslpath -w 把這個 Linux 路徑轉成 Windows 路徑格式；
           如果 wslpath 呼叫失敗（例如環境沒有這個指令），退回用
           WSL_DISTRO_NAME 環境變數手動拼出一個以 wsl.localhost 開頭、
           對應目前發行版的 UNC 網路路徑格式，當作備援方案。
        3. 組出一段 PowerShell 指令字串：載入 System.Drawing 與
           System.Windows.Forms、透過 P/Invoke 呼叫 user32.dll 的
           SetProcessDPIAware（避免高 DPI 縮放造成截圖尺寸與座標不
           準確），再用 SystemInformation.VirtualScreen 取得涵蓋
           所有螢幕的完整虛擬螢幕範圍，建立對應大小的 Bitmap，透過
           CopyFromScreen 把畫面內容複製進去，最後存成 PNG 到剛剛
           轉換好的 Windows 路徑。
        4. 呼叫 powershell.exe 執行上述指令（隱藏 stdout/stderr）。
        5. 若輸出檔案確實出現在 Linux 路徑上，用 PIL 開啟並轉成 RGB
           模式後回傳；任何一個步驟丟出例外都會被捕捉、印出錯誤
           訊息，並回傳 None 讓呼叫端知道截圖失敗。
        """

        linux_file_path = os.path.abspath(TEMP_FILE)

        if os.path.exists(linux_file_path):
            try:
                os.remove(linux_file_path)
            except:
                pass

        try:
            result = subprocess.run(
                ['wslpath', '-w', linux_file_path],
                capture_output=True,
                text=True,
                check=True
            )

            win_temp_path = result.stdout.strip()

        except Exception:
            wsl_distro = os.environ.get('WSL_DISTRO_NAME', 'Ubuntu')

            win_temp_path = (
                f"\\\\wsl.localhost\\{wsl_distro}{linux_file_path}"
            ).replace('/', '\\')

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
            subprocess.run(
                ["powershell.exe", "-Command", ps_command],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

            if os.path.exists(linux_file_path):
                img = Image.open(linux_file_path)
                return img.convert("RGB")

        except Exception as e:
            print(f"Capture Error: {e}")

        return None

    # ==========================================================
    # 觸發截圖
    # ==========================================================
    def trigger_capture(self):
        """
        「Capture Screen」按鈕的 callback：負責整個截圖流程的前後置
        動作。

        先把自己的視窗縮到工作列（iconify），避免這個 Tkinter 視窗
        本身也被拍進截圖畫面裡；接著 sleep(0.5) 讓 Windows 有時間
        真正完成縮小視窗的動畫與畫面重繪，再呼叫
        capture_via_wsl_hybrid() 實際擷取整個螢幕，最後把視窗還原
        （deiconify）。

        若截圖失敗（回傳 None），跳出錯誤訊息並直接 return，不更動
        任何狀態。截圖成功則存進 self.full_screen_img、更新預覽縮圖、
        啟用「Add Crop」按鈕（需要先有完整截圖才能框選裁切），並呼叫
        update_status() 刷新目前已加入圖片的數量顯示。注意這裡只是
        取得整張畫面，尚未加入 snipped_images 清單，真正加入清單是
        在框選裁切完成之後。
        """

        self.root.iconify()

        time.sleep(0.5)

        captured_img = self.capture_via_wsl_hybrid()

        self.root.deiconify()

        if captured_img is None:
            messagebox.showerror("Error", "無法抓取畫面")
            return

        self.full_screen_img = captured_img

        # 只顯示預覽
        self.display_preview(captured_img)

        # 開啟 crop 功能
        self.crop_btn.config(state=tk.NORMAL)

        # 不加入 model input
        self.update_status()

    # ==========================================================
    # 開啟 Crop 視窗
    # ==========================================================
    def open_crop_canvas(self):
        """
        開啟全螢幕的裁切框選視窗，讓使用者用滑鼠拖曳出想要裁切的
        區域。

        若尚未有 full_screen_img（還沒截過圖），直接 return（正常
        情況下 Add Crop 按鈕在截圖前是 disabled 的，這裡是多一層
        防呆）。

        建立一個沒有邊框（overrideredirect）、覆蓋整個螢幕大小、
        並且永遠置頂（-topmost）的 Toplevel 視窗當作框選遮罩；把
        先前截到的 full_screen_img 縮放成與這個視窗完全相同的像素
        尺寸後畫到 Canvas 上當作背景（讓畫面上看到的座標可以直接
        對應到視窗座標，實際換算回原始解析度的動作在
        on_button_release 才進行）。

        在這個 Canvas 上綁定三個滑鼠事件：ButtonPress-1 開始拖曳
        （on_button_press）、B1-Motion 拖曳中即時更新框選範圍
        （on_move_press）、ButtonRelease-1 放開滑鼠完成裁切
        （on_button_release）；並綁定 Escape 鍵可直接取消、安全
        關閉這個遮罩視窗（close_canvas_safely）。

        最後用 grab_set() 讓這個視窗獨佔輸入焦點（模態），確保
        使用者的滑鼠鍵盤操作都作用在框選遮罩上，而不會誤觸到背後
        的主視窗。
        """

        if self.full_screen_img is None:
            return

        self.snip_win = tk.Toplevel(self.root)

        win_w = self.snip_win.winfo_screenwidth()
        win_h = self.snip_win.winfo_screenheight()

        self.snip_win.overrideredirect(True)

        self.snip_win.geometry(f"{win_w}x{win_h}+0+0")

        self.snip_win.attributes("-topmost", True)

        resized_full_img = self.full_screen_img.resize(
            (win_w, win_h),
            Image.Resampling.LANCZOS
        )

        self.bg_image_tk = ImageTk.PhotoImage(resized_full_img)

        self.canvas = tk.Canvas(
            self.snip_win,
            cursor="crosshair",
            highlightthickness=0
        )

        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.canvas.create_image(
            0,
            0,
            anchor=tk.NW,
            image=self.bg_image_tk
        )

        self.canvas.bind("<ButtonPress-1>", self.on_button_press)
        self.canvas.bind("<B1-Motion>", self.on_move_press)
        self.canvas.bind("<ButtonRelease-1>", self.on_button_release)

        self.snip_win.bind(
            "<Escape>",
            lambda e: self.close_canvas_safely()
        )

        self.rect = None

        self.snip_win.update()

        self.snip_win.focus_force()

        self.snip_win.grab_set()

    # ==========================================================
    def close_canvas_safely(self):
        """
        安全關閉框選裁切用的 Toplevel 遮罩視窗。

        先呼叫 grab_release() 解除 open_crop_canvas() 設下的模態輸入
        獨佔，再 destroy() 整個視窗，避免視窗關閉後前一個 grab_set()
        仍殘留、導致主視窗之後收不到滑鼠或鍵盤事件。這個函式同時被
        Escape 鍵綁定與 on_button_release 完成裁切後呼叫，用來統一
        收尾流程。
        """

        if self.snip_win:
            self.snip_win.grab_release()
            self.snip_win.destroy()

    # ==========================================================
    def on_button_press(self, event):
        """
        框選畫布上滑鼠「按下」事件（ButtonPress-1）的處理函式。

        把當下滑鼠在 Canvas 上的座標（event.x／event.y）記錄成拖曳的
        起點 self.start_x／self.start_y，並在該位置建立一個一開始四個
        角座標都相同（也就是尚未展開、大小為 0）的紅色外框矩形，存到
        self.rect，後續 on_move_press 會不斷更新這個矩形的座標，形成
        拖曳選取框的視覺效果。
        """

        self.start_x = event.x
        self.start_y = event.y

        self.rect = self.canvas.create_rectangle(
            self.start_x,
            self.start_y,
            self.start_x,
            self.start_y,
            outline='red',
            width=2
        )

    # ==========================================================
    def on_move_press(self, event):
        """
        框選畫布上滑鼠「拖曳中」事件（B1-Motion）的處理函式，拖曳過程中
        會被連續觸發多次。

        取得目前滑鼠座標 (cur_x, cur_y)，只要 self.rect 已經存在（代表
        on_button_press 已建立起始矩形），就用 canvas.coords() 把矩形的
        座標從固定的起點 (start_x, start_y) 延伸到目前游標位置，讓使用者
        看到即時變化的選取框（橡皮筋效果）。
        """

        cur_x, cur_y = (event.x, event.y)

        if self.rect:
            self.canvas.coords(
                self.rect,
                self.start_x,
                self.start_y,
                cur_x,
                cur_y
            )

    # ==========================================================
    def on_button_release(self, event):
        """
        框選畫布上滑鼠「放開」事件（ButtonRelease-1）的處理函式，
        負責把使用者拖曳出的矩形範圍換算成實際裁切座標，完成一次
        裁切並加入多圖清單。

        先取得放開當下的座標，用 min/max 把起點與終點正規化成
        (x1, y1)-(x2, y2)（讓使用者無論往哪個方向拖曳都能正確算出
        矩形），並記下框選遮罩視窗當時的顯示尺寸；接著無論這次框選
        是否有效，都會先呼叫 close_canvas_safely() 關閉遮罩視窗。

        只有當框選範圍長寬都大於 5 像素時（避免誤觸或極小拖曳被
        當成有效裁切）才會真正處理：因為遮罩視窗顯示的背景圖是把
        原始截圖縮放到視窗大小後畫上去的，所以要先算出視窗尺寸與
        原始截圖尺寸的比例，把畫布座標換算回原始解析度下的座標
        (rx1, ry1, rx2, ry2)，再用這組座標對 self.full_screen_img
        做裁切；裁切結果會 append 進 self.snipped_images 清單（多圖
        版本的核心：每次框選都是新增一張圖，而不是取代），然後更新
        預覽縮圖與狀態列的圖片張數顯示。
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

            cropped_img = self.full_screen_img.crop(
                (rx1, ry1, rx2, ry2)
            )

            # ===== 加入多圖 list =====
            self.snipped_images.append(cropped_img)

            self.display_preview(cropped_img)

            self.update_status()

    # ==========================================================
    def display_preview(self, img_to_show):
        """
        將指定的 PIL Image 縮圖後顯示在 Preview 區塊的 Label 上。

        img_to_show 可能是完整截圖或某次裁切結果；這裡先 copy() 一份
        再呼叫 thumbnail()，避免直接改動（thumbnail 是 in-place 操作）
        到呼叫端仍在使用的原始圖片物件。縮放後轉成 Tkinter 能顯示的
        PhotoImage，設定給 preview_label 並清掉原本的提示文字。

        最後把 img_tk 額外存一份參照到 self.preview_label.image：這是
        Tkinter 常見的陷阱——PhotoImage 若沒有被任何 Python 變數持續
        參照住，會被垃圾回收，導致畫面上的圖片顯示後很快變成空白。
        """
        preview_img = img_to_show.copy()
        preview_img.thumbnail((460, 220))
        img_tk = ImageTk.PhotoImage(preview_img)
        self.preview_label.config(
            image=img_tk,
            text=""
        )
        self.preview_label.image = img_tk

    # ==========================================================
    def update_status(self):
        """
        刷新狀態列文字，顯示目前 self.snipped_images 清單中已經加入
        的裁切圖片張數（例如「目前已加入圖片數量：3」），讓使用者
        知道按下 Run Inference 時會送出幾張圖給模型。
        """
        self.status_label.config(
            text=f"目前已加入圖片數量：{len(self.snipped_images)}"
        )

    # ==========================================================
    def clear_images(self):
        """
        「Clear」按鈕的 callback：重置目前這一輪的擷圖與推論結果。

        清空 self.snipped_images 清單（丟棄所有已框選但尚未清除的
        裁切圖片）、把預覽 Label 復原成無圖片的提示文字、呼叫
        update_status() 讓圖片數量歸零顯示，並清空 result_text 裡
        先前模型回覆的內容，讓使用者可以重新開始一輪新的截圖與
        框選。
        """
        self.snipped_images.clear()
        self.preview_label.config(
            image="",
            text="[ No Image ]"
        )
        self.update_status()
        self.result_text.delete(1.0, tk.END)

    # ==========================================================
    # 開始推論
    # ==========================================================
    def run_inference_thread(self):
        """
        「Run Inference」按鈕的 callback：啟動背景執行緒呼叫模型
        推論，避免呼叫 Ollama 期間卡住 Tkinter 主執行緒導致視窗
        凍結無回應。

        若 self.snipped_images 還是空的（使用者還沒框選任何一張
        圖），跳出警告訊息並直接 return，不會送出空的推論請求。

        否則先清空 result_text 並顯示「Gemma 4 Thinking...」的暫時
        提示，讓使用者立即得到已經開始處理的視覺回饋，接著用
        threading.Thread 建立一個 daemon 執行緒去執行
        send_to_gemma()；daemon=True 確保就算這個背景執行緒還在跑，
        也不會阻擋整個應用程式關閉。
        """

        if not self.snipped_images:
            messagebox.showwarning(
                "Warning",
                "請先加入圖片"
            )
            return

        self.result_text.delete(1.0, tk.END)

        self.result_text.insert(
            tk.END,
            "Gemma 4 Thinking...\n"
        )

        threading.Thread(
            target=self.send_to_gemma,
            daemon=True
        ).start()

    # ==========================================================
    # 傳送多張圖片給 Gemma
    # ==========================================================
    def send_to_gemma(self):
        """
        在背景執行緒中實際呼叫 Ollama 執行 Gemma 4 視覺模型推論
        （由 run_inference_thread 啟動）。

        把 self.snipped_images 裡目前所有已框選的圖片逐一編碼成 PNG
        位元組（透過記憶體中的 BytesIO，不落地存檔），集中成
        all_images_bytes 清單；讀出 prompt_text 目前的提示詞內容並
        去除前後空白。接著呼叫 ollama.chat()，用單一則 user 訊息
        同時附上所有圖片的方式送出（多圖版本的關鍵：讓模型能一次
        看到所有張圖、綜合比較差異，而不是每張圖各自問一次）。

        推論成功時，透過 self.root.after(0, ...) 把 update_result()
        排程回 Tkinter 主執行緒執行並帶上模型回覆內容——因為 Tkinter
        元件不是執行緒安全的，不能在背景執行緒裡直接操作 UI，必須
        透過 after() 交回主事件迴圈處理。若過程中發生例外（例如
        Ollama 沒有啟動、模型不存在、網路錯誤等），一樣透過
        root.after 把整理過的錯誤訊息顯示在結果區。
        """

        try:

            all_images_bytes = []
            for img in self.snipped_images:
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format='PNG')
                all_images_bytes.append(
                    img_byte_arr.getvalue()
                )
            user_prompt = self.prompt_text.get(
                "1.0",
                tk.END
            ).strip()
            response = ollama.chat(
                model=MODEL_NAME,
                messages=[{
                    'role': 'user',
                    'content': user_prompt,
                    'images': all_images_bytes
                }]
            )
            self.root.after(
                0,
                self.update_result,
                response['message']['content']
            )
        except Exception as e:
            error_msg = (
                f"Error: {str(e)}\n"
                f"Please check Ollama and model."
            )
            self.root.after(
                0,
                self.update_result,
                error_msg
            )

    # ==========================================================
    def update_result(self, text):
        """
        在 Tkinter 主執行緒上更新 Gemma Result 區塊的顯示內容（由
        send_to_gemma 透過 root.after 排程呼叫，而不是被背景執行緒
        直接呼叫）。

        清空 result_text 目前的內容後插入新的 text（可能是模型的
        正常回覆，也可能是錯誤訊息）；因為 result_text 是
        DecodedText，插入時會自動修正內容中殘留、尚未還原的
        Unicode 逸出序列。
        """
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, text)
# ==========================================================
# MAIN
# ==========================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = ScreenshotSniper(root)
    root.mainloop()