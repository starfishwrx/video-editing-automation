"""Recall a reviewed source window, then let the normal placement and QC gates run."""
from __future__ import annotations

import json
import re
import unicodedata

from .. import ai_editor_contract as contract
from ..vision import evidence_store as store


def _terms(text):
    # Retrieval only. Overlap cannot establish a new visual claim.
    words = re.findall(r"[\u3400-\u9fff]+|[A-Za-z0-9]+", text.casefold())
    return {s[i:i+2] for s in words for i in range(max(1, len(s)-1))}


def _canonical(text):
    return re.sub(r"[\s，。！？、,.!?]+", "", str(text)).casefold()


def _equivalent_claim(text):
    # These wording variants preserve the action, object, result and sequence.
    # Unrecognised words and all numeric/environmental qualifiers stay in the key.
    phrase = _canonical(text)
    for before, after in (("纸巾表面", "纸面"), ("重新摊开", "重新展开"),
                          ("不会掉絮", "不掉絮"), ("吸收水分", "吸水"),
                          ("反复揉捏", "反复揉搓"), ("仍然完整", "依然完整")):
        phrase = phrase.replace(before, after)
    return phrase


def _gap(text, facts):
    # Conditions which commonly change the meaning of a demonstration.
    conditions = (("湿水", r"湿水|沾水|浸湿|打湿|遇水|浸水|湿润"),
                  ("揉搓", r"揉搓|揉过|揉完|揉捏"), ("展开", r"展开|摊开"),
                  ("擦脸", r"擦脸|擦拭脸部"), ("计时", r"秒|分钟"))
    return [name for name, pattern in conditions if re.search(pattern, text)
            and not re.search(pattern, facts)]


def _paper_equivalent(text, observed):
    # Closed grammar for a visible sequence. Additional adjectives, quantities,
    # wet conditions and negation cannot pass via a substring match.
    phrase = _canonical(text)
    if not re.fullmatch(r"(?:纸巾|纸张|纸面)?(?:揉过|揉完|揉搓后|揉搓过后|揉过后)"
                        r"(?:再|重新)?(?:展开|摊开)(?:后)?(?:纸面|纸巾|纸张|表面)"
                        r"(?:还是|仍然|仍|依然)?(?:完整|完好)(?:的)?", phrase):
        return False
    # A positive ordered observation, with the same physical object, is required.
    # Reject competing phases or uncertainty instead of token-matching them.
    return bool(re.fullmatch(
        r"(?:双手|手)(?:拿取|拿起|拿着)?(?:一张)?(?:白色)?纸巾(?:进行|反复|用力|进行反复)?"
        r"揉搓(?:、挤压)?(?:动作)?[，,]随后(?:将)?纸巾(?:重新)?展开(?:平铺)?(?:展示)?[，,]"
        r"纸巾表面完整(?:[，,]未见明显毛絮(?:或破损)?)?[。.]?", observed))



def _mapped_claims(answer, facts, points):
    """Admit a text-only paraphrase only when every point maps to a verified old anchor."""
    if not isinstance(answer, dict) or answer.get("entire_sentence_supported") is not True or answer.get("missing") != []:
        return {}
    old = {p["id"]: p for p in facts["claims"]}
    proposed = answer.get("points")
    if not isinstance(proposed, list) or len(proposed) != len(points):
        return {}
    requested = {p["id"] for p in points}
    mapped = {}
    for row in proposed:
        if not isinstance(row, dict):
            return {}
        new, prior = row.get("id"), row.get("prior_point")
        if isinstance(prior, str) and prior not in old:
            # Model responses sometimes return the old label verbatim. Resolve it
            # only when exactly one stored ID has that complete label.
            aliases = [key for key, point in old.items() if point.get("text") == prior]
            prior = aliases[0] if len(aliases) == 1 else None
        if (not isinstance(new, str) or new not in requested or new in mapped
                or not isinstance(prior, str) or prior not in old
                or row.get("relation") != "same_meaning" or row.get("new_condition") is not False
                or row.get("basis") != facts["basis"] or row.get("quote") != facts["observation"]):
            return {}
        mapped[new] = prior
    return mapped


