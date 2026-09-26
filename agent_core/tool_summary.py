"""ToolSummaryMixin：工具回傳的任務導向擷取——summarize_tool_result（大量回傳自動摘要）與 _result_recall（回原文重新提煉），兩者共用 _extract_task_oriented。"""
import json
import ollama
import os
import re
import shlex
from skills_system.scripts import _results_common
from .config import (
    DERIVED_RESULT_SCRIPTS,
    GREP_NO_HIT_RE,
    NUM_CTX,
    RECALL_MAX_RECORDS,
    TOOL_RESULT_TOKEN_THRESHOLD,
    TOOL_SUMMARY_INPUT_MAX_CHARS,
    TOOL_SUMMARY_MAX_CHARS,
    TOOL_SUMMARY_MAX_FOLLOWUP_HOPS,
    TOOL_SUMMARY_MAX_PREDICT,
    TOOL_SUMMARY_TAG,
)
from .protocol import action_text, parse_agent_reply
from .schemas import TOOL_SUMMARY_SCHEMA


class ToolSummaryMixin:

    def _last_assistant_step(self):
        """最近一則 assistant 回覆解析成 {thought, reply, action}：這一步「為什麼執行、執行了什麼」。
        CLI／Web 都在 run_tool 之前就把該則回覆加進 self.messages，所以摘要時最後一則 assistant 就是下這個工具的那一輪。
        給獨立摘要 session 當第二層聚焦依據（第一層是使用者的目標，見 _build_task_anchor_text）；沒有就回 None。"""
        for m in reversed(self.messages):
            if m['role'] != 'assistant':
                continue
            parsed = parse_agent_reply(m['content'])
            if parsed["valid"]:
                return {"thought": parsed["thought"], "reply": parsed["reply"], "action": parsed["action"]}
            return {"thought": "", "reply": parsed["reply"], "action": None}
        return None

    @staticmethod
    def _clip_tool_output(result, max_chars=None):
        """原始輸出超過獨立 session 的輸入上限時保留頭尾（開頭多半是狀態列與統計、結尾是最後狀態），
        中間以說明行取代並告知省略了多少字元。回傳 (text, omitted_chars)。"""
        max_chars = TOOL_SUMMARY_INPUT_MAX_CHARS if max_chars is None else max_chars
        if len(result) <= max_chars:
            return result, 0
        head = int(max_chars * 0.6)
        tail = max_chars - head
        omitted = len(result) - head - tail
        return (f"{result[:head]}\n\n…（原始輸出過長，此處省略中間 {omitted} 字元；以下是結尾部分）…\n\n{result[-tail:]}", omitted)

    @staticmethod
    def _gap_reported(not_covered):
        """not_covered 欄位是否代表「真的有缺口」（而不是模型寫「無」「沒有」這類空值的各種說法）。
        _render_tool_summary 用它決定要不要印「未涵蓋」；summarize_tool_result 的自動追問迴圈用它決定要不要再跳一次。"""
        text = str(not_covered or "").strip()
        return bool(text) and text.rstrip("。.") not in ("無", "沒有", "none", "None", "N/A", "n/a")

    @staticmethod
    def _render_tool_summary(data, result_id=None):
        """把 TOOL_SUMMARY_SCHEMA 的 JSON 排成固定結構的純文字（回答／相關事實／錯誤／未涵蓋／延伸方向）。"""
        if not isinstance(data, dict):
            raise TypeError("tool summary JSON 不是物件")

        def items(key):
            v = data.get(key) or []
            if not isinstance(v, list):
                v = [v]
            return [str(x).strip() for x in v if str(x).strip()]

        lines = [f"回答：{str(data.get('answer') or '').strip() or '（摘要模型沒有給出回答）'}", "相關事實："]
        lines += [f"- {f}" for f in items("facts")] or ["- （原始輸出中沒有與任務直接相關的事實）"]
        errors = items("errors")
        if errors:
            lines.append("錯誤／異常：")
            lines += [f"- {e}" for e in errors]
        not_covered = str(data.get("not_covered") or "").strip()
        if ToolSummaryMixin._gap_reported(not_covered):
            lines.append(f"未涵蓋：{not_covered}")
        related = [r for r in (data.get("related_records") or []) if isinstance(r, dict) and r.get("id")]
        if related:
            lines.append("這個問題可能還需要之前的存檔：")
            lines += [f"- #{r['id']}：{str(r.get('reason') or '').strip() or '（未給理由）'}" for r in related]
        questions = [q for q in (data.get("suggested_questions") or []) if isinstance(q, dict) and str(q.get("question") or "").strip()][:3]
        if questions:
            where = f"（存檔 #{result_id} 原文裡有、上面沒寫）" if result_id else ""
            # 給決策 AI 自己參考的方向，不是要它照抄給使用者看：故意不用「可追問」這種標籤式字眼，並要求改寫成自然的
            # 一句話。keywords 只留給 harness 內部的自動追問用，不給 main session——實測小模型會把「a|b」改寫成
            # 「a OR b」又不加引號，result_grep 直接報錯；追問改由 result_recall 交給獨立 session 回原文提煉。
            lines.append(f"使用者接下來可能還想知道{where}（你自己判斷要不要主動問；要問就用一句自然的話問，"
                         f"不要條列、不要照抄下面的文字）：")
            lines += [f"- {str(q['question']).strip()}" for q in questions]
        return "\n".join(lines)

    def _extract_task_oriented(self, raw_text, purpose_text, tool_tokens, omitted=0, catalog=""):
        """任務導向擷取的核心呼叫（summarize_tool_result 的第一輪、自動追問的每一跳、_result_recall 共用）。
        raw_text 是要讀的原文，purpose_text 是錨點（可以是「使用者目標＋這一步的目的」，也可以是使用者的一句追問——
        呼叫端決定錨在什麼問題上，這裡不管錨點從哪來）。規則要求名稱與數值照抄、不推測、不給建議，並用 not_covered
        明說原始輸出沒有涵蓋什麼。catalog 是工具使用檢索清單（_tool_use_catalog）：獨立 session 看過完整原文後，
        順便判斷使用者的問題還需要清單裡哪幾筆（related_records），給 main session 當 recall 建議。
        結構以 format=TOOL_SUMMARY_SCHEMA 強制。回傳 (data, structured, raw_model_text)：JSON 解析失敗時
        data=None、structured=False，呼叫端可退回 raw_model_text（模型原文，總比丟掉整段輸出好）。"""
        system_prompt = f"""你是一個「資訊過濾器」：把一支工具（腳本）執行後的完整原始輸出，依 user 訊息開頭提供的使用者目標／問題擷取成精簡的重點，交給另一個負責決策的 AI。那個 AI 看不到原始輸出，只看得到你的擷取結果。

規則：
1. 只保留與使用者目標或問題直接相關的事實、數值、名稱、錯誤；樣板文字、排版、重複內容、與任務無關的欄位一律捨棄。
2. 名稱與數值一律照抄原文：topic／node／容器／檔案／路徑／站點／任務 id、數值與單位、錯誤訊息，都不要改寫、四捨五入或概括成「一些」「若干」。
3. 只陳述原始輸出裡有的內容，不要推測、補充背景或給建議；原始輸出沒有的就寫進 not_covered。
4. 使用者的目標／問題若在原始輸出裡找不到答案，answer 要直接寫「輸出中沒有…」，並在 not_covered 說明缺什麼。
5. 原始輸出若標示「省略中間 N 字元」，被省略的部分不可假設，要寫進 not_covered。
6. 數量、排序與門檻判斷以原始輸出裡腳本算好的結果為準（例如「共 N 個」「最新修改：」「條件 …：第一則符合 #k」），直接照抄、不要自己重數或推翻；原始輸出沒有算好而必須比較時，逐一核對原文並在 facts 引用依據（序號、原文數值或時間），無法確定就寫進 not_covered，不要猜。
7. 精簡：answer 一句話（含關鍵數值或名稱）；facts 每項一句、不超過 60 字；全部合計不超過 {TOOL_SUMMARY_MAX_CHARS} 字。
8. suggested_questions：使用者接下來可能想知道、但目標／問題沒問到、且原始輸出裡有資料可答的問題，最多 3 個；每個附 keywords＝在原始輸出裡搜得到的關鍵字（同義詞用 | 分隔，可含正則）。使用者的要求已經明確、輸出也已回答時給空陣列，不要硬湊。
9. index_hint：之後可能是完全不同的一次對話，要靠這一句話判斷「使用者那時問的事跟這份存檔有沒有關」。寫法「<使用者想知道什麼>：<這份原文是什麼、關鍵內容>」，50 字以內（一段英數路徑或檔名算 1 字），名稱照抄原文、放前面。例：「想找記憶相關檔案：skills_system/tools 清單，含 modify_memory.md、result_recall.md」「inference.py 在做什麼：原始碼，視覺推論主流程與 DEFAULT_MODEL 等設定」。不要用「此輸出」「檢視」「了解」開頭，不要重複 answer。
10. related_records：user 訊息若附了【過去的工具使用檢索清單】，判斷使用者現在的目標／問題是否「還需要」清單裡某筆的內容才能答完整（例如要跟之前查過的東西比較、問題提到之前查過的東西、這份輸出缺的正好是那筆有的），是就列出那筆的 id（清單上的數字）與一句理由，最多 2 筆；只是同一種工具或主題相近不算；沒有清單或都不需要就給空陣列（大多數情況是空陣列）。

輸出 JSON 物件：
- answer：一句話直接回答使用者的目標／問題。
- facts：與任務相關的事實清單（名稱、數值照抄）。
- errors：原始輸出中的錯誤／警告／異常，照抄原文；沒有就空陣列。
- not_covered：原始輸出沒有涵蓋、或因篇幅被省略而無法確認的部分；沒有就寫「無」。
- suggested_questions：[{{question, keywords}}] 可追問的問題與搜尋關鍵字，最多 3 個。
- index_hint：50 字以內，「<使用者想知道什麼>：<這份原文有什麼>」，供日後跨對話檢索使用。
- related_records：[{{id, reason}}] 使用者的問題還需要的舊存檔，最多 2 筆，通常是空陣列。
"""
        catalog_block = (
            f"【過去的工具使用檢索清單（只用來填 related_records；不是這份原始輸出的內容，不可寫進 answer／facts）】\n{catalog}\n\n"
            if catalog else ""
        )
        user_prompt = (
            f"{purpose_text}\n\n{catalog_block}"
            f"【工具的完整原始輸出（約 {tool_tokens} tokens{'，過長已保留頭尾' if omitted else ''}）】\n{raw_text}"
        )
        res = ollama.chat(
            model=self.summary_model,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            format=TOOL_SUMMARY_SCHEMA,
            options={'temperature': 0.2, 'num_ctx': NUM_CTX, 'num_predict': TOOL_SUMMARY_MAX_PREDICT},
            think=False,
        )
        raw = res['message']['content'].strip()
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None, False, raw
        return (data, True, raw) if isinstance(data, dict) else (None, False, raw)

    def _extract_with_followups(self, clipped, purpose_text, tool_tokens, omitted, result_id, exclude_ids=()):
        """任務導向擷取＋有界的自動追問（summarize_tool_result 與 _result_recall 共用）：先擷取一次，若 not_covered
        還有缺口、且摘要自己給了 suggested_questions，就拿第一條的 keywords 對同一份存檔（result_id）再 result_grep
        一次（_exec_script 直接跑腳本，不經過 run_tool、不記軌跡、不產生新編號），把 grep 到的原文重新交給
        _extract_task_oriented——永遠錨回 purpose_text，不是拿上一輪的摘要文字當輸入。最多跳
        TOOL_SUMMARY_MAX_FOLLOWUP_HOPS 次；跳滿、grep 落空或沒有 keywords 可用時照實停下，not_covered 該是什麼
        就是什麼，不偽裝成已解決。每一次擷取都附工具使用檢索清單（排除 exclude_ids），最後的 related_records
        只留清單裡真的有的編號（_validate_related）。回傳 (data, structured, raw_model_text)。"""
        catalog, allowed = self._tool_use_catalog(exclude_ids)
        data, structured, raw_model_text = self._extract_task_oriented(clipped, purpose_text, tool_tokens, omitted, catalog)
        if not (structured and data is not None):
            return data, structured, raw_model_text
        self._validate_related(data, allowed)
        hops = 0
        while (
            result_id
            and not data.get("related_records")   # 缺的在別筆存檔：下一步是 recall 那筆，在這份裡 grep 只是白跑
            and self._gap_reported(data.get("not_covered"))
            and data.get("suggested_questions")
            and hops < TOOL_SUMMARY_MAX_FOLLOWUP_HOPS
        ):
            top_q = data["suggested_questions"][0] if isinstance(data["suggested_questions"][0], dict) else {}
            keywords = str(top_q.get("keywords") or "").strip()
            if not keywords:
                break
            grep_script = os.path.join(self.base_path, "scripts", "result_grep_cmd.py")
            grep_out = self._exec_script("result_grep_cmd.py", grep_script, [str(result_id), keywords, "--block"])
            # grep 落空就停：實測以前只擋 [ERROR]，「共 0 行命中」照樣拿去擷取，把第一輪列好的 26 個檔案換成「搜尋命中 0 行」
            if grep_out.lstrip().startswith("[ERROR]") or GREP_NO_HIT_RE.search(grep_out[:400]):
                break
            hops += 1
            print(f"🔁 [自動追問 {hops}/{TOOL_SUMMARY_MAX_FOLLOWUP_HOPS}] 關鍵字「{keywords}」對存檔 #{result_id} 再查一次…")
            grep_clipped, grep_omitted = self._clip_tool_output(grep_out)
            new_data, new_structured, _ = self._extract_task_oriented(
                grep_clipped, purpose_text, self.count_tokens(grep_out), grep_omitted, catalog)
            if not new_structured or new_data is None:
                break
            self._validate_related(new_data, allowed)
            data = self._merge_followup(data, new_data)
        return data, structured, raw_model_text

    @staticmethod
    def _merge_followup(base, hop):
        """自動追問一跳的結果併回第一輪：hop 只讀了 grep 到的片段，回答／未涵蓋／下一個方向用 hop 的（它針對缺口），
        事實、錯誤、related_records 取聯集（不然第一輪讀整份原文得到的事實會被片段蓋掉），index_hint 永遠用第一輪的
        （它描述的是整份存檔，不是 grep 片段）。"""
        merged = dict(hop)
        for key in ("facts", "errors"):
            merged[key] = list(dict.fromkeys(str(x) for x in (base.get(key) or []) + (hop.get(key) or [])))
        related, seen = [], set()
        for r in (base.get("related_records") or []) + (hop.get("related_records") or []):
            if r["id"] not in seen:
                seen.add(r["id"])
                related.append(r)
        merged["related_records"] = related[:2]
        merged["index_hint"] = base.get("index_hint") or hop.get("index_hint")
        return merged

    @staticmethod
    def _validate_related(data, allowed_ids):
        """related_records 只留檢索清單裡真的有的編號（小模型可能編出不存在的編號、或把這一份自己列進去），最多 2 筆；
        就地改寫 data["related_records"] 成 [{"id": int, "reason": str}]。"""
        out, seen = [], set()
        for r in data.get("related_records") or []:
            if not isinstance(r, dict):
                continue
            m = re.search(r"\d+", str(r.get("id") or ""))
            rid = int(m.group()) if m else None
            if rid is None or rid not in allowed_ids or rid in seen:
                continue
            seen.add(rid)
            out.append({"id": rid, "reason": " ".join(str(r.get("reason") or "").split())[:80]})
            if len(out) >= 2:
                break
        data["related_records"] = out
        return out

    def _followup_guidance(self, ids, data, structured, recalled=False):
        """接在任務導向擷取後面、給 main session 的下一步建議——獨立 session 看過完整原文與檢索清單後的判斷，
        依序：還需要之前的存檔 → 把「這一份＋那幾份」一起 recall（同一個獨立 session 才比得了）；有自動追問也解不開
        的缺口 → 自然地問使用者；已經回答 → 直接回答、不必再 recall。追問一律指向 result_recall（使用者的話交給
        獨立 session 回原文提煉），grep 只留給找字串。ids 是這次讀的存檔編號（摘要時只有一個；recall 可以有幾個）。"""
        ids = [str(i) for i in (ids if isinstance(ids, (list, tuple)) else [ids]) if i]
        if not ids:
            return "你看不到原始輸出：需要其他資訊時，換更精確的參數重新執行工具，不要憑空補上。"
        label = "、".join(f"#{i}" for i in ids)
        ok = structured and data is not None
        related = [str(r["id"]) for r in (data.get("related_records") or [])] if ok else []
        new = [r for r in related if r not in ids]
        has_gap = ok and self._gap_reported(data.get("not_covered"))
        has_questions = ok and bool(data.get("suggested_questions"))
        if recalled:
            base = f"不要再對 {label} 重複 recall 或 grep。"
        else:
            base = (f"完整原始輸出已存成 #{ids[0]}：之後使用者追問這份輸出裡上面沒寫到的內容時，執行 "
                    f"result_recall {ids[0]} \"<使用者的話>\" 回原文提煉（只有要找某個字串出現在哪幾行才用 result_grep），"
                    f"不要重新執行同一個工具。")
        if new and len(ids) < RECALL_MAX_RECORDS:
            combo = ",".join((ids + new)[:RECALL_MAX_RECORDS])
            return base + (f"獨立 session 判斷這個問題還需要之前的存檔 {'、'.join('#' + r for r in new)} 一起看才答得完整："
                           f"下一輪直接執行 result_recall {combo} \"<使用者的話>\"（逗號隔開的幾份原文會交給同一個獨立 session "
                           f"一起提煉），拿到後再回答，不要先問使用者、也不要重跑當時的工具。")
        if has_gap and has_questions:
            # 這裡的「未涵蓋」是自動追問已經跳過 TOOL_SUMMARY_MAX_FOLLOWUP_HOPS 次仍解不開的缺口，
            # harness 已經盡力，這時交回使用者選方向比主模型自己硬猜更可靠。
            return base + ("上面的缺口 harness 已經自動再查過還是沒解開：下一輪用 reply 自然地問使用者一句話（不要條列、不要照抄），"
                           "不要自己選一個方向去執行，除非使用者這句話已經對應到其中一個方向。")
        return base + ("上面已經回答了使用者的問題：直接據此回答或做任務的下一步，不需要再 recall；"
                       "後面列的方向只是額外可能有興趣的，順口提一句或略過都可以。")

    def summarize_tool_result(self, result, tool_tokens, step=None, result_id=None):
        """任務導向摘要（Task-Oriented Summarization）：開一個獨立、乾淨的一次性 session（自己的 system/user
        prompt，不接觸 self.messages），把超過門檻的工具回傳擷取成主對話用得上的重點，摘要完就丟棄。

        跟通用摘要的差別在「帶著問題讀原文」——獨立 session 同時收到兩層聚焦依據：
        1. 使用者的目標（_build_task_anchor_text：Objective、使用者最近 3 句原話（任務線）、已核准的計畫）；
        2. 這一步的目的（_last_assistant_step：決策 AI 剛才的 thought／reply 與執行的 action）。
        規則要求名稱與數值照抄、不推測、不給建議，並用 not_covered 明說原始輸出沒有涵蓋什麼，主模型才知道
        該換參數重查而不是憑空補上。結構以 format=TOOL_SUMMARY_SCHEMA 強制，解析失敗退回模型原文。
        原始輸出超過 TOOL_SUMMARY_INPUT_MAX_CHARS 時只讀頭尾（_clip_tool_output），並在給主模型的附註標明。
        step 可由呼叫端指定（測試用），預設取最近一則 assistant 回覆。
        result_id 預設取最近一次 run_tool 的存檔編號，並把 answer 回填到 index.md。獨立 session 同時拿到工具使用
        檢索清單，順便判斷使用者的問題還需要清單裡哪幾筆（related_records）；結尾的下一步建議（_followup_guidance）
        依序是：需要舊存檔 → 先 result_recall 那筆；自動追問也解不開的缺口 → 用自然的一句話問使用者；已回答 →
        直接回答不必 recall；之後的追問一律 result_recall <編號> "<使用者的話>"（延伸方向不再附 grep 關鍵字給主模型）。
        同時把 index_hint（使用者問題 x 這份輸出的關聯，50 字以內）連同檔名記進 tools_use_index.md（見
        _append_tool_use_index；result_* 這類看舊存檔的衍生輸出不記）——這份跟 index.md 不同，永久累加、跨 session
        存活，讓再久以前的工具回傳也有機會被日後的 main session 從 system prompt 尾端的檢索清單裡認出來。

        自動追問（有界、確定性；_extract_with_followups，與 _result_recall 共用）：第一輪擷取後若 not_covered 還有缺口、且摘要自己給了 suggested_questions，
        直接拿第一條的 keywords 對同一份存檔再跑一次 result_grep（_exec_script，不經過 run_tool，不記軌跡、
        不產生新編號——這不是模型的動作，是同一次工具回傳的延伸擷取），把 grep 到的原文重新交給
        _extract_task_oriented（永遠錨回 purpose_text，不是拿上一輪的摘要文字當輸入，避免摘要疊摘要）。
        最多跳 TOOL_SUMMARY_MAX_FOLLOWUP_HOPS 次；跳滿、grep 落空或沒有 keywords 可用時照實停下，
        not_covered 該是什麼就是什麼，不偽裝成已經解決——使用者原始問題若本來就模糊，這裡不會硬鎖定一個
        可能不相關的答案，而是誠實地把缺口留給主模型，逼出使用者下一句話當新錨點。"""
        anchor = self._build_task_anchor_text()
        result_id = self.last_result_id if result_id is None else result_id
        step = step if step is not None else (self._last_assistant_step() or {})
        step_lines = []
        if step.get("action"):
            step_lines.append(f"執行的工具：{action_text(step['action'])}")
        if step.get("thought"):
            step_lines.append(f"決策 AI 執行前的想法：{step['thought']}")
        if step.get("reply"):
            step_lines.append(f"決策 AI 對使用者的說明：{step['reply']}")
        step_text = "\n".join(step_lines) or "（沒有取得這一步的說明，請以使用者的目標為依據）"
        purpose_text = f"【使用者的目標】\n{anchor}\n\n【這一步的目的（決策 AI 為什麼執行這個工具）】\n{step_text}"
        clipped, omitted = self._clip_tool_output(result)

        data, structured, raw_model_text = self._extract_with_followups(
            clipped, purpose_text, tool_tokens, omitted, result_id, exclude_ids=(result_id,))

        answer = ""
        if structured and data is not None:
            body = self._render_tool_summary(data, result_id)
            answer = str(data.get("answer") or "").strip()
        else:
            body = raw_model_text
        self.last_tool_summary = {
            'structured': structured, 'input_tokens': tool_tokens, 'omitted_chars': omitted,
            'summary_tokens': self.count_tokens(body), 'model': self.summary_model, 'result_id': result_id,
        }
        if result_id and answer:
            self.update_result_answer(result_id, answer)
        # 檢索清單只收「原始工具」的結果：result_grep／result_view／result_list／result_recall 是看舊存檔的衍生輸出，
        # 記進去會跟原本那筆重複佔位，而且之後 recall 到它只拿得到部分內容（摘要的摘要），不如 recall 原本那筆。
        script = next((r.get("script") for r in reversed(self.trajectory) if r.get("id") == result_id), None)
        if structured and data is not None and script not in DERIVED_RESULT_SCRIPTS:
            self._append_tool_use_index(result_id, self.last_result_file, data.get("index_hint"))
        status = "失敗（[ERROR]）" if result.lstrip().startswith("[ERROR]") else "成功"
        recall = self._followup_guidance([result_id] if result_id else [], data, structured)
        note = (
            f"（原始輸出約 {tool_tokens} tokens，超過門檻 {TOOL_RESULT_TOKEN_THRESHOLD}；以上由獨立 session 依使用者目標與"
            f"這一步的目的從完整輸出擷取{'，過長部分只讀了頭尾' if omitted else ''}，完整內容已顯示給使用者。{recall}）"
        )
        return f"{TOOL_SUMMARY_TAG}\n執行結果：{status}\n{body}\n{note}"

    def _result_recall(self, remainder):
        """result_recall 偽技能——追問任何存檔內容的主要路徑（檢索清單裡的舊紀錄、或剛剛摘要沒寫到的細節）：main
        session 帶編號＋使用者的問題原文執行；這裡把那份存檔的完整原文重新讀出來，交給獨立 session 以使用者的問題為
        錨點提煉（_extract_with_followups：同一套任務導向擷取＋有界自動追問，逐步收斂；也附檢索清單，讓它判斷還需要
        哪一筆），main session 只收到提煉後的重點與下一步建議。跟 result_grep 的差別是語意由獨立 session 理解，不是
        main session 自己想關鍵字比對字面。錨點用任務線（_build_task_anchor_text：最新一句＋前幾句）——實測使用者
        回答追問時最新一句常只剩「第一個」，模型給的參數只當聚焦點。
        可以一次給多個編號（1,2，最多 RECALL_MAX_RECORDS 份）：問題要一起看幾份存檔時（例如比較 tools 與 scripts 的
        清單），這幾份原文交給同一個獨立 session。實測只能 recall 一份時會來回跳——讀 #1 的 session 看不到 #2，說「還
        需要 #2」；讀 #2 的又說「還需要 #1」，比較永遠做不成。多份時不做自動追問（原文已經都在），正在讀的這幾份也從
        檢索清單排除，不會再被列成「還需要」。這是 main session 主動選的一次動作，照樣記軌跡、產生新的存檔編號；但不記進
        tools_use_index.md（它是被取回那幾筆的衍生輸出，清單留原本的就好）。"""
        try:
            parts = shlex.split(remainder) if remainder.strip() else []
        except ValueError:
            parts = remainder.split()
        if not parts:
            return '[ERROR] 需要指定存檔編號與問題：result_recall <編號>[,<編號>] "<使用者的問題原文>"。'
        ref_arg, question = parts[0], " ".join(parts[1:]).strip()
        if not question:
            return '[ERROR] 需要問題內容：result_recall <編號>[,<編號>] "<使用者的問題原文>"。'

        loaded, missing = [], []
        for ref in [r.strip() for r in re.split(r"[,，、]+", ref_arg) if r.strip()]:
            path, err = _results_common.resolve_result(ref, self.results_dir())
            if err:
                missing.append(ref.lstrip("#"))
                continue
            meta, body_start = _results_common.read_header(path)
            rid = str(meta.get("id") or ref.lstrip("#"))
            if rid in {x[0] for x in loaded}:
                continue
            loaded.append((rid, meta, "\n".join(_results_common.read_lines(path)[body_start - 1:])))
            if len(loaded) >= RECALL_MAX_RECORDS:
                break
        if not loaded:
            return (f"[ERROR] 找不到結果 {'、'.join('#' + m for m in missing) or ref_arg}。這個編號若來自工具使用檢索清單，"
                    f"代表原始檔已超過保留上限被清掉：直接告訴使用者這筆資料已經不在，需要的話重新執行當時的工具，不要換個編號亂試。")
        ids = [rid for rid, _, _ in loaded]
        label = "、".join(f"#{i}" for i in ids)
        per_budget = TOOL_SUMMARY_INPUT_MAX_CHARS // len(loaded)
        chunks, omitted, background = [], 0, []
        for rid, meta, raw in loaded:
            clipped_one, om = self._clip_tool_output(raw, per_budget)
            omitted += om
            chunks.append(clipped_one if len(loaded) == 1 else f"===== 存檔 #{rid}（{meta.get('script') or '?'}）=====\n{clipped_one}")
            hint = self._tool_use_index_hint(rid)
            background.append(f"#{rid}：當時的任務：{meta.get('task') or '(未知)'}；執行的指令：{meta.get('command') or meta.get('script') or '(未知)'}"
                              + (f"；檢索清單對它的描述：{hint}" if hint else ""))
        clipped = "\n\n".join(chunks)
        tool_tokens = sum(self.count_tokens(raw) for _, _, raw in loaded)
        hints = {self._tool_use_index_hint(i) for i in ids}
        # 錨點用 harness 自己記的任務線，不靠模型抄：實測小模型會把清單上的描述抄進參數當問題，而使用者回答追問時
        # 最新一句常只剩「第一個」。模型給的參數只當聚焦點，跟使用者原話或清單描述一樣時就是噪音、不帶。
        user_now = " ".join((self.current_task or "").split())
        focus = question if user_now and question != user_now and question not in hints else ""
        primary = user_now or question
        anchor = self._build_task_anchor_text() if user_now else question
        purpose_text = (
            f"【使用者現在的問題】\n{anchor}\n"
            + (f"【決策 AI 要聚焦的點】\n{focus}\n" if focus else "")
            + f"\n【{'這份' if len(ids) == 1 else '這幾份'}存檔的背景（不是現在要回答的問題）】\n" + "\n".join(background)
        )
        data, structured, raw_model_text = self._extract_with_followups(
            clipped, purpose_text, tool_tokens, omitted, ids[0] if len(ids) == 1 else None, exclude_ids=ids)
        if structured and data is not None:
            body = self._render_tool_summary(data, "、#".join(ids))
        else:
            body = raw_model_text or "（獨立 session 沒有回傳合法結構，請改用 result_grep 自行搜尋關鍵字。）"
        gone = f"（{'、'.join('#' + m for m in missing)} 已經不在：原始檔超過保留上限被清掉，要的話告訴使用者。）\n" if missing else ""
        guidance = self._followup_guidance(ids, data, structured, recalled=True)
        output_text = (f"{TOOL_SUMMARY_TAG}\n重新讀取存檔 {label} 的完整原文，"
                       f"針對「{primary}」{'（聚焦：' + focus + '）' if focus else ''}提煉：\n{gone}{body}\n"
                       f"（以上是獨立 session 回到完整原文、針對使用者的問題提煉的重點。{guidance}）")

        self._record_trajectory(
            "result_recall_cmd.py", [ref_arg, question], output_text,
            self.current_cwd, self.container_cwd, self.target_container,
        )
        return output_text
