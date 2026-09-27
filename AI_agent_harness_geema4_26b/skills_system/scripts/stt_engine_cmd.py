import os
import sys
import subprocess

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

# ===== 環境設定（此工具綁定作者的 WSL + Windows 錄音裝置，換環境需修改這一區）=====
RECORD_SECONDS = 10
FFMPEG_PATH = "/mnt/c/ffmpeg/bin/ffmpeg.exe"
WSL_TEMP_DIR = "/mnt/c/temp"                          # WSL 端檢查／建立目錄用
WINDOWS_OUTPUT_PATH = r"C:\temp\input.wav"            # 交給 ffmpeg.exe 的 Windows 原生路徑（避開 WSL 權限問題）
OUTPUT_WAV = os.path.join(WSL_TEMP_DIR, "input.wav")  # WSL 端讀取同一個檔案
MODEL_PATH = "small"
MIC_DEVICE = r"@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{C0074AF2-0AB4-4BC4-BC79-5E04A3A91914}"
# ffmpeg 逾時 = 錄音長度 + 啟動裝置／寫檔的緩衝；裝置不存在或無回應時不會永遠卡住
FFMPEG_TIMEOUT_SECONDS = RECORD_SECONDS + 20


def record_audio_via_windows():
    """錄音。回傳 None 代表成功，否則回傳 [ERROR] 訊息。"""
    if not os.path.exists(FFMPEG_PATH):
        return f"[ERROR] 找不到 ffmpeg.exe: {FFMPEG_PATH}（此工具需要 Windows 端 ffmpeg，請安裝或修改腳本內的 FFMPEG_PATH）。"
    try:
        os.makedirs(WSL_TEMP_DIR, exist_ok=True)
    except OSError as e:
        return f"[ERROR] 無法建立錄音暫存目錄 {WSL_TEMP_DIR}: {e}"

    print(f"🎤 啟動 Windows 錄音裝置，錄音 {RECORD_SECONDS} 秒...", file=sys.stderr)
    cmd = [FFMPEG_PATH, "-f", "dshow", "-i", f"audio={MIC_DEVICE}",
           "-t", str(RECORD_SECONDS), "-y", WINDOWS_OUTPUT_PATH]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return (f"[ERROR] 錄音逾時：ffmpeg 超過 {FFMPEG_TIMEOUT_SECONDS} 秒未結束"
                f"（麥克風裝置可能無回應或名稱錯誤），已強制終止。")
    except Exception as e:
        return f"[ERROR] 啟動 ffmpeg 失敗: {e}"

    if r.returncode != 0:
        tail = "\n".join((r.stderr or "").strip().splitlines()[-8:])
        return f"[ERROR] 錄音失敗（ffmpeg exit code {r.returncode}），請確認麥克風裝置名稱與權限：\n{tail}"
    if not os.path.exists(OUTPUT_WAV):
        return f"[ERROR] ffmpeg 回報成功但在 WSL 端找不到音訊檔 {OUTPUT_WAV}，請確認 C:\\temp 對應到 /mnt/c/temp。"
    print(f"✅ 錄音結束，檔案已存至 {WINDOWS_OUTPUT_PATH}", file=sys.stderr)
    return None


def transcribe_audio(filename=OUTPUT_WAV):
    """語音辨識。回傳 (text, error_message)。

    注意：Whisper 推論在本程序內執行、無法中途中斷，因此這一步沒有自己的逾時，
    只受 Agent_Runner 的 TOOL_EXEC_TIMEOUT（600 秒）總後盾保護。"""
    if WhisperModel is None:
        return None, "[ERROR] 缺少 faster_whisper 套件，無法進行語音辨識（pip install faster-whisper）。"
    if not os.path.exists(filename):
        return None, f"[ERROR] 找不到音訊檔案: {filename}，錄音可能未成功。"

    print("🔄 正在載入模型並識別...", file=sys.stderr)
    try:
        model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8")
        segments, _info = model.transcribe(filename, beam_size=5)
        text = "".join(segment.text for segment in segments).strip()
    except Exception as e:
        return None, f"[ERROR] 語音辨識失敗: {e}"
    return text, None


def execute():
    # 先檢查依賴，不要等錄完 10 秒才發現無法辨識
    if WhisperModel is None:
        return "[ERROR] 缺少 faster_whisper 套件，無法進行語音辨識（pip install faster-whisper）。"

    err = record_audio_via_windows()
    if err:
        return err
    text, err = transcribe_audio()
    if err:
        return err
    if not text:
        return "[PASS] 辨識完成，但沒有偵測到任何語音內容（請確認麥克風有收到聲音）。"

    note = ""
    try:
        with open("stt_output.txt", "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        note = f"\n（附註：無法寫入 stt_output.txt: {e}）"
    return f"[PASS] 語音辨識結果:\n{text}{note}"


if __name__ == "__main__":
    try:
        print(execute())
    except Exception as e:
        print(f"[ERROR] stt_engine 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