def _eligible(manifest, sentence):
    return (store.database() and sentence.get("selling_points")
            and not any(sentence.get(k) for k in ("qc_feedback","hook_neighbor_context",
                                                    "action_continuation","visual_requirements"))
            and not any(manifest.get(k) for k in ("revision_feedback","shot_notes_general",
                                                   "shot_notes","material_index_sha256")))


# Pure appearance is a presentation claim. Extra specifications, actions, or
# qualified designs must never disappear when the surrounding narration changes.
_LOOK_WORDS = re.compile(r"(?:这款|这个|这种|这|它的|的|包装|外观|颜值|设计|真的|真是|真|"
                         r"也|太|非常|很|好|高|可爱|好看|漂亮|精美|戳我|戳人|了|啦|啊|呀|/)+")
_LOOK_FEATURE = re.compile(r"图案|卡通|印花|外观|配色|设计|(?:粉|白|红|蓝|绿|黄|紫|黑)色")
_LOOK_DOUBT = re.compile(r"不清|看不|看不到|不可见|无法|模糊|遮挡|疑似|可能|未见|未展示|没有|普通款|非联名")


def _information_panel(observation, design):
    """A filmed instruction, barcode or license panel does not establish a style."""
    if "正面" in design and not _LOOK_DOUBT.search(design):
        return False
    text = observation + design
    panel = re.search(r"背面|反面|侧面", text)
    information = re.search(r"说明|使用方法|注意事项|产品信息|条形码|条码|二维码|授权|license", text, re.I)
    return bool(panel and information)


def _look_requirement(text, points):
    point = points[0] if isinstance(points, list) and len(points) == 1 else {}
    if not isinstance(point, dict) or not point.get("id") or not isinstance(point.get("text"), str):
        return None
    spoken = _canonical(unicodedata.normalize("NFKC", str(text)))
    label = _canonical(unicodedata.normalize("NFKC", point["text"]))
    partner_match = re.fullmatch(r"(.+)\(([a-z0-9\u3400-\u9fff]+)联名\)", label)
    partner = ""
    if partner_match:
        label, partner = partner_match.groups()
    if not all(_LOOK_WORDS.fullmatch(v) and re.search(r"可爱|好看|漂亮|精美|戳|颜值", v)
               for v in (spoken, label)):
        return None
    if not re.search(r"颜值|包装|外观|设计", spoken + label):
        return None
    return partner, "可爱" in spoken + label


def appearance_requirement(manifest, sentence, shot=None):
    if (not _eligible(manifest, sentence) or sentence.get("kind") != "selling_point"
            or sentence.get("scene_ids") or sentence.get("experiment_role") not in (None, "other")
            or sentence.get("standalone_durations_us") or sentence.get("shot_value_request")
            or sentence.get("index") in (0, None)):
        return None
    if shot is not None:
        from .ai_editor import qc_rejected_shots
        if shot.get("_evidence_gap") or shot.get("shot_id") in qc_rejected_shots(manifest).get(sentence["index"], ()):
            return None
    if not store.scope(manifest):
        return None
    return _look_requirement(sentence.get("text"), sentence.get("selling_points"))


def appearance_prompt(manifest, source_sha, start_us, end_us, samples):
    """One visual question per SKU, source window, sampler and editorial protocol."""
    identity = {k: manifest.get(k) for k in ("product", "product_id", "product_key", "sku", "brand")}
    return contract.policy_prompt(manifest) + "\n" + json.dumps({
        "question": "packaging-appearance-v1", "product": identity, "source_sha256": source_sha,
        "window_us": [start_us, end_us], "frame_times_us": samples}, ensure_ascii=False, sort_keys=True) + """
只核对同一商品外包装的可见外观。观察包装主体、颜色、图案、风格及清晰可读的联名标称。
逐项区分具体外观、可爱装饰和联名原文。只看见粉色、卡通图案或单个商标不能证明联名。
不要从商品名猜测看见了什么。只见运输箱、手部或背景物时，不作零售包装的外观证据。
返回 JSON 对象 {"same_product":true,"confidence":0.9,"observation":"所见事实","views":[
{"start_us":100000,"end_us":300000,"sample_us":200000,"design":"直接看到的零售包装颜色、图案或设计",
"cute_detail":"直接看到的可爱装饰；没有或看不清则填空字符串","collaboration_text":"明确的联名标称逐字原文；未读到则填空字符串"}]}。
每项只能描述同一时间段上同一包装。start_us、end_us 给出画面中这些特征清楚的最短完整源区间，
sample_us 必须取 frame_times_us 中实际看清它们的一帧。多个可用时间段分别列出。看不清时 views 留空。
如使用动态入画、翻面或放置才看清外观，请按现行规则提供 presentation_observation 并保住动作和落点。
不作功能、数量、材质或擦拭效果判断。"""


