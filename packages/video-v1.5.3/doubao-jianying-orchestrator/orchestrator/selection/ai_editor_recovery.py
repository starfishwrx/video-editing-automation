"""Bounded selection repair inputs and structured failure routing."""
from __future__ import annotations

import re


class WindowFailure(ValueError):
    def __init__(self, index: int, shot_id: str, reason: str):
        self.index, self.shot_id, self.reason = index, shot_id, reason
        super().__init__(f"AI_EDITOR_WINDOW_INFEASIBLE:{index}:{shot_id}:{reason}")


def failure_fields(message: str, *, stage: str = "semantic_match") -> dict:
    """A request outage is not evidence of a material/semantic gap."""
    text = message.upper()
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
    match = re.match(r"AI_EDITOR_(?:NO_SELECTION|AUDIO_ALLOCATION_INVALID|UNREVIEWED_SELECTION|POINTS_UNCOVERED):(\d+)", str(error))
    return int(match[1]) if match else None


def proposed_window(shot: dict, part: dict, *, max_speed: float = 2.0) -> tuple[int, int] | None:
    """Propose a cut to inspect, never manufacture or clip evidence for it."""
    start = part.get("source_start_us")
    duration = part.get("audio_duration_us")
    if type(start) is not int or type(duration) is not int or duration <= 0:
        return None
    lo, hi = round(float(shot["source_start"]) * 1e6), round(float(shot["source_end"]) * 1e6)
    if not lo <= start < hi:
        return None
    end = min(hi, start + round(duration * max_speed))
    return (start, end) if end > start else None
