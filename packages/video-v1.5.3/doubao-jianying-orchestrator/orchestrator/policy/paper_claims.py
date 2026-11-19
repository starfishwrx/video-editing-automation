"""Bind separately observable paper specifications and demonstrations to the reviewed cut."""
from __future__ import annotations

import re

_NUMBERS = "零一二三四五六七八九"
_SPEC = re.compile(r"(?<![\d零一二两三四五六七八九十百千])([1-9]\d*|[一二两三四五六七八九十百千][零一二两三四五六七八九十百千]*)\s*(层|包|抽)(?!层)")
_RUB = re.compile(r"不(?:易|会|容易)?(?:掉|起)(?:毛絮|絮|毛|屑)")
_WET = re.compile(r"湿水|沾水|浸水|湿了|打湿")
_INTACT = re.compile(r"不(?:易|会|容易)?(?:破|烂|裂|断)")


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


def requirements(text):
    facts = [(m.start(), {"kind": "quantity", "unit": m[2], "value": _number(m[1])})
             for m in _SPEC.finditer(text) if m[1] != "一"]
    rub = _RUB.search(text)
    if rub:
        facts.append((rub.start(), {"kind": "rub_unfold"}))
    wet, intact = _WET.search(text), _INTACT.search(text)
    if wet and intact:
        facts.append((min(wet.start(), intact.start()), {"kind": "wet_intact"}))
    return sorted(facts, key=lambda pair: pair[0])


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


def sampled_fact(requirements, fact, samples):
    anchors = {}
    for clock in ("action_us", "result_us"):
        instant = fact.get(clock)
        if type(instant) is not int or not samples:
            return None, []
        sample = min(samples, key=lambda t: abs(t - instant))
        if abs(sample - instant) > 501:  # Sheet labels show milliseconds.
            return None, []
        anchors[clock] = sample
    bound = {**fact, **anchors}
    return bound, [i for i, requirement in enumerate(requirements) if confirms(requirement, bound)]


def reusable(points, review):
    supported = set(review.get("supported_point_ids") or [])
    for point in points:
        required = [f for _, f in requirements(point["text"])]
        if point["id"] not in supported or not required:
            continue
        proven = set()
        for fact in review.get("claim_checks") or []:
            if fact.get("id") == point["id"] and fact.get("object_matches") is True and fact.get("result_seen") is True:
                _, matches = sampled_fact(required, fact, review.get("frame_times_us") or [])
                proven.update(i for i in matches if required[i]["kind"] == "quantity" or fact.get("action_seen") is True)
        if len(proven) != len(required):
            return False
    return True


def confirms(requirement, fact):
    if requirement["kind"] != "quantity":
        return fact.get("kind") == requirement["kind"] and fact["action_us"] < fact["result_us"]
    if fact.get("kind") == "readable_label":
        text = fact.get("label_text")
        return isinstance(text, str) and any(m[2] == requirement["unit"] and _number(m[1]) == requirement["value"]
                                              for m in _SPEC.finditer(text))
    return (fact.get("kind") == "counted_items" and fact.get("unit") == requirement["unit"]
            and type(fact.get("count")) is int and fact["count"] == requirement["value"])
