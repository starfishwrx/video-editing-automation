"""Bind separately observable paper specifications and demonstrations to the reviewed cut."""
from __future__ import annotations

import re

_NUMBERS = "零一二三四五六七八九"
_SPEC = re.compile(r"(?<![\d零一二两三四五六七八九十百千])([1-9]\d*|[一二两三四五六七八九十百千][零一二两三四五六七八九十百千]*)\s*(层|包|抽)(?!层)")
_PERCENT = r"(?:100|[1-9]?\d)(?:\.\d+)?\s*[%％]|百分之?百"
_MATERIAL = r"(?:(?:天然)?(?:原生|再生|原)?(?:木浆|竹浆|植物纤维|竹纤维|棉纤维)|纯棉)"
_COMPOSITION = re.compile(rf"(?<![\d.])(?P<n>{_PERCENT})\s*(?:的)?\s*(?P<m>{_MATERIAL})"
                          rf"|(?P<material>{_MATERIAL})\s*[:：]?\s*(?P<percentage>{_PERCENT})(?!\d)")
_RUB = re.compile(r"不(?:易|会|容易)?(?:掉|起)(?:毛絮|絮|毛|屑)")
_WET = re.compile(r"湿水|沾水|浸水|湿了|打湿")
_INTACT = re.compile(r"不(?:易|会|容易)?(?:破|烂|裂|断)")
_CLEAR_VIEW = re.compile(r"(?:没有|未被|未受|未|毫无|无)(?:被)?(?:手指|手部|手掌)?(?:任何|明显)?(?:遮挡|遮住|挡住)")
_UNCLEAR_READING = re.compile(r"遮挡|遮住|挡住|看不|读不|无法|不能(?:辨|读|确认|看)|不清|模糊|疑似|似乎|猜测|推测|可能|"
                              r"未(?:能|见|读|看|辨|标|写|印|出现|显示|展示)|没(?:有)?(?:能|看|读|标|写|印|出现|显示)|"
                              r"不确定|口播|文案|据称|宣称|应该|应当|如果|假设|建议")
_INEXACT_PREFIX = re.compile(r"(?:并非|非|无|没有|不(?:是|含|到|足|满|超过|超出|采用|使用|多于|少于|足以)|"
                             r"未(?:采用|使用|达到|达|满)|少于|低于|超过|多于|最多|最少|至少|至多|大约|约|[<>≤≥≈])"
                             r"\s*(?:由|含有?|采用|达到|使用|为|足|约|的)?\s*$")
_INEXACT_SUFFIX = re.compile(r"^\s*(?:左右|以内|以[上下]|之内|并不属实)")
_OTHER_TEXT = re.compile(r"^\s*(?:(?:但|而)?(?:其他|其它|其余)(?:产品)?(?:信息|小字|文字|细节|图案)|"
                         r"(?:但|而)?(?:旁边|旁侧)(?:的)?(?:品牌|图案|商标))")


