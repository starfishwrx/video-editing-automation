"""Joint pixel observations for a continuous demonstration across sentence boundaries.

This initial contract permits one source at real time. It grants neither a
cross-file pairing nor permission to compress a critical action or its result.
"""
from __future__ import annotations

import math

SCHEMA = "reviewed-sentence-continuity-v1"
MAX_MEMBERS = 3


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
    return group


def member_errors(item, index):
    """Every slice keeps its own observed evidence and the full joint observation."""
    receipt = item.get("ai_selection") or {}
    group = item.get("continuity_group")
    if group is None and receipt.get("continuity_group") is None:
        return (["CONTINUITY_GROUP_MISSING"] if item.get("action_completeness_scope") == "continuous_sentence_group"
                or receipt.get("action_completeness_scope") == "continuous_sentence_group" else [])
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
