"""Bind separately observable paper specifications and demonstrations to the reviewed cut."""
from __future__ import annotations

import re

_NUMBERS = "零一二三四五六七八九"
_SPEC = re.compile(r"(?<![\d零一二两三四五六七八九十百千])([1-9]\d*|[一二两三四五六七八九十百千][零一二两三四五六七八九十百千]*)\s*(?:[大小](?=包))?(层|包|抽)(?!层)")
_PERCENT = r"(?:100|[1-9]?\d)(?:\.\d+)?\s*[%％]|百分之?百"
_MATERIAL = r"(?:(?:天然)?(?:原生|再生|原)?(?:木浆|竹浆|木源纤维|植物纤维|竹纤维|棉纤维)|纯棉)"
_COMPOSITION = re.compile(rf"(?<![\d.])(?P<n>{_PERCENT})\s*(?:的)?\s*(?P<m>{_MATERIAL})"
                          rf"|(?P<material>{_MATERIAL})\s*[:：]?\s*(?P<percentage>{_PERCENT})(?!\d)")
_RUB = re.compile(r"不(?:易|会|容易)?(?:掉|起)(?:毛絮|絮|毛|屑)")
_WET = re.compile(r"湿水|沾水|浸水|湿了|打湿")
_DUAL_USE = re.compile(r"干湿两用|干湿都能用|既能干用也能湿用")
_USAGE_QUALIFIER = re.compile(r"(?:不是|不能|并非|不支持|并不支持|不可以|无法|不可|不适合|没有|不具备|"
                              r"(?:包装|标签)(?:上|正面|背面)?(?:只)?(?:标注|标称|标有|写着|印着|印有|写有|显示|注明))"
                              r"[的为是：:‘“「『\"\s]*$")
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


def wet_use_mentions(text):
    return [m for m in _DUAL_USE.finditer(text) if not _USAGE_QUALIFIER.search(text[:m.start()])]


def requirements(text):
    facts = [(m.start(), {"kind": "quantity", "unit": m[2], "value": _number(m[1])})
             for m in _SPEC.finditer(text) if m[1] != "一"]
    for match in _COMPOSITION.finditer(text):
        facts.append((match.start(), _composition_fact(match)))
    facts.extend((m.start(), {"kind": "wet_use"}) for m in wet_use_mentions(text))
    rub = _RUB.search(text)
    if rub:
        facts.append((rub.start(), {"kind": "rub_unfold"}))
    wet, intact = _WET.search(text), _INTACT.search(text)
    if wet and intact:
        facts.append((min(wet.start(), intact.start()), {"kind": "wet_intact"}))
    return sorted(facts, key=lambda pair: pair[0])


def include_spoken_requirements(spoken, points):
    """Retain a specific spoken fact when broad recall classified the line as a promotion."""
    known = [fact for p in points for _, fact in requirements(p["text"])]
    result = list(points)
    used = {p["id"] for p in points}
    facts = requirements(spoken)
    for i, (start, fact) in enumerate(facts):
        if fact in known:
            continue
        end = facts[i + 1][0] if i + 1 < len(facts) else len(spoken)
        key = f"spoken_{start}"
        while key in used:
            key += "_"
        used.add(key)
        known.append(fact)
        result.append({"id": key, "text": spoken[start:end].strip(" ，、,。")})
    return result


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


def bind_sample_times(fact, samples):
    """Resolve a one-based sheet position or an observed microsecond label to that sample."""
    if not samples:
        return None
    anchors = {}
    for stage in ("action", "result"):
        clock = stage + "_us"
        ordinal = stage + "_frame"
        instant = fact.get(clock)
        if ordinal in fact:
            index = fact[ordinal]
            if type(index) is not int or not 1 <= index <= len(samples):
                return None
            sample = samples[index - 1]
        elif type(instant) is int:
            sample = min(samples, key=lambda t: abs(t - instant))
        else:
            return None
        if clock in fact and (type(instant) is not int or abs(sample - instant) > 501):
            return None
        anchors[clock] = sample
    return {**{k: v for k, v in fact.items() if k not in ("action_frame", "result_frame")}, **anchors}


