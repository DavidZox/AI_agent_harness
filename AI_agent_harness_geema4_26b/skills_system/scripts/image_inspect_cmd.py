import os
import sys

# 本腳本由 Agent 以 `python3 scripts/image_inspect_cmd.py` 從 AI 的工作目錄執行，
# 要把 harness 專案根目錄加進 sys.path 才 import 得到 vision library。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from vision import DEFAULT_MODEL, VisionError, analyze, from_file  # noqa: E402

TIMEOUT_SECONDS = 300   # 視覺推論可能較慢；必須低於 Agent_Runner.TOOL_EXEC_TIMEOUT (600)
DEFAULT_PROMPT = "請詳細描述這張影像的內容，並逐字列出可見的文字、數值、狀態指示與任何異常之處。"
USAGE = (
    "用法: scripts/image_inspect_cmd.py <image_path> [prompt]\n"
    "  image_path 為影像檔路徑（相對於目前工作目錄或絕對路徑）；prompt 省略時用預設的描述提示詞。"
)


def execute(image_path, prompt=""):
    image_path = (image_path or "").strip()
    prompt = (prompt or "").strip() or DEFAULT_PROMPT
    if not image_path:
        return f"[ERROR] 請提供影像檔路徑。\n{USAGE}"
    try:
        img = from_file(image_path)
        text = analyze([img], prompt, timeout=TIMEOUT_SECONDS)
    except VisionError as e:
        return f"[ERROR] {e}"
    return (
        f"[PASS] 影像分析結果（{image_path}，{img.width}x{img.height}，模型 {DEFAULT_MODEL}）:\n"
        f"提示詞：{prompt}\n---\n{text}"
    )


if __name__ == "__main__":
    try:
        if len(sys.argv) < 2:
            print(f"[ERROR] 參數不足。\n{USAGE}")
            sys.exit(0)
        print(execute(sys.argv[1], " ".join(sys.argv[2:])))
    except Exception as e:
        print(f"[ERROR] image_inspect 未預期的例外: {e}", file=sys.stderr)
        sys.exit(1)
