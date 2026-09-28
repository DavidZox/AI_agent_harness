"""給決策 AI 用的影像提取：使用者附上的影像（螢幕框選、上傳）＋使用者的說明 → 結構化的文字。

決策 AI（main session）看不到原圖，只看得到這裡產生的文字，要拿它回答使用者、或當成觸發技能的線索
（影像裡的錯誤訊息、機器人編號、容器、topic 可以直接當技能的參數）。所以提取的順序與格式由程式保證
（format=extraction_schema(張數)），不是靠模型自己遵守範本——跟工具回傳的任務導向擷取同一個想法：
  每張影像：文字（逐字）→ 畫面的系統性描述（沒有文字時要完整）→ 明確的個體 → 個體以外的背景
  全部影像：影像之間的關係 → 可以拿去查系統的線索 → 看不清楚的地方 → 針對使用者說明的重點
JSON 的欄位順序就是模型生成的順序：重點（focus）放最後，讓它根據已經提取出來的內容回答，而不是先下結論
再找證據；排版（render_extraction）時才把重點移到最上面，決策 AI 第一眼就知道這些資訊跟使用者要的事有什麼關係。

使用者的說明不是「回答完就好」的問題，它有三個用途：決定關注方向（相關的文字與細節優先、完整地提取）、
告訴視覺模型畫面裡某個東西是什麼（例如「左邊那台是 amr_03」）、補充屬性。使用者給的名稱標〔使用者說明〕，
決策 AI 才分得出哪些是影像裡讀到的、哪些是使用者告訴系統的。
"""
import json
import re

from .inference import DEFAULT_OPTIONS, analyze

NAME_SOURCES = ["影像中的文字", "使用者說明", "外觀判斷"]

# 長度上限由 schema 硬性規定（Ollama 的結構化輸出會照 minItems／maxItems／maxLength 生成）：實測 e4b 遇到沒有文字的
# 影像時，texts 會一直輸出空字串直到 num_predict 用完，JSON 斷掉、七千字的殘骸差點交給決策 AI；背景欄也寫過四百字的
# 重複推論。陣列與字串都有上限，就一定是合法的 JSON。字串上限刻意放寬（正常內容碰不到），只防失控；真的碰到時
# 排版會退回最後一個完整的句子（_prose）。images 的張數固定等於附上的張數，模型不會少描述一張，也不會把一張拆成兩筆。
TEXTS_MAX, ENTITIES_MAX, CLUES_MAX, UNCLEAR_MAX = 40, 20, 12, 8
LIMITS = {"kind": 30, "text": 300, "overview": 600, "name": 60, "details": 250, "background": 200,
          "relations": 300, "clue_kind": 20, "clue_value": 300, "unclear": 200, "focus": 400}


def extraction_schema(n_images):
    """這次呼叫的 JSON schema（format=）：欄位順序＝生成順序；images 剛好 n_images 筆，每個陣列都有上限。"""
    def text(key):
        return {"type": "string", "maxLength": LIMITS[key]}

    def texts(key, max_items):
        return {"type": "array", "items": text(key), "maxItems": max_items}
    entity = {
        "type": "object",
        "properties": {"name": text("name"), "name_from": {"type": "string", "enum": NAME_SOURCES}, "details": text("details")},
        "required": ["name", "name_from", "details"],
    }
    image = {
        "type": "object",
        "properties": {
            "kind": text("kind"),
            "texts": texts("text", TEXTS_MAX),
            "overview": text("overview"),
            "entities": {"type": "array", "items": entity, "maxItems": ENTITIES_MAX},
            "background": text("background"),
        },
        "required": ["kind", "texts", "overview", "entities", "background"],
    }
    clue = {"type": "object", "properties": {"kind": text("clue_kind"), "value": text("clue_value")}, "required": ["kind", "value"]}
    return {
        "type": "object",
        "properties": {
            "images": {"type": "array", "items": image, "minItems": n_images, "maxItems": n_images},
            "relations": text("relations"),
            "clues": {"type": "array", "items": clue, "maxItems": CLUES_MAX},
            "unclear": texts("unclear", UNCLEAR_MAX),
            "focus": text("focus"),
        },
        "required": ["images", "relations", "clues", "unclear", "focus"],
    }


