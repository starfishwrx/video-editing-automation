"""Joint pixel observations for a continuous demonstration across sentence boundaries.

This initial contract permits one source at real time. It grants neither a
cross-file pairing nor permission to compress a critical action or its result.
"""
from __future__ import annotations

import math
import re

SCHEMA = "reviewed-sentence-continuity-v1"
EXPERIMENT_SCHEMA = "reviewed-experiment-sentence-group-v1"
MAX_MEMBERS = 3
# 反渗、干爽、不黏的按压验证只认纸巾。手掌或手指接触不是验证。
_REWET_CLAIM = re.compile(r"反渗|回流|不黏|不粘|干爽|黏腻|粘腻")


# These are observed actions, not a mention of a prop or a proposed test.
# Keep the affirmative sequence in one observation; never assemble it from reasons.
_TISSUE_SEQUENCE = re.compile(
    r"纸巾(?:由手拿着)?[^，,。！？;；手\n]{0,40}?(?:按压|压住|压在|接触)"
    r"[^。！？;；手\n]{0,60}?(?:提起|举起|抬起|拿起|揭起|移开)")
_TISSUE_UNCERTAIN = re.compile(
    r"未|没|并非|不是|不可见|看不到|缺少|缺失|看不清|无法|不能确认|不确定|不清楚|疑似|似乎|可能|或许|"
    r"如果|假如|若|假设|需要|应当|应该|建议|待验证|准备|计划|将要|"
    r"(?:不|无)(?:见|含|存在|使用|用|接触|按压|提起|举起|抬起)|"
    r"\b(?:no|not|never|without|uncertain|unclear|possibly|maybe|might|could|"
    r"would|should|if|need|planned)\b", re.I)
# Negative wetness findings are positive results, not missing action evidence.
_DRY_RESULT = re.compile(r"未被染湿|没有变湿|(?:未见|没有|没见到|未发现)(?:明显(?:的)?)?(?:湿痕|水渍|湿印|蓝色液体|液体回渗|反渗)")
_HAND_LIFT = re.compile(r"(?:提起|举起|抬起|拿起|移开)(?:了)?(?:手掌|手指|手)(?!中)"
                        r"|(?:手掌|手指)(?:随后|然后|再)?(?:提起|举起|抬起|移开)")


def tissue_press_missing(texts, observations) -> bool:
    """Require affirmative observed tissue contact followed by lifting, fail closed.

    Callers supply only visual observation fields. Explanations and claims cannot
    substitute for observed evidence; result/completeness gates still apply.
    """
    if not any(_REWET_CLAIM.search(str(text or "")) for text in texts):
        return False
    return not any(isinstance(item, str) and _TISSUE_SEQUENCE.search(item)
                   and not _TISSUE_UNCERTAIN.search(_DRY_RESULT.sub("干燥", item))
                   and not _HAND_LIFT.search(item)
                   for item in observations)


def adjacent(indices):
    """One ordering rule shared by source phases, sentence groups and previews."""
    return (bool(indices) and len(set(indices)) == len(indices)
            and all(type(i) is int for i in indices)
            and list(indices) == list(range(indices[0], indices[0] + len(indices))))


def source_chain(cuts, matched_pairs=()):
    """A source change/gap needs a reviewed directed pair; overlaps never do."""
    previous = None
    used = {}
    for source, start, end in cuts:
        _require(type(start) is int and type(end) is int and 0 <= start < end, "SOURCE_BOUNDARY")
        _require(not any(max(start, a) < min(end, b) for a, b in used.get(source, [])), "SOURCE_REPLAY")
        if previous:
            path, last = previous
            if source == path:
                _require(start >= last, "SOURCE_TIME_REVERSED")
            _require((source == path and start == last) or (path, source) in matched_pairs,
                     "SOURCE_GAP" if source == path else "EXPERIMENT_PAIR_EVIDENCE_REQUIRED")
        used.setdefault(source, []).append((start, end))
        previous = source, end


def is_experiment(group):
    return isinstance(group, dict) and group.get("schema") == EXPERIMENT_SCHEMA


def _require(condition, reason):
    if not condition:
        raise ValueError("CONTINUITY_" + reason)


def _intervals(intervals, start, end):
    _require(isinstance(intervals, list) and bool(intervals), "EVIDENCE_MISSING")
    for ev in intervals:
        _require(isinstance(ev, dict) and all(type(ev.get(k)) is int for k in ("start", "end"))
                 and start <= ev["start"] < ev["end"] <= end, "EVIDENCE_OUTSIDE_WINDOW")