def bind_appearance(answer, requirement, point_id, start_us, end_us, samples):
    """Answer only the current appearance question from a source-bound visual read."""
    if (not isinstance(answer, dict) or type(answer.get("same_product")) is not bool
            or type(answer.get("confidence")) not in (float, int)
            or not 0 <= answer["confidence"] <= 1 or not isinstance(answer.get("observation"), str)
            or not answer["observation"].strip() or not isinstance(answer.get("views"), list)):
        raise ValueError("AI_EDITOR_APPEARANCE_RESPONSE_INVALID")
    result = {"verdict":"uncertain", "same_product":answer["same_product"], "confidence":answer["confidence"],
              "basis":"product_display", "observation":answer["observation"], "reason":answer["observation"],
              "time_sensitive":False, "scene_ids":[], "evidence_intervals_us":[], "supported_point_ids":[]}
    if not answer["same_product"] or answer["confidence"] < 0.6:
        return result
    partner, cute = requirement
    possible = []
    for view in answer["views"]:
        if not isinstance(view, dict):
            continue
        a,b,t = (view.get(k) for k in ("start_us","end_us","sample_us"))
        if (any(type(n) is not int for n in (a,b,t)) or not start_us <= a < b <= end_us):
            continue
        if t not in samples and t % 1000 == 0:
            # Contact sheets print seconds to three decimals. Resolve only a
            # unique printed time, then carry the original microsecond anchor.
            printed = [v for v in samples if f"{v / 1e6:.3f}" == f"{t / 1e6:.3f}"]
            if len(printed) == 1:
                t = printed[0]
        if t not in samples or not a <= t <= b:
            continue
        design, style, label = (view.get(k) for k in ("design","cute_detail","collaboration_text"))
        if not all(isinstance(x,str) for x in (design,style,label)) or not design.strip():
            continue
        if not _LOOK_FEATURE.search(design) or _LOOK_DOUBT.search(design) or _information_panel(answer["observation"], design):
            continue
        if cute and (not style.strip() or _LOOK_DOUBT.search(style) or re.search(r"未|无|不|仅", style)):
            continue
        if partner:
            # An aesthetic brief can specify a named package design. Require
            # its readable name plus the identifiable character in this view,
            # or an explicit collaboration mark. Spoken licensing claims do
            # not enter this path.
            named_character = partner in _canonical(style) and re.search(r"图案|卡通|角色|形象|插画|头像", style)
            collaboration = re.search(r"联名|聯名|collab|\s[x×]\s", label, re.I)
            if (partner not in _canonical(label) or _LOOK_DOUBT.search(label)
                    or re.search(r"未|无|并非|不含", label) or not (collaboration or named_character)):
                continue
        possible.append((b-a, a, b, t, "，".join(v for v in (design,style if cute else "",label if partner else "") if v)))
    if possible:
        _,a,b,t,detail = min(possible)
        result.update(verdict="matched", supported_point_ids=[point_id], evidence_intervals_us=[{"start":a,"end":b}],
                      claim_checks=[{"id":point_id,"kind":"static_visible","object_matches":True,"action_seen":True,
                                     "result_seen":True,"action_us":t,"result_us":t,"result_observed":detail}])
        if "presentation_observation" in answer:
            observation = answer["presentation_observation"]
            result["presentation_observation"] = observation
            result["time_sensitive"] = not isinstance(observation, dict) or observation.get("kind") != "static"
    return result