EXTRACTION_SYSTEM_PROMPT = """你是「影像提取器」：把使用者附上的影像（螢幕框選的區域或上傳的圖片）轉成文字，交給另一個負責決策的 AI。那個 AI 看不到影像，只看得到你的提取結果，會拿它回答使用者、或決定要執行哪個技能。這套系統做 AMR／AGV 車隊調度與 ROS2 維運，畫面裡常見機器人編號、站點、任務、容器、ROS topic／node、錯誤訊息、檔案路徑。

images 每張影像一筆，依附上的順序（影像 1、影像 2…），每張依序提取：
1. kind：這是什麼畫面（終端機、網頁介面、監控畫面、地圖、表格、照片…），幾個字。
2. texts：畫面裡看得到的文字，逐字照抄（不翻譯、不改寫、不補字）。一行文字抄成一項；表格的一列抄成一項，欄位之間用「｜」隔開。錯誤訊息、狀態、名稱、編號、數值與單位優先。文字很多（整頁 log、長表格）時，只抄跟使用者說明有關的、錯誤與警告、標題與名稱，最後一項寫「其餘約 N 行未抄錄：<那些行大致是什麼>」。沒有文字就給空陣列 []，不要放空字串。
3. overview：畫面的系統性描述。先說整體佈局，再依位置（上→下、左→右）逐一說出畫面裡的每個物體與區塊（包括小的、深色的，不只是使用者提到的）。有文字時一兩句即可；沒有文字時要完整，因為決策 AI 只能靠這一段理解畫面。
4. entities：畫面裡每個明確的個體都列一項（機器人、車輛、人、設備、貨架、箱子或障礙物、視窗、按鈕、圖示…），不要只列跟使用者說明有關的；機器人周圍與行進路線上的物體尤其不要漏。每個寫 name（是什麼）、name_from（名稱從哪來：畫面裡有寫這個名稱就填「影像中的文字」；畫面沒寫、是使用者告訴你的填「使用者說明」；都不是填「外觀判斷」）、details（外觀、顏色、標示、在畫面中的位置、看得出來的狀態，例如燈號亮不亮、有沒有異常；靜態影像看不出是否在移動，不要寫移動中）。沒有明確的個體就給空陣列。
5. background：個體以外的背景與環境（場地、地面、光線；介面所在的應用程式或視窗），一兩句；介面截圖沒有什麼背景可說就給空字串。不要在這裡推論，也不要重複前面寫過的內容。

全部影像做完後：
6. relations：多張影像之間的關係（同一畫面的不同部分、前後的變化、互相對照）；只有一張就給空字串。
7. clues：可以拿去查系統的線索。畫面裡出現的每個機器人／站點／任務編號、容器、topic／node、檔案路徑、網址或 IP 都各列一項，再加上錯誤訊息、關鍵數值、看得出來的異常狀態。每項寫 kind（種類）與 value（照抄原文）；沒有就給空陣列。只列畫面裡真的有的（或使用者說明提到的），不要推測。
8. unclear：看不清楚、被遮住、無法判斷的地方；沒有就給空陣列。不確定的寫在這裡，不要猜。
9. focus：針對使用者說明的重點，一到三句，用第三人稱陳述（不要對使用者說話、不要建議操作）。使用者問了問題就直接回答；使用者提到的狀況（例如「被擋住」「有問題」）要說明畫面裡對應的是哪個個體、看起來怎樣，看不出來就說看不出來；只提示了關注方向或畫面裡的東西，就說在那個方向上看到了什麼；沒有說明就用一句話總結這些影像在呈現什麼。只根據上面提取到的內容，影像裡沒有答案就直說「影像中沒有…」。

使用者說明的用法：它決定你的關注方向——跟它有關的文字、個體、細節要優先而且更完整地提取。說明裡指出畫面中某個東西是什麼（例如「左邊那台是 amr_03」）或補充了屬性時，照使用者說的命名與描述，name_from 填「使用者說明」；使用者的說法跟畫面明顯矛盾時，照畫面寫，並在 unclear 註明。說明裡要系統做的事（例如「幫我派車」）不是給你的，你只負責提取影像內容。

使用繁體中文；名稱、編號、錯誤訊息照畫面原文。每一項盡量精簡，不要寫字數、註記或自我檢查，不要寫建議或下一步。"""