def validate(group):
    """Validate review completeness and exact allocation; raises on uncertainty."""
    if is_experiment(group):
        return _validate_experiment(group)
    _require(isinstance(group, dict) and group.get("schema") == SCHEMA, "SCHEMA_INVALID")
    context, review = group["input"], group["review"]
    _require(isinstance(context, dict) and isinstance(review, dict), "REVIEW_INVALID")
    members, observations = context["members"], review["members"]
    _require(isinstance(members, list) and isinstance(observations, list)
             and 2 <= len(members) <= MAX_MEMBERS and len(observations) == len(members)
             and all(isinstance(m, dict) for m in members + observations), "MEMBERS_INVALID")
    _require(review.get("verdict") == "matched" and all(review.get(k) is True for k in (
        "same_product", "same_demonstration", "same_selling_point", "semantic_continuity",
        "complete_action", "result_preserved")), "JOINT_REVIEW_UNCERTAIN")
    for key in ("shared_point", "product_observation", "demonstration_observation", "observation", "reason"):
        _require(isinstance(review.get(key), str) and bool(review[key].strip()), "OBSERVATION_MISSING")
    confidence = review.get("confidence")
    _require(type(confidence) in (int, float) and math.isfinite(confidence)
             and .6 <= confidence <= 1, "CONFIDENCE_INVALID")
    start, end = context["window_us"]["start"], context["window_us"]["end"]
    _require(type(start) is int and type(end) is int and 0 <= start < end, "WINDOW_INVALID")
    _require(context["scene_us"]["start"] <= start < end <= context["scene_us"]["end"], "SOURCE_BOUNDARY")
    _intervals(review.get("complete_intervals_us"), start, end)
    cursor, previous = start, None
    for member, observed in zip(members, observations):
        index = member["index"]
        _require(type(index) is int and index > 0 and (previous is None or index == previous + 1), "NOT_ADJACENT")
        _require(member.get("kind") == "selling_point" and member.get("selling_points"), "NOT_SELLING_POINT")
        _require(type(member["audio_duration_us"]) is int and member["audio_duration_us"] > 0
                 and type(member["target_us"]) is int and member["target_us"] >= member["audio_duration_us"], "AUDIO_INVALID")
        _require(member["start_us"] == cursor and member["end_us"] == cursor + member["target_us"], "DISCONTINUOUS")
        _require(observed.get("index") == index and observed.get("verdict") == "matched"
                 and observed.get("same_product") is True and type(observed.get("time_sensitive")) is bool,
                 "MEMBER_UNCERTAIN")
        required = {p["id"] for p in member["selling_points"]}
        supported = observed.get("supported_point_ids")
        _require(isinstance(supported, list) and bool(supported) and set(supported) <= required, "POINT_UNSUPPORTED")
        for key in ("observation", "reason", "phase"):
            _require(isinstance(observed.get(key), str) and bool(observed[key].strip()), "MEMBER_OBSERVATION_MISSING")
        _require(observed.get("basis") in {"editorial_fit", "visible_result", "readable_label", "product_display"}, "BASIS_INVALID")
        _intervals(observed.get("evidence_intervals_us"), cursor, member["end_us"])
        cursor, previous = member["end_us"], index
    _require(cursor == end, "GROUP_DURATION_CHANGED")
    _require(adjacent([m["index"] for m in members]), "NOT_ADJACENT")
    source_chain([(str(context["source_identity"]["path"]), m["start_us"], m["end_us"]) for m in members])
    if context.get("category") == "weipin" and tissue_press_missing(
            [member.get("text") for member in members],
            [review.get(key) for key in ("demonstration_observation", "observation")]
            + [row.get("observation") for row in observations]):
        _require(False, "TISSUE_PRESS_REQUIRED")
    return group