def appearance_review(manifest, sentence, shot, rows, *, dense_sheet=None):
    """Reuse a witnessed design at its original times; selection still places the cut."""
    requirement = appearance_requirement(manifest, sentence, shot)
    if requirement is None or shot.get("_review_focus_window"):
        return None
    partner, cute = requirement
    durations = sentence.get("audio_durations_us") or []
    if not durations or any(type(d) is not int or d <= 0 for d in durations):
        return None
    # Keep explicit failed reads scoped to overlapping reviewed windows.
    blocked = []
    matches = []
    for row in rows:
        f = row["facts"]
        if row["grade"] != "review" or not f.get("completed_dense_review"):
            continue
        observation = f.get("observation") or ""
        lo, hi = row["start_us"], row["end_us"]
        if ((f.get("verdict") == "mismatch" and
                _look_requirement(f.get("claim_context"), sentence["selling_points"]) is not None)
                or (partner and _LOOK_DOUBT.search(observation) and "包装" in observation)):
            if store.proof_available(row):
                blocked.append((lo, hi))
            continue
        if (f.get("verdict") != "matched" or f.get("basis") != "product_display"
                or f.get("same_product") is not True or f.get("time_sensitive") is not False
                or f.get("scene_ids") or type(f.get("confidence")) not in (int, float)
                or not 0.6 <= f["confidence"] <= 1
                or not round(shot["source_start"]*1e6) <= lo < hi <= round(shot["source_end"]*1e6)):
            continue
        old = _look_requirement(f.get("claim_context"), f.get("claims"))
        if old is None or (partner and old[0] != partner):
            continue
        checked = f.get("claim_checks") or []
        if len(checked) != 1 or not isinstance(checked[0], dict):
            continue
        check = checked[0]
        detail = check.get("result_observed") or ""
        if ("包装" not in observation or not _LOOK_FEATURE.search(observation)
                or not detail or _LOOK_DOUBT.search(observation + detail) or _information_panel(observation, detail)
                or not all(check.get(k) is True for k in ("object_matches","action_seen","result_seen"))
                or check.get("id") != f["claims"][0]["id"]
                or (cute and not (old[1] or re.search(r"可爱|卡通", detail)))
                or (partner and not (partner in _canonical(detail) and "联名" in detail
                                    and not re.search(r"未|无|并非|不含", detail)))):
            continue
        intervals = f.get("evidence_intervals_us") or []
        if not intervals or not all(isinstance(i,dict) and type(i.get("start")) is int
                                    and type(i.get("end")) is int and lo <= i["start"] < i["end"] <= hi
                                    for i in intervals):
            continue
        left = min(i["start"] for i in intervals)
        right = max(i["end"] for i in intervals)
        bounds = (check.get("action_us"), check.get("result_us"))
        if (any(type(t) is not int or not any(i["start"] <= t <= i["end"] for i in intervals) for t in bounds)
                or bounds[0] > bounds[1] or right-left > min(durations)):
            continue
        if dense_sheet is not None and store.identity(dense_sheet) != f.get("sheet_sha256"):
            continue
        matches.append((right-left, row["id"], row))
    for _, _, row in sorted(matches, key=lambda item:item[:2]):
        if any(a < row["end_us"] and b > row["start_us"] for a,b in blocked) or not store.proof_available(row):
            continue
        point = sentence["selling_points"][0]["id"]
        result = _review_result(row, sentence["selling_points"], {point:row["facts"]["claims"][0]["id"]}, shot)
        if result:
            result.update(source_sha256=row["sha"], completed_dense_review=True)
            return result
    return None


