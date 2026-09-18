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
    if not isinstance(text, str):
        return text
    pattern = re.compile(r'\\u([0-9a-fA-F]{4})')
    def replace(match):
        return chr(int(match.group(1), 16))
    return pattern.sub(replace, text)

class DecodedText(tk.Text):
    def insert(self, index, chars, *tags):
        decoded_chars = decode_unicode_str(chars)
        super().insert(index, decoded_chars, *tags)

class ScreenshotSniper:
    def __init__(self, root):
        self.root = root
        self.root.title("Gemma 4 Vision Sniper (WSL2 Double Save)")
        self.root.geometry("480x680")
        
        self.full_screen_img = None  
        self.snipped_image = None    
        self.start_x = None
        self.start_y = None
        
        self.setup_ui()

    def setup_ui(self):
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
        if self.snip_win:
            self.snip_win.grab_release()
            self.snip_win.destroy()

    def on_button_press(self, event):
        self.start_x = event.x
        self.start_y = event.y
        self.rect = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline='red', width=2)

    def on_move_press(self, event):
        cur_x, cur_y = (event.x, event.y)
        if self.rect:
            self.canvas.coords(self.rect, self.start_x, self.start_y, cur_x, cur_y)

    def on_button_release(self, event):
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
        preview_img = img_to_show.copy()
        preview_img.thumbnail((440, 180)) 
        img_tk = ImageTk.PhotoImage(preview_img)
        self.preview_label.config(image=img_tk, text="")
        self.preview_label.image = img_tk

    def run_inference_thread(self):
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, "Gemma 4 Thinking (模型思考中)...\n")
        threading.Thread(target=self.send_to_gemma, daemon=True).start()

    def send_to_gemma(self):
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
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, text)

if __name__ == "__main__":
    root = tk.Tk()
    app = ScreenshotSniper(root)
    root.mainloop()