def member_errors(item, index):
    """Every slice keeps its own observed evidence and the full joint observation."""
    receipt = item.get("ai_selection") or {}
    group = item.get("continuity_group")
    if is_experiment(group) or is_experiment(receipt.get("continuity_group")):
        return _experiment_member_errors(item, index)
    if group is None and receipt.get("continuity_group") is None:
        return (["CONTINUITY_GROUP_MISSING"] if item.get("action_completeness_scope") in {
                    "continuous_sentence_group", "paired_experiment_group"}
                or receipt.get("action_completeness_scope") in {
                    "continuous_sentence_group", "paired_experiment_group"} else [])
    try:
        validate(group)
        _require(group == receipt.get("continuity_group"), "RECEIPT_CHANGED")
        context = group["input"]
        member = next(m for m in context["members"] if m["index"] == index)
        observed = next(m for m in group["review"]["members"] if m["index"] == index)
        _require(not item.get("multi_shot") and item.get("text") == member["text"], "MEMBER_CHANGED")
        _require(receipt.get("source_identity") == context["source_identity"]
                 and item.get("shot_id", item.get("temporary_shot_id")) == context["shot_id"], "SOURCE_CHANGED")
        _require(receipt.get("window_us") == {"start": member["start_us"], "end": member["end_us"]}
                 and item.get("audio_duration_us") == member["audio_duration_us"]
                 and item.get("video_speed") == 1, "ALLOCATION_CHANGED")
        # Point IDs express coverage, not sequence; assembly canonicalizes their order.
        # Compare only this field as a set, without rewriting the original observation.
        supported = receipt.get("supported_point_ids")
        _require(isinstance(supported, list)
                 and set(supported) == set(observed["supported_point_ids"]), "MEMBER_OBSERVATION_CHANGED")
        for key in ("evidence_intervals_us", "observation", "reason", "basis", "time_sensitive"):
            _require(receipt.get(key) == observed.get(key), "MEMBER_OBSERVATION_CHANGED")
        _require(item.get("selling_points") == member["selling_points"], "POINTS_CHANGED")
    except (ValueError, KeyError, TypeError, StopIteration, OverflowError) as exc:
        return [str(exc) if str(exc).startswith("CONTINUITY_") else "CONTINUITY_INVALID"]
    return []


def _validate_experiment(group):
    context, review = group["input"], group["review"]
    members, observed = context["members"], review["members"]
    _require(isinstance(members, list) and len(members) >= 2 and adjacent([m["index"] for m in members]),
             "NOT_ADJACENT")
    _require(isinstance(observed, list) and len(observed) == len(members), "MEMBERS_INVALID")
    _require(review.get("verdict") == "matched" and all(review.get(k) is True for k in (
        "same_product", "same_demonstration", "same_selling_point", "semantic_continuity",
        "complete_action", "result_preserved")), "JOINT_REVIEW_UNCERTAIN")
    for key in ("shared_point", "product_observation", "demonstration_observation", "observation", "reason"):
        _require(isinstance(review.get(key), str) and bool(review[key].strip()), "OBSERVATION_MISSING")
    _require(type(review.get("confidence")) in (int, float) and math.isfinite(review["confidence"])
             and .6 <= review["confidence"] <= 1, "CONFIDENCE_INVALID")
    roles = [m.get("experiment_role") for m in members]
    _require(set(roles) <= {"absorption", "rewet", "followup", "bridge"}
             and "absorption" in roles and "rewet" in roles
             and roles.index("absorption") < roles.index("rewet"), "EXPERIMENT_TOPIC_CHANGED")
    sources = context["sources"]
    pair = group["evidence"]["pair"]
    _require(pair.get("observation", {}).get("verdict") == "matched", "EXPERIMENT_PAIR_UNCERTAIN")
    checks = pair["observation"].get("comparisons", {})
    _require(set(checks) == {"product", "setup", "material_state", "verification_correspondence"}
             and all(c.get("status") == "consistent" and c.get("observed") for c in checks.values()),
             "EXPERIMENT_PAIR_UNCERTAIN")
    paired = tuple(m["samples"]["original"]["path"] for m in pair["members"])
    _require(len(paired) == 2, "EXPERIMENT_PAIR_UNCERTAIN")
    chain = []
    for member, row in zip(members, observed):
        _require(type(member["audio_duration_us"]) is int and member["audio_duration_us"] > 0
                 and type(member["target_us"]) is int and member["target_us"] >= member["audio_duration_us"],
                 "AUDIO_INVALID")
        _require(row.get("index") == member["index"] and row.get("verdict") == "matched"
                 and row.get("same_product") is True and type(row.get("time_sensitive")) is bool,
                 "MEMBER_UNCERTAIN")
        required = {p["id"] for p in member["selling_points"]}
        supported = row.get("supported_point_ids")
        _require(isinstance(supported, list) and set(supported) <= required
                 and (bool(supported) or member["kind"] != "selling_point"), "POINT_UNSUPPORTED")
        for key in ("observation", "reason", "phase"):
            _require(isinstance(row.get(key), str) and bool(row[key].strip()), "MEMBER_OBSERVATION_MISSING")
        _require(row.get("basis") in {"editorial_fit", "visible_result", "readable_label", "product_display"},
                 "BASIS_INVALID")
        parts = member["parts"]
        _require(bool(parts) and isinstance(row.get("parts"), list) and len(parts) == len(row["parts"]),
                 "MEMBERS_INVALID")
        _require(sum(p["target_us"] for p in parts) == member["target_us"], "GROUP_DURATION_CHANGED")
        for part, fact in zip(parts, row["parts"]):
            source = sources[part["shot_id"]]
            start, end, target = part["start_us"], part["end_us"], part["target_us"]
            _require(type(start) is int and type(end) is int and type(target) is int and target > 0
                     and source["start_us"] <= start < end <= source["end_us"], "SOURCE_BOUNDARY")
            _require(source["identity"] == part["source_identity"] and source["identity"]["path"] in paired,
                     "SOURCE_CHANGED")
            speed = (end - start) / target
            _require(1 - 2 / target <= speed <= 3 + 2 / target
                     and (part["process"] is True or abs(end - start - target) <= 2)
                     and (not row["time_sensitive"] or abs(end - start - target) <= 2), "ALLOCATION_CHANGED")
            _require(isinstance(fact.get("observation"), str) and bool(fact["observation"].strip()),
                     "MEMBER_OBSERVATION_MISSING")
            _intervals(fact.get("evidence_intervals_us"), start, end)
            chain.append((source["identity"]["path"], start, end))
    source_chain(chain, (paired,))
    if context.get("category") == "weipin" and tissue_press_missing(
            [member.get("text") for member in members],
            [review.get(key) for key in ("demonstration_observation", "observation")]
            + [row.get("observation") for row in observed]
            + [part.get("observation") for row in observed for part in row["parts"]]):
        _require(False, "TISSUE_PRESS_REQUIRED")
    return group