def inspect(manifest, sentence, shot, *, rows=None, judge=None):
    if not _eligible(manifest, sentence):
        return None, None
    text = sentence["text"]
    best, score = None, 0
    semantic = None
    candidates = list(store.windows(manifest, shot) if rows is None else rows)
    appearance = appearance_review(manifest, sentence, shot, candidates)
    if appearance:
        return appearance, None
    negatives = [(r["start_us"], r["end_us"]) for r in candidates
                 if r["grade"] == "review" and r["facts"].get("completed_dense_review")
                 and r["facts"].get("verdict") == "mismatch"
                 and _canonical(r["facts"].get("claim_context")) == _canonical(text)
                 and store.proof_available(r)]
    for row in candidates:
        f = row["facts"]
        observed = str(f.get("observation") or "")
        words = " ".join([observed, str(f.get("action") or ""), str(f.get("result") or ""),
                          " ".join(f.get("text") or []), *(p["text"] for p in f.get("claims") or [])])
        overlap = len(_terms(text) & _terms(words))
        missing = _gap(text, observed)
        if overlap > score:
            best, score = (row, missing), overlap
        if (row["grade"] not in ("qc","review") or not observed or f.get("verdict") != "matched"
                or f.get("same_product") is not True or missing
                or not set(sentence.get("scene_ids") or []) <= set(f.get("scene_ids") or [])):
            continue
        if any(a < row["end_us"] and b > row["start_us"] for a, b in negatives):
            continue
        if not f.get("evidence_intervals_us") or f.get("basis") not in ("visible_result","readable_label"):
            continue
        if row["grade"] == "review" and not f.get("completed_dense_review"):
            continue
        if not isinstance(f.get("confidence"), (int, float)) or f["confidence"] < .6:
            continue
        if not store.proof_available(row):
            continue
        exact = _equivalent_claim(text) == _equivalent_claim(f.get("claim_context"))
        equivalent = (row["grade"] == "qc" and f.get("basis") == "visible_result"
                      and _paper_equivalent(text, observed))
        claims = {_equivalent_claim(p["text"]): p["id"] for p in f.get("claims") or []}
        points = sentence["selling_points"]
        direct = (exact or equivalent) and all(_equivalent_claim(p["text"]) in claims or
                  (equivalent and _paper_equivalent(p["text"], observed)) for p in points)
        mapped = {p["id"]: claims.get(_equivalent_claim(p["text"])) for p in points}
        if not direct:
            if not judge or overlap < 2 or not f.get("claims"):
                continue
            if semantic is None or overlap > semantic[0]:
                semantic = (overlap, row)
            continue
        return_value = _review_result(row, points, mapped, shot)
        if return_value is not None:
            return return_value, None
    if semantic:
        row = semantic[1]
        f = row["facts"]
        answer = judge(row, f["observation"], sentence)
        mapped = _mapped_claims(answer, f, sentence["selling_points"])
        if mapped:
            result = _review_result(row, sentence["selling_points"], mapped, shot)
            if result:
                return result, None
        gaps = answer.get("missing") if isinstance(answer, dict) else None
        if isinstance(gaps, list) and gaps and all(isinstance(g, str) for g in gaps):
            best = (row, gaps)
    return None, best


def _review_result(row, points, mapped, shot):
    f = row["facts"]
    observed = f["observation"]
    window = {"start":row["start_us"], "end":row["end_us"]}
    checks = [dict(check, id=p["id"]) for p in points for check in f.get("claim_checks") or []
              if mapped.get(p["id"]) == check.get("id")]
    if row["grade"] == "review" and (not checks or len(checks) != len(points)):
        return None
    proof = {k:f[k] for k in ("basis","observation","scene_ids","time_sensitive","evidence_intervals_us",
                                "presentation_observation","playback_observation","presentation_status",
                                "preferred_speed","speed_range_override")
             if k in f}
    return {**proof, "verdict":"matched", "same_product":True,
            "reason":observed, "confidence":f["confidence"],
            "claim_checks":checks, "supported_point_ids":[p["id"] for p in points],
            "review_window_us":window, "source_identity":contract.source_identity(shot["video"]),
            "shot_id":shot["shot_id"], "frame_times_us":f.get("frame_times_us", []),
            "sheet":f.get("sheet", ""), "dense_required":False,
            "evidence_id":row["id"], "evidence_grade":row["grade"]}


def catalog(manifest, shots):
    if not store.database() or manifest.get("material_index_sha256"):
        return {}
    found = {}
    for shot in shots:
        rows = store.windows(manifest, shot)
        facts = []
        for row in rows:
            f = row["facts"]
            note = (f.get("observation") or " ".join(f.get("text") or [])
                    or "，".join(str(f[k]) for k in ("object","action","result") if f.get(k)))
            if note and len(facts) < 6:
                facts.append({"window":[row["start_us"]/1e6, row["end_us"]/1e6],
                              "observation":note, "basis":f.get("basis", row["grade"])})
        if facts:
            found[shot["shot_id"]] = facts
    return found
