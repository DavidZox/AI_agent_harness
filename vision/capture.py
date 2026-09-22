"""螢幕擷取來源：依伺服器所在平台自動選擇後端。

- "powershell"：WSL 底下呼叫 Windows PowerShell（原 sniper 的做法，含 DPI-aware 處理）
- "x11"       ：Linux 桌面（X11，或 Wayland 底下的 XWayland），用 Pillow ImageGrab 截圖、xrandr 列螢幕
- None        ：伺服器端無法擷取。前端（vision/web/snip.js）會改用瀏覽器的 getDisplayMedia
                 讓使用者自己選螢幕，任何作業系統都能用，但頁面需以 http://localhost 或 https 開啟。

注意：伺服器端後端擷取的是「執行 web_console 那台機器」的螢幕；瀏覽器在別台電腦時
請改用瀏覽器端擷取或「選擇檔案」。

兩個公開函式都維持 (result, error_message) 的回傳形式；backend() 回傳目前後端名稱。
"""
import os
import re
import shutil
import subprocess
import tempfile

from PIL import Image

LIST_TIMEOUT_SECONDS = 15
CAPTURE_TIMEOUT_SECONDS = 30

# PowerShell 截圖暫存檔：Windows 端要能寫入，WSL 的任何路徑透過 \\wsl.localhost\<distro>\... 都可存取。
# 放在系統暫存目錄，不會混進專案目錄。可用 VISION_CAPTURE_TEMP 覆寫。
TEMP_FILE = os.environ.get(
    "VISION_CAPTURE_TEMP",
    os.path.join(tempfile.gettempdir(), "vision_fullscreen_capture.png"),
)

NO_BACKEND_MESSAGE = (
    "伺服器端沒有可用的螢幕擷取後端（需要 WSL 的 PowerShell，或 Linux X11 桌面且 Pillow 支援 xcb）；"
    "請改由瀏覽器端擷取，或用「選擇檔案」。"
)


# =========================================================
# 後端偵測
# =========================================================

def _x11_available():
    try:
        from PIL import features
        return bool(features.check("xcb"))
    except Exception:
        return False


def backend():
    """回傳 "powershell" / "x11" / None。每次呼叫重新偵測（很便宜），環境變了不用重啟。"""
    if shutil.which("powershell.exe"):
        return "powershell"
    if os.environ.get("DISPLAY") and _x11_available():
        return "x11"
    return None


def list_screens():
    """列出所有螢幕。回傳 (screens, error)，screens 為 list[dict]：
    index/name/x/y/width/height/primary；失敗時 screens 為 None。"""
    b = backend()
    if b == "powershell":
        return _ps_list_screens()
    if b == "x11":
        return _x11_list_screens()
    return None, NO_BACKEND_MESSAGE


def capture_screen(bounds=None, temp_file=None):
    """擷取螢幕。bounds=(x, y, width, height) 只截該範圔（座標來自 list_screens，可能是負數）；
    不給時擷取整個虛擬桌面。回傳 (PIL.Image RGB, error)。"""
    b = backend()
    if b == "powershell":
        return _ps_capture(bounds, temp_file)
    if b == "x11":
        return _x11_capture(bounds)
    return None, NO_BACKEND_MESSAGE


# =========================================================
# Linux X11 後端
# =========================================================

_GEOM_RE = re.compile(r"(\d+)/\d+x(\d+)/\d+([+-]\d+)([+-]\d+)")


def parse_xrandr_monitors(text):
    """解析 `xrandr --listmonitors` 輸出，例如：
        Monitors: 2
         0: +*HDMI-0 1920/477x1080/268+0+0  HDMI-0
         1: +DP-2 2560/597x1440/336-2560+0  DP-2
    第二欄的 * 代表主螢幕；幾何欄為 W/mmxH/mm+X+Y（X/Y 可為負）。"""
    screens = []
    for line in text.splitlines():
        tokens = line.split()
        if len(tokens) < 3 or not tokens[0].endswith(":"):
            continue
        m = _GEOM_RE.search(tokens[2])
        if not m:
            continue
        w, h, x, y = (int(v) for v in m.groups())
        try:
            index = int(tokens[0][:-1])
        except ValueError:
            continue
        screens.append({
            "index": index, "name": tokens[-1],
            "x": x, "y": y, "width": w, "height": h,
            "primary": "*" in tokens[1],
        })
    return screens


def _x11_list_screens():
    if not shutil.which("xrandr"):
        return None, "找不到 xrandr，無法列出螢幕（仍可擷取整個桌面）"
    try:
        r = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True,
                           check=True, timeout=LIST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None, f"xrandr 超過 {LIST_TIMEOUT_SECONDS} 秒沒有回應"
    except subprocess.CalledProcessError as e:
        return None, f"xrandr 執行失敗（exit code {e.returncode}）: {(e.stderr or '').strip()[-300:]}"
    except Exception as e:
        return None, str(e)
    screens = parse_xrandr_monitors(r.stdout)
    if not screens:
        return None, "xrandr 輸出無法解析"
    return screens, None