def sampled_fact(requirements, fact, samples, *, basis=None, observation=None):
    bound = bind_sample_times(_observed_label(fact, basis), samples)
    if bound is None:
        return None, []
    return bound, [i for i, requirement in enumerate(requirements)
                   if (requirement["kind"] != "wet_use" or
                       (basis in (None, "visible_result") and wet_contact_observed(observation) is not False))
                   and confirms(requirement, bound)]


def reusable(points, review):
    supported = set(review.get("supported_point_ids") or [])
    for point in points:
        required = [f for _, f in requirements(point["text"])]
        if point["id"] not in supported or not required:
            continue
        if review.get("frame_clock") != "decoded_source_pts":
            return False
        proven = set()
        for fact in review.get("claim_checks") or []:
            if (not isinstance(fact, dict) or fact.get("id") != point["id"] or fact.get("object_matches") is not True
                    or fact.get("result_seen") is not True or not str(fact.get("result_observed") or "").strip()):
                continue
            bound, matches = sampled_fact(required, fact, review.get("frame_times_us") or [],
                                          basis=review.get("basis"), observation=review.get("observation"))
            if bound and bound["action_us"] <= bound["result_us"]:
                proven.update(i for i in matches if required[i]["kind"] in ("quantity", "composition") or fact.get("action_seen") is True)
        if len(proven) != len(required):
            return False
    return True


def action_result(kind, fact):
    result = str(fact.get("result_observed") or "")
    action = str(fact.get("action_observed") or "") + " " + result
    if re.search(r"看不清|无法.{0,5}(?:确认|判断|辨认)|未(?:展示|特写|检查|见到)|没有(?:展示|检查)|看不出|遮挡.{0,7}(?:结果|纸面)", result):
        return False
    negated = r"(?:未曾?|未见|尚未|并未|没有|没|不曾|未能)(?:明显|再次|重新|被|看到|进行|实际)?"
    if fact.get("action_complete") is False or fact.get("result_clear") is False or fact.get("contrary_visible") is True:
        return False
    if kind == "rub_unfold":
        debris = r"(?:毛絮|浮毛|纸屑|碎屑|掉毛|掉屑|起毛|残屑)"
        if (re.search(negated + r"(?:揉|搓|捏|展开|摊开|铺开|打开|展平)", action)
                or any(re.search(r"(?:有|发现|可见|出现|掉出|掉落)(?:明显|少量|零星|细小|大量)?(?:的)?" + debris,
                                 re.sub(r"(?:没有|未见|无|未发现|未出现|不可见)(?:任何|明显|少量|细小|可见|的)*" + debris, "", clause))
                       for clause in re.split(r"[，。；,;]|但是?|然而", result))):
            return False
        return bool(re.search(r"揉|搓|捏", action) and re.search(r"展开|摊开|铺开|打开|展平", result)
                    and re.search(r"(?:没有|无|未见|未出现|未发现|不见|不)[^，。；]{0,7}(?:毛絮|浮毛|纸屑|碎屑|掉毛|掉屑|起毛|残屑)", result))
    if re.search(negated + r"(?:沾水|湿水|浸水|打湿|浸湿|接触水|入水|浸入水|提起|展开)", action):
        return False
    return bool(re.search(r"湿|水中|水盆|水盘|水里", action) and re.search(r"提起|拿起|拉起|捞起|展开|摊开|拉伸|展平", action)
                and re.search(r"完整|完好|无破损|未破|没有破|没破", result)
                and not re.search(r"不完整|并非完整|(?:出现|可见|已经|有了)(?:明显)?(?:破损|破洞|裂口)", result))


