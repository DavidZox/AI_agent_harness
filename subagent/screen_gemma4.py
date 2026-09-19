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

        self.root.title("Gemma 4 Vision Multi-Image Sniper")
        self.root.geometry("520x760")

        self.full_screen_img = None

        # ===== 改成多張圖 =====
        self.snipped_images = []

        self.start_x = None
        self.start_y = None

        self.setup_ui()

    def setup_ui(self):

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

        if self.snip_win:
            self.snip_win.grab_release()
            self.snip_win.destroy()

    # ==========================================================
    def on_button_press(self, event):

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
        self.status_label.config(
            text=f"目前已加入圖片數量：{len(self.snipped_images)}"
        )

    # ==========================================================
    def clear_images(self):
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
                }],
                options={'temperature': 0.2, 'num_ctx': 12288},
                think=False,
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
        self.result_text.delete(1.0, tk.END)
        self.result_text.insert(tk.END, text)
# ==========================================================
# MAIN
# ==========================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = ScreenshotSniper(root)
    root.mainloop()