def _number(word):
    if word.isdigit():
        return int(word)
    total = digit = 0
    for char in word.replace("两", "二"):
        if char in _NUMBERS:
            digit = _NUMBERS.index(char)
        else:
            total += (digit or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            digit = 0
    return total + digit


def _composition_fact(match):
    percentage = match["n"] or match["percentage"]
    return {"kind": "composition", "material": match["m"] or match["material"],
            "percent": 100 if percentage.startswith("百分") else float(percentage[:-1].strip())}


def requirements(text):
    facts = [(m.start(), {"kind": "quantity", "unit": m[2], "value": _number(m[1])})
             for m in _SPEC.finditer(text) if m[1] != "一"]
    for match in _COMPOSITION.finditer(text):
        facts.append((match.start(), _composition_fact(match)))
    rub = _RUB.search(text)
    if rub:
        facts.append((rub.start(), {"kind": "rub_unfold"}))
    wet, intact = _WET.search(text), _INTACT.search(text)
    if wet and intact:
        facts.append((min(wet.start(), intact.start()), {"kind": "wet_intact"}))
    return sorted(facts, key=lambda pair: pair[0])


def checks_by_id(points):
    result = {}
    for point in points:
        required = [fact for _, fact in requirements(point["text"])]
        if required:
            result[point["id"]] = required
    return result


def split_points(points):
    """A visible pack count may support one point while sheet count remains uncovered."""
    result = []
    used = {point["id"] for point in points}
    for point in points:
        text = point["text"]
        starts = [start for start, _ in requirements(text)]
        if len(starts) < 2:
            result.append(point)
            continue
        key = point["id"]
        ordinal = 2
        for begin, end in zip([0, *starts[1:]], [*starts[1:], len(text)]):
            if begin:
                key = f"{point['id']}.{ordinal}"
                while key in used:
                    ordinal += 1
                    key = f"{point['id']}.{ordinal}"
                used.add(key)
            result.append({**point, "id": key, "text": text[begin:end].strip(" ，、,。")})
    return result


def _reading_disputed(observation):
    if re.search(r"(?:标称|标签|标注|字样|文字)[^，,。；;]{0,12}(?:属于|来自|出自|是)(?:旁边的?|另外的?)?(?:另|其他|其它|别的)(?:一)?(?:款|件|商品|产品)|"
                 r"(?:纸箱|外箱|包装)(?:的)?(?:原料|材质)[^，,。；;]{0,12}(?:而非|而不是|不是|并非)(?:纸张|纸巾|纸品)", observation):
        return True
    return any(_UNCLEAR_READING.search(_CLEAR_VIEW.sub("", clause)) and not _OTHER_TEXT.search(clause)
               for clause in re.split(r"[，,。；;\n]", observation))


def _exact_at(text, start, end):
    return not _INEXACT_PREFIX.search(text[:start]) and not _INEXACT_SUFFIX.search(text[end:])


def _observed_label(fact, basis):
    """Older responses transcribe the actual label inside the per-point result."""
    if (basis != "readable_label" or fact.get("kind") not in (None, "readable_label")
            or fact.get("label_text") or fact.get("result_seen") is not True):
        return fact
    quote = r'[“‘「『"]([^”’」』"\n]{1,50})[”’」』"]'
    for clause in re.split(r"[。；;\n]", str(fact.get("result_observed") or "")):
        if not re.search(r"包装|纸包|标签|标注", clause) or _reading_disputed(clause):
            continue
        match = (re.search(r"(?:印有|印着|印的是|写着|写有|标有|显示|可见|可读|读到|读出|可辨认|看清)"
                           r"[\s的为是：:]*" + quote, clause)
                 or re.search(quote + r"(?:字样|文字|标注)?(?:处)?[，,\s]*(?:这几个字|字样|文字)?"
                              r"(?:清晰|清楚)(?:可读|可辨认|可见)", clause))
        if match:
            return {**fact, "kind": "readable_label", "label_text": match[1]}
    return fact


def sampled_fact(requirements, fact, samples, *, basis=None):
    anchors = {}
    for clock in ("action_us", "result_us"):
        instant = fact.get(clock)
        if type(instant) is not int or not samples:
            return None, []
        sample = min(samples, key=lambda t: abs(t - instant))
        if abs(sample - instant) > 501:  # Sheet labels show milliseconds.
            return None, []
        anchors[clock] = sample
    bound = {**_observed_label(fact, basis), **anchors}
    return bound, [i for i, requirement in enumerate(requirements) if confirms(requirement, bound)]


def reusable(points, review):
    supported = set(review.get("supported_point_ids") or [])
    for point in points:
        required = [f for _, f in requirements(point["text"])]
        if point["id"] not in supported or not required:
            continue
        proven = set()
        for fact in review.get("claim_checks") or []:
            if (not isinstance(fact, dict) or fact.get("id") != point["id"] or fact.get("object_matches") is not True
                    or fact.get("result_seen") is not True or not str(fact.get("result_observed") or "").strip()):
                continue
            bound, matches = sampled_fact(required, fact, review.get("frame_times_us") or [], basis=review.get("basis"))
            if bound and bound["action_us"] <= bound["result_us"]:
                proven.update(i for i in matches if required[i]["kind"] in ("quantity", "composition") or fact.get("action_seen") is True)
        if len(proven) != len(required):
            return False
    return True


def confirms(requirement, fact):
    if requirement["kind"] not in {"quantity", "composition"}:
        return fact.get("kind") == requirement["kind"] and fact["action_us"] < fact["result_us"]
    if _reading_disputed(str(fact.get("result_observed") or "")):
        return False
    if fact.get("kind") == "readable_label":
        text = fact.get("label_text")
        if not isinstance(text, str):
            return False
        if requirement["kind"] == "composition":
            names = {requirement["material"]}
            if requirement["material"].startswith("原生"):
                names.add("天然" + requirement["material"])
            return any(_exact_at(text, m.start(), m.end()) and _composition_fact(m)["percent"] == requirement["percent"]
                       and (m["m"] or m["material"]) in names for m in _COMPOSITION.finditer(text))
        return any(m[2] == requirement["unit"] and _number(m[1]) == requirement["value"]
                   and _exact_at(text, m.start(), m.end()) for m in _SPEC.finditer(text))
    if requirement["kind"] == "composition":
        return False
    return (fact.get("kind") == "counted_items" and fact.get("unit") == requirement["unit"]
            and type(fact.get("count")) is int and fact["count"] == requirement["value"])