def counted(requirement, fact):
    if (fact.get("unit") != requirement["unit"] or type(fact.get("count")) is not int
            or fact["count"] != requirement["value"]):
        return False
    words = str(fact.get("result_observed") or "")
    if _reading_disputed(words):
        return False
    number = r"[零一二两三四五六七八九十百千\d]+"
    totals = re.findall(r"(?:共计|总计|一共|合计|总共|共|总数为)\s*(" + number + r")\s*(?:大|小)?"
                        + re.escape(requirement["unit"]), words)
    if any(_number(value) != fact["count"] for value in totals):
        return False
    group = str(fact.get("group") or "").strip()
    account = group + " " + words
    if (re.search(r"随后|后来|又(?:加|进|放|拿)|另(?:一包|加入)|至少|至多|大约|约有", account)
            or re.search(number + r"\s*[-–—至到]\s*" + number, account)):
        return False
    # Counts in explicit layers can be components of a stated total. A larger
    # number cannot be part of the purported smaller same-frame group.
    items = [_number(m[1]) for m in _SPEC.finditer(account) if m[2] == requirement["unit"]]
    if any(value > fact["count"] for value in items) or (items and not totals and fact["count"] not in items):
        return False
    return bool(group and words.strip() or totals)


def wet_contact_observed(observation):
    """Return True for visible wet use, False for explicit contrary evidence, otherwise None."""
    observation = str(observation or "")
    towel = r"(?:巾体|纸巾|棉柔巾|洁面巾|洗脸巾|毛巾|湿巾|纸张|巾面|纸面|布面|洗面巾)"
    contact = (r"打湿|浸湿|淋湿|沾水|蘸水|遇水|入水|浸水|冲淋|冲洗|冲刷|(?:浸|泡)(?:在|入)?水(?:中|里)|"
               r"放入水|接触(?:到)?水|水龙头下接水|水流.{0,6}(?:淋在|打在|冲在|落在|接触|穿过|积聚在)")
    wipe = r"(?:湿润|打湿|浸湿|沾水|湿的|湿(?:纸|巾|棉柔巾|毛巾)).{0,12}(?:湿擦|擦拭|擦脸|擦手|擦桌|擦洗)"
    seen = False
    only_package = re.search(r"(?:只|仅)(?:看(?:到|见)|展示|拍(?:到|摄)?|有|见)(?:了|到)?[^。；;，,]{0,12}(?:包装|袋面|标签)", observation)
    for clause in re.split(r"[。；;，,]|但是?|然而|随后|然后", observation):
        if re.search(r"包装|标签|字样|印有|写着|图示|示意图|靠近|水龙头旁", clause):
            continue
        for action in re.finditer(contact + "|" + wipe, clause):
            # Negation of damage is compatible with wetting. Negation of wetting is a contrary observation.
            prefix = re.sub(r"(?:没(?:有)?|未|无)(?:出现|见)?(?:破损|破裂|破洞|撕裂|掉絮|掉毛)", "", clause[:action.start()])
            if re.search(r"未|没|并非|不是|无法|不能|不曾|看不清|看不到|不确定|将要|准备|等待|等(?=水|着)|似乎|可能|即将", prefix):
                return False
            if re.search(towel, clause):
                seen = True
    if seen:
        return True
    return False if only_package else None


def confirms(requirement, fact):
    if requirement["kind"] == "wet_use":
        observation = "。".join(str(fact.get(k) or "") for k in ("action_observed", "result_observed"))
        return (fact.get("kind") == "wet_use" and fact["action_us"] <= fact["result_us"]
                and fact.get("action_complete") is not False and fact.get("result_clear") is not False
                and fact.get("contrary_visible") is not True and wet_contact_observed(observation) is True)
    if requirement["kind"] not in {"quantity", "composition"}:
        return (fact.get("kind") == requirement["kind"] and fact["action_us"] < fact["result_us"]
                and action_result(requirement["kind"], fact))
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
    return fact.get("kind") == "counted_items" and counted(requirement, fact)


