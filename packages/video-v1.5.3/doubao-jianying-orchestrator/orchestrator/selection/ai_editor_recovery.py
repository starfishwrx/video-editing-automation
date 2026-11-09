"""Bounded selection repair inputs and structured failure routing."""
from __future__ import annotations

import re


class WindowFailure(ValueError):
    def __init__(self, index: int, shot_id: str, reason: str, part_index: int | None = None):
        self.index, self.shot_id, self.reason, self.part_index = index, shot_id, reason, part_index
        super().__init__(f"AI_EDITOR_WINDOW_INFEASIBLE:{index}:{shot_id}:{reason}")


def repairable_index(error: Exception) -> int | None:
    """Only window conflicts and explicit empty selections can defer another row."""
    if isinstance(error, WindowFailure):
        return error.index
    missing = re.fullmatch(r"AI_EDITOR_NO_SELECTION:(\d+)", str(error))
    return int(missing[1]) if missing else None


def only_outside_sequence_conflicts(segments: list[dict], manifest: dict, members: set[int]) -> bool:
    """Defer only sequence conflicts that can be caused by an outside row.

    The public assembly error reports only the first conflict. Inspect every
    sequence error before deferring it, so a group conflict cannot be hidden.
    """
    from .. import ai_editor_contract as contract
    errors = contract.sequence_errors_all(segments, manifest=manifest, ai_only=True)
    reuse_details = contract.source_reuse_conflict_details(segments, contract.source_reuse_limit(manifest))
    if not errors:
        return False
    for error in errors:
        repeat = re.fullmatch(r"AI_EDITOR_REPEATED_SOURCE_WINDOW:(\d+):(\d+)", error)
        reuse = re.fullmatch(r"AI_EDITOR_SOURCE_REUSE_LIMIT:(\d+)", error)
        if repeat and not members.intersection(map(int, repeat.groups())):
            continue
        if reuse:
            detail = next((d for d in reuse_details if d[0] == error), None)
            if detail and any(i not in members for i in contract.source_reuse_indices(segments, detail[1])):
                continue
        return False
    return True


def failure_fields(message: str, *, stage: str = "semantic_match") -> dict:
    """A request outage is not evidence of a material/semantic gap."""
    text = message.upper()
    if "EXPERIMENT_GROUP_PENDING" in text:
        return {"status": "SHOT_MATCH_BLOCKED", "failure_class": "semantic",
                "failure_code": "EXPERIMENT_GROUP_UNCONFIRMED", "reason": message,
                "retryable": False, "failed_stage": stage, "resume_stage": "semantic_match",
                "next_action": "review_same_experiment_pair_or_phase_budget"}
    if any(code in text for code in ("WINDOW_INFEASIBLE", "KNOWN_WINDOWS_INFEASIBLE",
                                    "PLAYBACK_SPEED_CONFLICT", "PLAYBACK_TIMING_INVALID", "CONTINUITY_")):
        from ..policy import playback
        return {"status": "TIMING_INFEASIBLE", "failure_class": "timing",
                "failure_code": "PLAYBACK_POLICY_CONFLICT", "reason": message,
                "retryable": False, "failed_stage": stage, "resume_stage": "semantic_match",
                "playback_policy_version": playback.VERSION,
                "next_action": "reselect_short_complete_window_or_review_related_group"}
    transient = any(code in text for code in (
        "DEADLINE_EXCEEDED", "REQUEST_TIMEOUT", "CONNECT_TIMEOUT", "TIMED OUT", "TIMEOUT", "EMPTY_CONTENT",
        "CONNECTION RESET", "CONNECTION REFUSED", "429", "502", "503", "TEMPORARILY UNAVAILABLE"))
    blocked = any(code in text for code in (
        "UNAUTHORIZED", "FORBIDDEN", "INVALID_API_KEY", "INVALID API KEY", "CREDENTIAL",
        "INSUFFICIENT BALANCE", "401", "403"))
    if "AI_EDITOR_CALL_FAILED" not in text:
        return {}
    return {"status": "VISION_CALL_FAILED", "failure_class": "vision",
            "failure_code": "VISION_CALL_FAILED", "reason": message,
            "retryable": transient and not blocked, "failed_stage": stage,
            "resume_stage": "semantic_match"}


def failed_index(error: Exception) -> int | None:
    if isinstance(error, WindowFailure):
        return error.index
    repeat = re.fullmatch(r"AI_EDITOR_REPEATED_SOURCE_WINDOW:\d+:(\d+)", str(error))
    if repeat:
        return int(repeat[1])
    reuse = re.fullmatch(r"AI_EDITOR_SOURCE_REUSE_LIMIT:(\d+)", str(error))
    if reuse:
        return int(reuse[1])
    match = re.match(r"AI_EDITOR_(?:NO_SELECTION|AUDIO_ALLOCATION_INVALID|UNREVIEWED_SELECTION|POINTS_UNCOVERED|EXPERIMENT_GROUP_PENDING):(\d+)", str(error))
    return int(match[1]) if match else None


def proposed_window(shot: dict, part: dict, *, max_speed: float = 2.0) -> tuple[int, int] | None:
    """Propose a cut to inspect, never manufacture or clip evidence for it."""
    start = part.get("source_start_us")
    duration = part.get("audio_duration_us")
    if type(start) is not int or type(duration) is not int or duration <= 0:
        return None
    lo, hi = round(float(shot["source_start"]) * 1e6), round(float(shot["source_end"]) * 1e6)
    actual_duration = shot.get("material_duration_us")
    if type(actual_duration) is int and actual_duration > 0:
        hi = min(hi, actual_duration)
    if not lo <= start < hi:
        return None
    end = min(hi, start + round(duration * max_speed))
    return (start, end) if end > start else None
