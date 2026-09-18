import os
import sys
import subprocess
from faster_whisper import WhisperModel

# 設定路徑與參數
OUTPUT_WAV = "/mnt/c/temp/input.wav" 
MODEL_PATH = "small"
# 請填入你剛剛查到的麥克風名稱
MIC_NAME = "麥克風排列 (適用於數位麥克風的 Intel® 智慧型音效 技術)" 

def record_audio_via_windows():
    print("🎤 啟動 Windows 錄音裝置...", file=sys.stderr)
    
    # 1. 直接使用 Windows 格式的路徑，不要用 /mnt/c/
    # 這樣 ffmpeg.exe 會以 Windows 原生方式存取檔案，避開 WSL 權限限制
    windows_output_path = "C:\\temp\\input.wav"
    
    # 2. 確保目錄在 Windows 端存在 (如果用 Python 建立目錄失敗，請手動在 Windows 建立該資料夾)
    if not os.path.exists("C:\\temp"):
        os.makedirs("C:\\temp", exist_ok=True)
    
    alt_name = r"@device_cm_{33D9A762-90C8-11D0-BD43-00A0C911CE86}\wave_{C0074AF2-0AB4-4BC4-BC79-5E04A3A91914}"
    
    cmd = [
        "/mnt/c/ffmpeg/bin/ffmpeg.exe", 
        "-f", "dshow", 
        "-i", f"audio={alt_name}", 
        "-t", "10", 
        "-y", 
        windows_output_path  # 使用 Windows 原生路徑
    ]
    
    subprocess.run(cmd, check=True)
    print(f"✅ 錄音結束，檔案已存至 {windows_output_path}", file=sys.stderr)

def transcribe_audio(filename=OUTPUT_WAV):
    if not os.path.exists(filename):
        raise FileNotFoundError(f"找不到音訊檔案: {filename}，錄音可能未成功。")
        
    print("🔄 正在載入模型並識別...", file=sys.stderr)
    # 載入 Whisper 模型
    model = WhisperModel(MODEL_PATH, device="cpu", compute_type="int8")
    segments, info = model.transcribe(filename, beam_size=5)
    text = "".join([segment.text for segment in segments])
    return text.strip()

if __name__ == "__main__":
    try:
        # 1. 先錄音
        record_audio_via_windows()
        # 2. 再識別
        text = transcribe_audio()
        
        # 3. 輸出結果
        print(text) 
        with open("stt_output.txt", "w") as f:
            f.write(text)
            
    except Exception as e:
        print(f"🚨 錯誤: {e}", file=sys.stderr)
        sys.exit(1)