def output_coverage(spoken, evidence, cuts, *, observed_cut=None):
    """Attribute evidence only to source instants in the corresponding final timeline cuts."""
    required = [fact for _, fact in requirements(spoken)]
    # Mixed sensory and specification copy still permits one genuinely observed point.
    features = {"soft_drape": r"柔软|柔韧|绵软", "embossing": r"压花|压纹", "stretch": r"有韧性|抗拉扯"}
    if required:
        required += [{"kind": "visible_feature", "feature": feature} for feature, pattern in features.items()
                     if re.search(pattern, spoken)]
    if not isinstance(evidence, dict) or not isinstance(evidence.get("frames"), list):
        return [], required
    frames = evidence["frames"]
    observed = evidence.get("facts")
    if not isinstance(observed, list):
        return [], required
    verified = []
    for item in observed:
        if not isinstance(item, dict) or item.get("object_matches") is not True:
            continue
        a, b = item.get("action_frame"), item.get("result_frame")
        if not all(type(n) is int and 1 <= n <= len(frames) for n in (a, b)) or a > b:
            continue
        first, last = frames[a - 1], frames[b - 1]
        if not all(isinstance(f, list) and len(f) == 2 and all(type(i) is int for i in f)
                   and f[0] in cuts and cuts[f[0]][0] <= f[1] < cuts[f[0]][1] for f in (first, last)):
            continue
        if first[0] == last[0]:
            if first[1] > last[1] or (a != b and first[1] == last[1]):
                continue
        elif list(cuts).index(last[0]) != list(cuts).index(first[0]) + 1 or item.get("same_demonstration") is not True:
            continue
        # Use the ordered frame positions here; source clocks may differ across cuts.
        bound = {**item, "action_us": a, "result_us": b}
        for req in required:
            if req["kind"] == "visible_feature":
                pattern = {"soft_drape": r"轻捏|弯折|自然垂|按压回弹", "embossing": r"凹凸|压纹|压花",
                           "stretch": r"拉伸|拉扯|回弹"}[req["feature"]]
                observation = str(item.get("result_observed") or "")
                supported = (item.get("kind") == "visible_feature" and item.get("feature") == req["feature"]
                             and item.get("result_clear") is True and re.search(pattern, observation)
                             and not _reading_disputed(observation))
            else:
                supported = (confirms(req, bound) and
                             (req["kind"] != "wet_use" or wet_contact_observed(observed_cut) is not False))
            if req not in verified and supported:
                verified.append(req)
    return verified, [r for r in required if r not in verified]


def count_disputed(covered, observation):
    """Keep physical subgroup counts distinct from later additions and background inventory."""
    for req in covered:
        if req.get("kind") != "quantity" or req["unit"] != "包":
            continue
        other_scope = False
        for clause in re.split(r"[。；;，,]", str(observation or "")):
            if re.search(r"随后|后来|之后|另(?:外|一组)|背景|库存|远处|后排", clause):
                other_scope = True
            if re.search(r"同一时刻|目标组|起初|开头|最初|前景|前排", clause):
                other_scope = False
            if other_scope:
                continue
            counts = [(_number(m[1]), m) for m in _SPEC.finditer(clause) if m[2] == req["unit"]]
            for number, match in counts:
                if number == req["value"]:
                    continue
                if (number < req["value"] and re.search(r"底层|上层|中层|下层|第一排|第二排", clause)
                        and not re.search(r"共|总|合计", clause[:match.start()])):
                    continue
                return True
    return False


def selected_cut_bounds(segment, slots):
    parts = segment.get("span_parts") if segment.get("multi_shot") else [segment]
    if not isinstance(parts, list) or len(parts) != len(slots):
        return {}
    return {slot: (round(part["source_start"] * 1e6), round(part["source_end"] * 1e6))
            for slot, part in zip(slots, parts) if isinstance(part, dict) and
            all(isinstance(part.get(k), (float, int)) for k in ("source_start", "source_end"))}