def _experiment_member_errors(item, index):
    receipt = item.get("ai_selection") or {}
    group = item.get("continuity_group")
    try:
        validate(group)
        _require(group == receipt.get("continuity_group"), "RECEIPT_CHANGED")
        member = next(m for m in group["input"]["members"] if m["index"] == index)
        observed = next(m for m in group["review"]["members"] if m["index"] == index)
        _require(item.get("text") == member["text"], "MEMBER_CHANGED")
        if item.get("multi_shot"):
            _require(len(item.get("span_parts") or []) == len(member["parts"]), "MEMBERS_INVALID")
            for part, cut in zip(item["span_parts"], member["parts"]):
                _require((part.get("ai_selection") or {}).get("window_us") ==
                         {"start": cut["start_us"], "end": cut["end_us"]}, "ALLOCATION_CHANGED")
        else:
            matches = [(p, fact) for p, fact in zip(member["parts"], observed["parts"])
                       if receipt.get("shot_id") == p["shot_id"] and receipt.get("window_us") ==
                       {"start": p["start_us"], "end": p["end_us"]}]
            _require(len(matches) == 1, "ALLOCATION_CHANGED")
            cut, fact = matches[0]
            _require(receipt.get("source_identity") == cut["source_identity"]
                     and receipt.get("evidence_intervals_us") == fact["evidence_intervals_us"]
                     and receipt.get("observation") == fact["observation"]
                     and receipt.get("time_sensitive") == observed["time_sensitive"]
                     and set(receipt.get("supported_point_ids") or []) == set(observed["supported_point_ids"])
                     and abs(float(item.get("video_speed", 0)) -
                             (cut["end_us"] - cut["start_us"]) / cut["target_us"]) < 1e-8,
                     "MEMBER_OBSERVATION_CHANGED")
    except (ValueError, KeyError, TypeError, StopIteration, OverflowError) as exc:
        return [str(exc) if str(exc).startswith("CONTINUITY_") else "CONTINUITY_INVALID"]
    return []


def sequence_errors(segments):
    errors = []
    for index, item in enumerate(segments, 1):
        group = item.get("continuity_group")
        errors.extend(member_errors(item, index))
        if not group or errors:
            continue
        for member in group["input"]["members"]:
            target = member["index"] - 1
            if target >= len(segments) or segments[target].get("continuity_group") != group:
                errors.append("CONTINUITY_GROUP_INCOMPLETE")
                break
    return list(dict.fromkeys(errors))