# 結構化輸出失敗（JSON 解析不了）時的退路：同樣的提取順序，改成純文字格式再問一次，
# 決策 AI 拿到的仍是看得懂的一段文字，而不是一段斷掉的 JSON。
FALLBACK_SYSTEM_PROMPT = """你是「影像提取器」：把使用者附上的影像轉成文字，交給另一個看不到影像的決策 AI（這套系統做 AMR／AGV 車隊調度與 ROS2 維運）。
用純文字、繁體中文，依下面的格式輸出，沒有內容的項目整行省略：
重點：<針對使用者說明的一到三句；沒有說明就一句話總結>
影像 1（<這是什麼畫面>）
- 文字（照抄）：<畫面裡的文字逐字照抄，一行一項；錯誤訊息、名稱、編號、數值優先>
- 畫面：<整體佈局，再依位置逐一說出每個物體與區塊；沒有文字時要完整>
- 個體：<每個明確的個體：名稱〔影像中的文字／使用者說明／外觀判斷〕：外觀、位置、看得出來的狀態>
- 背景：<個體以外的環境>
（有多張影像就依序寫影像 2、影像 3…）
可以拿去查的線索（照抄）：<機器人／站點／任務編號、容器、topic、路徑、錯誤訊息，一行一項>
無法判斷：<看不清楚的地方>
使用者說明決定關注方向；它指出畫面裡的東西是什麼時照它命名，並標〔使用者說明〕。不要猜、不要建議下一步。"""

# 結構化輸出比較長；num_predict 是最後一道防線（陣列與字串都有上限，一般碰不到）
EXTRACTION_OPTIONS = {**DEFAULT_OPTIONS, "num_predict": 3072}


def describe_sources(items):
    """VisionSession 的影像項目（{"img", "source"}）→ 給視覺模型的來源說明，例如「螢幕框選的區域（1280×720）」。
    框選的是螢幕的一部分，模型才不會把它當成完整畫面去猜外面的內容；上傳的檔名常帶資訊（amr03_error.png）。"""
    out = []
    for it in items:
        img, source = it.get("img"), str(it.get("source") or "")
        size = f"（{img.width}×{img.height}）" if hasattr(img, "width") else ""
        if source == "screen":
            out.append(f"螢幕框選的區域{size}")
        elif source.startswith("file:"):
            out.append(f"上傳的檔案 {source[len('file:'):]}{size}")
        else:
            out.append(f"影像{size}")
    return out


def build_extraction_prompt(user_text, sources):
    """交給視覺模型的 user 訊息：附上了哪些影像、使用者的說明（沒有說明時明講，模型就照順序完整提取）。"""
    lines = [f"【附上的影像】共 {len(sources)} 張，依附上的順序："]
    lines += [f"- 影像 {i}：{desc}" for i, desc in enumerate(sources, 1)]
    user_text = (user_text or "").strip()
    if user_text:
        lines += ["", "【使用者的說明】（決定你的關注方向；指出畫面裡的東西是什麼、補充屬性時，照它命名與描述）", user_text]
    else:
        lines += ["", "【使用者的說明】沒有：依順序完整提取，focus 用一句話總結這些影像在呈現什麼。"]
    return "\n".join(lines)


_SENTENCE_END_RE = re.compile(r"(?<=[。！？])")
# 模型偶爾在欄位裡寫自我檢查的註記，例如「(總字數：59/60字，符合要求。)」：整段括號拿掉
_META_NOTE_RE = re.compile(r"[（(【{][^（()）【】{}]*?(?:字數|字以內|符合要求|限制字數|已控制|自我檢查)[^（()）【】{}]*?[）)】}]")


def _text(value):
    return " ".join(str(value or "").split())


