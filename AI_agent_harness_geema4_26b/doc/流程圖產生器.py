"""把 doc/*.puml 透過 PlantUML 伺服器轉成 doc/images/*.png。

做法比照 fih_rmf_system/doc/流程圖產生器.py（PlantUML 官方伺服器渲染），差別是 .puml 語法放在同目錄的檔案裡、
一次轉全部，且純標準庫：自己做 `plantuml` 套件那套 deflate + PlantUML base64 編碼，向
https://www.plantuml.com/plantuml/png/<編碼> 取圖，不需要本機 Java／Graphviz，需要網路。
走 https 並帶瀏覽器的 User-Agent：實測公司網路的 http 會被代理要求認證，Python 預設的 UA 會被伺服器回 403。

用法：
  python3 doc/流程圖產生器.py                 # 轉 doc/ 底下全部 .puml
  python3 doc/流程圖產生器.py 某張圖.puml     # 只轉指定檔
"""
import http.client
import os
import shutil
import string
import subprocess
import sys
import urllib.error
import urllib.request
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "images")
SERVER = "https://www.plantuml.com/plantuml/png/"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
# PlantUML 自訂的 64 字元表：0-9、A-Z、a-z、-、_（順序與標準 base64 不同）
_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase + "-_"


def encode(text):
    """PlantUML 文字編碼：UTF-8 → raw deflate → 每 3 bytes 換 4 個自訂字元。"""
    data = zlib.compress(text.encode("utf-8"), 9)[2:-4]   # 去掉 zlib 標頭與 checksum 就是 raw deflate
    out = []
    for i in range(0, len(data), 3):
        chunk = data[i:i + 3].ljust(3, b"\0")
        n = (chunk[0] << 16) | (chunk[1] << 8) | chunk[2]
        out.append(_ALPHABET[(n >> 18) & 63] + _ALPHABET[(n >> 12) & 63] + _ALPHABET[(n >> 6) & 63] + _ALPHABET[n & 63])
    return "".join(out)


def render(text, retries=3):
    """回傳 (PNG bytes, ok)。語法錯誤時伺服器回 HTTP 400 但仍附帶錯誤說明圖，一樣存下來方便看。
    大圖的 chunked 回應偶爾會被中途切斷（IncompleteRead），帶 Connection: close 並重試幾次；
    都不行就退回用 curl（有裝的話）。"""
    url = SERVER + encode(text)
    headers = {"User-Agent": USER_AGENT, "Connection": "close", "Accept": "image/png"}
    last = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as resp:
                return resp.read(), True
        except urllib.error.HTTPError as e:
            body = e.read()
            if body[:4] != b"\x89PNG":   # 不是圖（代理／防火牆的 HTML 頁）就別存成 .png
                raise SystemExit(f"❌ 伺服器回 HTTP {e.code}，不是圖片：{body[:120]!r}")
            return body, False
        except (http.client.IncompleteRead, urllib.error.URLError, TimeoutError) as e:
            last = e
    if shutil.which("curl"):
        r = subprocess.run(["curl", "-sS", "-L", "-A", USER_AGENT, url], capture_output=True, timeout=180)
        if r.returncode == 0 and r.stdout[:4] == b"\x89PNG":
            return r.stdout, True
    raise SystemExit(f"❌ 取圖失敗（重試 {retries} 次）：{last}")


def main(argv):
    names = argv or sorted(f for f in os.listdir(HERE) if f.endswith(".puml"))
    if not names:
        print("doc/ 底下沒有 .puml 檔"); return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    rc = 0
    for name in names:
        path = name if os.path.isabs(name) else os.path.join(HERE, os.path.basename(name))
        text = open(path, encoding="utf-8").read()
        png, ok = render(text)
        out = os.path.join(OUT_DIR, os.path.splitext(os.path.basename(path))[0] + ".png")
        with open(out, "wb") as f:
            f.write(png)
        print(("✅" if ok else "❌ 伺服器回報語法錯誤（圖裡有說明）：") + f" {out}（{len(png)} bytes）")
        rc |= 0 if ok else 1
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