def _x11_capture(bounds):
    from PIL import ImageGrab
    bbox = None
    if bounds:
        x, y, w, h = (int(v) for v in bounds)
        bbox = (x, y, x + w, y + h)
    try:
        img = ImageGrab.grab(bbox=bbox, xdisplay=os.environ.get("DISPLAY"))
    except Exception as e:
        return None, f"X11 擷取失敗: {e}（若是 Wayland 桌面請改由瀏覽器端擷取或選擇檔案）"
    return img.convert("RGB"), None


# =========================================================
# WSL → PowerShell 後端（原 sniper 邏輯，加上逾時）
# =========================================================

_DPI_AWARE = (
    "$type = Add-Type -MemberDefinition '[DllImport(\"user32.dll\")] public static extern bool SetProcessDPIAware();' "
    "-Name 'User32' -Namespace 'Win32' -PassThru; "
    "[Win32.User32]::SetProcessDPIAware() | Out-Null; "
)


def _run_powershell(command, timeout, capture_output=True):
    try:
        return subprocess.run(
            ["powershell.exe", "-Command", command],
            capture_output=capture_output,
            stdout=None if capture_output else subprocess.DEVNULL,
            stderr=None if capture_output else subprocess.DEVNULL,
            text=True,
            check=True,
            timeout=timeout,
        ), None
    except FileNotFoundError:
        return None, "找不到 powershell.exe"
    except subprocess.TimeoutExpired:
        return None, f"PowerShell 超過 {timeout} 秒沒有回應，已中止"
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or "").strip() if capture_output else ""
        return None, f"PowerShell 執行失敗（exit code {e.returncode}）" + (f": {detail[-500:]}" if detail else "")
    except Exception as e:
        return None, str(e)


def _ps_list_screens():
    """這裡也要宣告 DPI-aware：沒有宣告的 process 拿到的 Screen.Bounds 是被顯示縮放
    （125%/150%）縮小過的邏輯座標，傳給以物理像素運作的擷取端會只截到左上角一小塊。"""
    ps_command = (
        "[Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
        + _DPI_AWARE +
        "$i = 0; "
        "foreach ($s in [System.Windows.Forms.Screen]::AllScreens) { "
        "Write-Output \"$i|$($s.DeviceName)|$($s.Bounds.X)|$($s.Bounds.Y)|$($s.Bounds.Width)|$($s.Bounds.Height)|$($s.Primary)\"; "
        "$i++; "
        "}"
    )
    result, error = _run_powershell(ps_command, LIST_TIMEOUT_SECONDS)
    if error:
        return None, error
    screens = []
    for line in result.stdout.splitlines():
        parts = line.strip().split("|")
        if len(parts) != 7:
            continue
        idx, name, x, y, w, h, primary = parts
        try:
            screens.append({
                "index": int(idx), "name": name,
                "x": int(x), "y": int(y), "width": int(w), "height": int(h),
                "primary": primary.strip().lower() == "true",
            })
        except ValueError:
            continue
    if not screens:
        return None, "找不到任何螢幕（PowerShell 輸出無法解析）"
    return screens, None


def _windows_path(linux_path):
    try:
        result = subprocess.run(["wslpath", "-w", linux_path], capture_output=True, text=True, check=True, timeout=5)
        return result.stdout.strip()
    except Exception:
        distro = os.environ.get("WSL_DISTRO_NAME", "Ubuntu")
        return (f"\\\\wsl.localhost\\{distro}{linux_path}").replace("/", "\\")


def _ps_capture(bounds, temp_file):
    linux_file_path = temp_file or TEMP_FILE
    if os.path.exists(linux_file_path):
        try:
            os.remove(linux_file_path)
        except Exception:
            pass
    win_temp_path = _windows_path(linux_file_path)

    # 用四個獨立變數而不是建構 Rectangle 依位置傳參，避免 PowerShell 把負數座標誤判成旗標
    if bounds:
        x, y, w, h = bounds
        screen_vars = f"$captureX = {int(x)}; $captureY = {int(y)}; $captureW = {int(w)}; $captureH = {int(h)}; "
    else:
        screen_vars = (
            "$__vs = [System.Windows.Forms.SystemInformation]::VirtualScreen; "
            "$captureX = $__vs.Left; $captureY = $__vs.Top; "
            "$captureW = $__vs.Width; $captureH = $__vs.Height; "
        )
    ps_command = (
        "[Reflection.Assembly]::LoadWithPartialName('System.Drawing') | Out-Null; "
        "[Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null; "
        + _DPI_AWARE + screen_vars +
        "$bmp = New-Object System.Drawing.Bitmap $captureW, $captureH; "
        "$graphics = [System.Drawing.Graphics]::FromImage($bmp); "
        "$graphics.CopyFromScreen($captureX, $captureY, 0, 0, $bmp.Size); "
        f"$bmp.Save('{win_temp_path}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$graphics.Dispose(); $bmp.Dispose();"
    )
    _, error = _run_powershell(ps_command, CAPTURE_TIMEOUT_SECONDS, capture_output=False)
    if error:
        return None, error
    if not os.path.exists(linux_file_path):
        return None, "PowerShell 執行完成，但找不到輸出檔案"
    try:
        with Image.open(linux_file_path) as img:
            return img.convert("RGB"), None
    except Exception as e:
        return None, f"讀取截圖失敗: {e}"