def _prose(value, limit_key=None):
    """描述性欄位（畫面、個體、背景、重點…）：收空白、拿掉自我檢查的註記、去掉重複的句子——實測小模型偶爾在一個
    欄位裡把同一段推論重複寫兩三次，去重之後內容不變、長度回到正常。寫到 schema 的字串上限（被硬切在半句）時，
    退回最後一個完整的句子；整段沒有句號就原樣保留。逐字照抄的 texts 不經過這裡。"""
    value = _text(value)
    if limit_key and len(value) >= LIMITS[limit_key]:
        cut = max(value.rfind(p) for p in "。！？")
        value = value[:cut + 1] if cut > 0 else value
    sentences = [x.strip() for x in _SENTENCE_END_RE.split(_META_NOTE_RE.sub("", value)) if x.strip()]
    return "".join(dict.fromkeys(sentences))


def _items(value):
    return [v for v in (value if isinstance(value, list) else []) if v not in (None, "", {})]


def render_extraction(data, n_images):
    """extraction_schema 的 JSON → 給決策 AI 與畫面卡片的固定文字區塊。重點放最上面；每張影像依「文字 → 畫面 →
    個體 → 背景」；線索與無法判斷放最後。模型描述的張數跟附上的不一樣時照實註明，不自己補。"""
    out = []
    focus = _prose(data.get("focus"), "focus")
    if focus:
        out.append(f"重點：{focus}")
    images = [x for x in _items(data.get("images")) if isinstance(x, dict)]
    for i, img in enumerate(images, 1):
        kind = _text(img.get("kind"))
        out.append(f"影像 {i}" + (f"（{kind}）" if kind else ""))
        texts = [_text(t) for t in _items(img.get("texts")) if _text(t)]
        if texts:
            out.append("- 文字（照抄）：")
            # 單行寫到 schema 上限時是被硬切的：標明後面沒抄到，決策 AI 才不會把半行當成完整的一行
            out += [f"  「{t}」" + ("（這一行後面未抄錄）" if len(t) >= LIMITS["text"] else "") for t in texts]
        else:
            out.append("- 文字：沒有看到文字")
        overview = _prose(img.get("overview"), "overview")
        if overview:
            out.append(f"- 畫面：{overview}")
        entities = [e for e in _items(img.get("entities")) if isinstance(e, dict) and _text(e.get("name"))]
        if entities:
            out.append("- 個體：")
            for e in entities:
                source = _text(e.get("name_from"))
                details = _prose(e.get("details"), "details")
                out.append(f"  - {_text(e.get('name'))}" + (f"〔{source}〕" if source else "") + (f"：{details}" if details else ""))
        background = _prose(img.get("background"), "background")
        if background:
            out.append(f"- 背景：{background}")
    if len(images) != n_images:
        out.append(f"（附上 {n_images} 張影像，視覺模型描述了 {len(images)} 張）")
    relations = _prose(data.get("relations"), "relations")
    if relations and n_images > 1:
        out.append(f"影像之間：{relations}")
    clues = [c for c in _items(data.get("clues")) if isinstance(c, dict) and _text(c.get("value"))]
    if clues:
        out.append("可以拿去查的線索（照抄）：")
        out += [f"- {_text(c.get('kind')) or '線索'}：{_text(c.get('value'))}" for c in clues]
    unclear = [x for x in (_prose(u, "unclear") for u in _items(data.get("unclear"))) if x]
    if unclear:
        out.append("無法判斷：")
        out += [f"- {u}" for u in unclear]
    return "\n".join(out)


def extract(images, user_text="", sources=None, model=None, timeout=None):
    """對 images（PIL.Image 或 bytes 的列表）做給決策 AI 用的結構化提取。回傳 (text, data)：text 是排好的文字區塊，
    data 是模型的 JSON。JSON 解析不了時用純文字格式再問一次（FALLBACK_SYSTEM_PROMPT），回傳 (那段文字, None)。
    連線、逾時等失敗照樣拋 VisionError。"""
    images = list(images or [])
    sources = list(sources or []) or ["影像"] * len(images)
    prompt = build_extraction_prompt(user_text, sources)
    raw = analyze(images, prompt, model=model, timeout=timeout, system_prompt=EXTRACTION_SYSTEM_PROMPT,
                  options=EXTRACTION_OPTIONS, format=extraction_schema(len(images)))
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        text = render_extraction(data, len(images))
        if text:
            return text, data
    return analyze(images, prompt, model=model, timeout=timeout, system_prompt=FALLBACK_SYSTEM_PROMPT), None
