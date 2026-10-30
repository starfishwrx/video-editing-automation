"""Fit one continuous, already-reviewed paper shot around its original-speed parts.

This module does no media I/O and makes no semantic judgments. Callers identify
the effective shot and the action/result intervals which must remain at 1x.
All source intervals are preserved; an infeasible fit returns an explicit reason.
"""
from __future__ import annotations

from fractions import Fraction
import math
from typing import Any

from .timing_contract import frame_us


def _failure(reason: str, **detail: Any) -> dict:
    return {"ok": False, "reason": reason, "parts": [], "detail": detail}


def _integer(value: Any) -> bool:
    return type(value) is int


def _finite(value: Any) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def _protected(values: Any, start: int, end: int) -> list[tuple[int, int]] | None:
    if not isinstance(values, (list, tuple)):
        return None
    intervals = []
    for value in values:
        if isinstance(value, dict):
            left, right = value.get("start"), value.get("end")
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            left, right = value
        else:
            return None
        if not (_integer(left) and _integer(right) and start <= left < right <= end):
            return None
        intervals.append((left, right))
    merged: list[tuple[int, int]] = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged


def _ceil(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _allocate(lengths: list[int], output_us: int, cap: Fraction) -> list[int] | None:
    """Apportion integer microseconds without rounding a part above the cap."""
    minimum = [_ceil(Fraction(length, 1) / cap) for length in lengths]
    if sum(minimum) > output_us or output_us > sum(lengths):
        return None
    total = sum(lengths)
    ideal = [Fraction(length * output_us, total) for length in lengths]
    allocated = [max(low, min(length, value.numerator // value.denominator))
                 for low, length, value in zip(minimum, lengths, ideal)]
    remainder = output_us - sum(allocated)
    # The initial proportional floors differ by fewer than len(lengths)
    # microseconds. Correct that remainder with deterministic largest remainders.
    while remainder:
        if remainder > 0:
            eligible = [i for i, value in enumerate(allocated) if value < lengths[i]]
            index = max(eligible, key=lambda i: (ideal[i] - allocated[i], -i))
            allocated[index] += 1
            remainder -= 1
        else:
            eligible = [i for i, value in enumerate(allocated) if value > minimum[i]]
            index = max(eligible, key=lambda i: (allocated[i] - ideal[i], -i))
            allocated[index] -= 1
            remainder += 1
    return allocated


def plan_retiming(source_start_us: int, source_end_us: int,
                  protected_intervals: list, target_duration_us: int,
                  fps: float = 30, preferred_cap: float = 2.0,
                  hard_cap: float = 3.0) -> dict:
    """Return a continuous fixed-duration fit, or a structured failure.

    Protected intervals use absolute source microseconds as ``{start, end}``
    mappings or two-integer pairs. Overlapping/touching intervals are merged.
    Their duration is unchanged. Remaining phases share the closest feasible
    compression ratio, with at most one microsecond of allocation rounding.
    No slowdown, source truncation, hold frame or source gap is synthesized.
    """
    if not (_integer(source_start_us) and _integer(source_end_us)
            and 0 <= source_start_us < source_end_us):
        return _failure("INVALID_SOURCE_WINDOW")
    if not _integer(target_duration_us) or target_duration_us <= 0:
        return _failure("INVALID_TARGET_DURATION")
    if not _finite(fps) or fps <= 0 or not math.isfinite(1_000_000 / fps):
        return _failure("INVALID_FPS")
    if not (_finite(preferred_cap) and _finite(hard_cap)
            and 1 <= preferred_cap <= 2 and preferred_cap <= hard_cap <= 3):
        return _failure("INVALID_SPEED_LIMIT")
    protected = _protected(protected_intervals, source_start_us, source_end_us)
    if protected is None:
        return _failure("INVALID_PROTECTED_INTERVALS")

    source_us = source_end_us - source_start_us
    protected_us = sum(right - left for left, right in protected)
    if target_duration_us > source_us:
        return _failure("SOURCE_TOO_SHORT", source_us=source_us,
                        target_duration_us=target_duration_us)
    phases = []
    cursor = source_start_us
    for left, right in protected:
        if left > cursor:
            phases.append((cursor, left, False))
        phases.append((left, right, True))
        cursor = right
    if cursor < source_end_us:
        phases.append((cursor, source_end_us, False))
    compressible = [right - left for left, right, locked in phases if not locked]
    budget = target_duration_us - protected_us
    limits = (Fraction(str(preferred_cap)), Fraction(str(hard_cap)))
    minimum_target_us = protected_us + sum(_ceil(Fraction(length, 1) / limits[1])
                                           for length in compressible)
    if not compressible:
        if target_duration_us != source_us:
            return _failure("DURATION_FIT_CONFLICT", cause="NO_COMPRESSIBLE_TIME",
                            protected_us=protected_us, minimum_target_us=minimum_target_us,
                            target_duration_us=target_duration_us)
        allocated, tier = [], "ORIGINAL"
    elif budget <= 0:
        return _failure("DURATION_FIT_CONFLICT", cause="PROTECTED_TIME_EXHAUSTS_TARGET",
                        protected_us=protected_us, minimum_target_us=minimum_target_us,
                        target_duration_us=target_duration_us)
    else:
        allocated = _allocate(compressible, budget, limits[0])
        tier = "ORIGINAL" if target_duration_us == source_us else "PREFERRED"
        if allocated is None:
            allocated = _allocate(compressible, budget, limits[1])
            tier = "EXCEPTION"
        if allocated is None:
            return _failure("DURATION_FIT_CONFLICT", cause="SPEED_CAP_EXCEEDED",
                            protected_us=protected_us, minimum_target_us=minimum_target_us,
                            target_duration_us=target_duration_us)

    parts = []
    output_cursor = 0
    available = iter(allocated)
    for left, right, locked in phases:
        duration = right - left if locked else next(available)
        parts.append({"source_start_us": left, "source_end_us": right,
                      "target_start_us": output_cursor, "segment_us": duration,
                      "speed": (right - left) / duration, "locked": locked,
                      "phase": "protected" if locked else "compressible"})
        output_cursor += duration
    return {"ok": True, "reason": "", "tier": tier, "parts": parts,
            "source_start_us": source_start_us, "source_end_us": source_end_us,
            "source_us": source_us, "target_duration_us": target_duration_us,
            "timeline_us": output_cursor, "protected_us": protected_us,
            "protected_intervals": [{"start": left, "end": right} for left, right in protected],
            "required_speed": sum(compressible) / budget if compressible else 1.0,
            "preferred_cap": float(preferred_cap), "hard_cap": float(hard_cap),
            "fps": float(fps), "frame_us": max(1, frame_us(fps)),
            "maximum_speed": max(part["speed"] for part in parts),
            "exception_reason": "NO_PREFERRED_SPEED_FIT" if tier == "EXCEPTION" else ""}
