"""Pure, frame-aware contiguous action windows; previews remain uncertified.

Microsecond narration bounds and CFR presentation bounds are different clocks.
The latter uses the preview renderer's absolute nearest-even frame rounding.
Protected motion keeps a 1x source-time map; ordinary 60 -> 30 fps sampling is
not mistaken for cutting half of the motion. No extra tail samples are invented.
"""
from __future__ import annotations

from fractions import Fraction
import math

US = 1_000_000
PROTOCOL = "paper-action-window-frames-v1"


def _fail(reason, **details):
    return {"ok": False, "reason": reason, "preview_only": True, **details}


def _integer(value, *, minimum=0):
    return type(value) is int and value >= minimum


def _ratio(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        raise ValueError("INVALID_RATE")
    try:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("INVALID_RATE")
        return Fraction(str(value))
    except (OverflowError, ZeroDivisionError):
        raise ValueError("INVALID_RATE") from None


def _ceil(value):
    return -(-value.numerator // value.denominator)


def _round_frame(time_us, fps):
    return round(Fraction(time_us) * fps / US)


def _cell(frame, fps):
    # Integer times whose exact rational conversion rounds to this frame.
    left = Fraction(2 * frame - 1, 2) * US / fps
    right = Fraction(2 * frame + 1, 2) * US / fps
    lo, hi = _ceil(left), right.numerator // right.denominator
    if frame % 2:
        lo += left.denominator == 1
        hi -= right.denominator == 1
    return lo, hi


def _fraction(value):
    return {"numerator": value.numerator, "denominator": value.denominator}


def _inputs(frame_pts_us, phases, source_end_us, source_fps, output_fps,
            target_start_us, target_duration_us, min_result_frames,
            preferred_cap, hard_cap, require_exact_frame_duration):
    if (not isinstance(frame_pts_us, (list, tuple)) or not frame_pts_us
            or any(not _integer(p) for p in frame_pts_us)
            or any(a >= b for a, b in zip(frame_pts_us, frame_pts_us[1:]))
            or not _integer(source_end_us, minimum=1) or source_end_us <= frame_pts_us[-1]):
        raise ValueError("INVALID_SOURCE_PTS")
    src_fps, fps = _ratio(source_fps), _ratio(output_fps)
    if src_fps < fps:
        raise ValueError("SOURCE_FPS_REQUIRES_DUPLICATION")
    normal_step = src_fps / fps
    if normal_step.denominator != 1:
        raise ValueError("NONINTEGER_SOURCE_OUTPUT_RATIO_UNSUPPORTED")
    period = Fraction(US) / src_fps
    boundaries = list(frame_pts_us) + [source_end_us]
    if (any(abs(Fraction(b - a) - period) > 1 for a, b in zip(boundaries, boundaries[1:]))
            or any(abs(Fraction(p - boundaries[0]) - i * period) > 1 for i, p in enumerate(boundaries))):
        raise ValueError("SOURCE_FRAME_GAP_OR_NON_CFR")
    if (not _integer(target_start_us) or not _integer(target_duration_us, minimum=1)
            or not _integer(min_result_frames, minimum=1)
            or type(require_exact_frame_duration) is not bool):
        raise ValueError("INVALID_TARGET_OR_RESULT_POLICY")
    preferred, hard = _ratio(preferred_cap), _ratio(hard_cap)
    if not 1 <= preferred <= hard <= 3:
        raise ValueError("INVALID_SPEED_CAP")
    if not isinstance(phases, list) or not phases:
        raise ValueError("INVALID_PHASES")
    protected, results, cursor = [], [], None
    for phase in phases:
        if not isinstance(phase, dict):
            raise ValueError("INVALID_PHASES")
        a, b = phase.get("start_us"), phase.get("end_us")
        left, right = phase.get("start_uncertainty_us", 0), phase.get("end_uncertainty_us", 0)
        if (not _integer(a) or not _integer(b, minimum=1) or a >= b
                or not _integer(left) or not _integer(right)
                or a - left < boundaries[0] or b + right > source_end_us
                or (cursor is not None and cursor != a)
                or phase.get("kind") not in {"setup", "process", "key_action", "result", "display"}
                or type(phase.get("can_speed")) is not bool):
            raise ValueError("INVALID_OR_NONCONTIGUOUS_PHASES")
        cursor = b
        if phase["kind"] in {"key_action", "result"} and phase["can_speed"]:
            raise ValueError("PROTECTED_PHASE_CANNOT_SPEED")
        if not phase["can_speed"]:
            protected.append((a - left, b + right))
        if phase["kind"] == "result":
            if a + left >= b - right:
                raise ValueError("RESULT_BOUNDARY_UNCERTAIN")
            results.append((a + left, b - right))
    if not any(p["kind"] == "key_action" for p in phases) or not results:
        raise ValueError("COMPLETE_ACTION_AND_RESULT_REQUIRED")
    start_frame = _round_frame(target_start_us, fps)
    end_frame = _round_frame(target_start_us + target_duration_us, fps)
    count = end_frame - start_frame
    if count <= 0:
        raise ValueError("TARGET_HAS_NO_OUTPUT_FRAMES")
    target = {
        "target_start_us": target_start_us, "target_duration_us": target_duration_us,
        "output_fps": float(fps), "output_start_frame": start_frame,
        "output_end_frame": end_frame, "output_frames": count,
        "frame_duration_us": _fraction(Fraction(US) / fps),
        "presentation_duration_us": _fraction(Fraction(count * US) / fps),
        "duration_quantization_error_us": float(Fraction(count * US) / fps - target_duration_us),
        "start_quantization_error_us": float(Fraction(start_frame * US) / fps - target_start_us),
        "end_quantization_error_us": float(Fraction(end_frame * US) / fps - target_start_us - target_duration_us),
        "microsecond_exact": True,
        "frame_duration_exact": Fraction(count * US) / fps == target_duration_us,
        "quantization": "absolute-nearest-even",
        "source_pts_rounding_tolerance_us": 1,
    }
    return {"boundaries": boundaries, "phases": phases, "protected": protected, "results": results,
            "fps": fps, "source_fps": src_fps, "normal_step": int(normal_step),
            "preferred": preferred, "hard": hard, "target": target,
            "min_result_frames": min_result_frames,
            "exact_required": require_exact_frame_duration}


def _runs(context, start, end):
    bounds, phases = context["boundaries"], context["phases"]
    if (start not in bounds or end not in bounds or start >= end
            or start < phases[0]["start_us"] or end > phases[-1]["end_us"]):
        raise ValueError("WINDOW_NOT_REVIEWED_SOURCE_FRAME_BOUNDARIES")
    first, last = bounds.index(start), bounds.index(end)
    if any(start > a or end < b for a, b in context["protected"]):
        raise ValueError("WINDOW_CUTS_PROTECTED_ACTION")
    runs = []
    for i in range(first, last):
        a, b = bounds[i:i + 2]
        locked = (any(a < y and b > x for x, y in context["protected"])
                  or not any(p["can_speed"] and p["start_us"] <= a and p["end_us"] >= b for p in phases))
        if runs and runs[-1]["locked"] == locked:
            runs[-1]["source_end_us"] = b
            runs[-1]["source_end_frame"] = i + 1
        else:
            runs.append({"source_start_us": a, "source_end_us": b, "locked": locked,
                         "source_start_frame": i, "source_end_frame": i + 1})
    return runs


def _allocate(context, runs, cap):
    """Interval DP: <= target frame count states per phase in ordinary cases.

    A state retains every integer target timestamp in one quantization cell.
    Containment pruning preserves feasibility without a microsecond-sized grid.
    """
    target, fps = context["target"], context["fps"]
    final_time = target["target_start_us"] + target["target_duration_us"]
    initial = {"lo": target["target_start_us"], "hi": target["target_start_us"],
               "frame": target["output_start_frame"], "parent": None}
    states = [initial]
    for run in runs:
        length = run["source_end_us"] - run["source_start_us"]
        physical = Fraction((run["source_end_frame"] - run["source_start_frame"]) * US) / context["source_fps"]
        minimum = length if run["locked"] else max(_ceil(Fraction(length) / cap), _ceil(physical / cap))
        max_us = length
        min_frames = _ceil(physical * fps / US / (1 if run["locked"] else cap))
        max_step = int((1 if run["locked"] else cap) * context["normal_step"])
        min_frames = max(min_frames, _ceil(Fraction(run["source_end_frame"] - run["source_start_frame"], max_step)))
        next_states = []
        for previous in states:
            for frame in range(previous["frame"] + max(1, min_frames), target["output_end_frame"] + 1):
                n = frame - previous["frame"]
                # Each requested output sample must lie inside the actual
                # source; accepting tpad-generated frames would hide truncation.
                min_us = max(minimum, _ceil(Fraction(US) / fps),
                             (Fraction((n - 1) * US) / fps).numerator
                             // (Fraction((n - 1) * US) / fps).denominator + 1)
                if min_us > max_us:
                    continue
                lo, hi = _cell(frame, fps)
                lo, hi = max(lo, previous["lo"] + min_us), min(hi, previous["hi"] + max_us, final_time)
                if lo > hi:
                    continue
                if any(s["frame"] == frame and s["lo"] <= lo and s["hi"] >= hi for s in next_states):
                    continue
                next_states = [s for s in next_states
                               if not (s["frame"] == frame and lo <= s["lo"] and hi >= s["hi"])]
                next_states.append({"lo": lo, "hi": hi, "frame": frame, "parent": previous,
                                    "run": run, "min_us": min_us, "max_us": max_us})
        states = next_states
        if not states:
            return None
        if len(states) > 100_000:
            raise ValueError("FRAME_SEARCH_LIMIT")
    finals = [s for s in states if s["lo"] <= final_time <= s["hi"]]
    if not finals:
        return None
    protected_us = sum(r["source_end_us"] - r["source_start_us"] for r in runs if r["locked"])
    process_us = sum(r["source_end_us"] - r["source_start_us"] for r in runs if not r["locked"])
    remaining = target["target_duration_us"] - protected_us
    node, end, parts = finals[0], final_time, []
    while node["parent"] is not None:
        previous, run = node["parent"], node["run"]
        lo = max(previous["lo"], end - node["max_us"])
        hi = min(previous["hi"], end - node["min_us"])
        length = run["source_end_us"] - run["source_start_us"]
        ideal = length if run["locked"] else Fraction(length * remaining, process_us)
        start = min(hi, max(lo, round(Fraction(end) - ideal)))
        duration = end - start
        parts.append({**run, "target_start_us": start, "target_end_us": end,
                      "segment_us": duration, "speed": float(Fraction(length, duration)),
                      "output_start_frame": previous["frame"], "output_end_frame": node["frame"],
                      "output_frames": node["frame"] - previous["frame"]})
        node, end = previous, start
    return list(reversed(parts))


def _proof(context, parts, source, cap):
    fps, bounds = context["fps"], context["boundaries"]
    minimum, samples, result_frames = context["min_result_frames"], [], 0
    for part_index, part in enumerate(parts):
        a, b, duration = part["source_start_us"], part["source_end_us"], part["segment_us"]
        speed = Fraction(b - a, duration)
        n = part["output_frames"]
        presentation = Fraction(n * US) / fps
        physical = Fraction((part["source_end_frame"] - part["source_start_frame"]) * US) / context["source_fps"]
        physical_start = Fraction(bounds[0]) + Fraction(part["source_start_frame"] * US) / context["source_fps"]
        physical_end = physical_start + physical
        source_frames = part["source_end_frame"] - part["source_start_frame"]
        step = (Fraction(context["normal_step"]) if part["locked"]
                else max(Fraction(context["normal_step"]), Fraction(source_frames, n)))
        map_speed = step / context["normal_step"]
        part.update(source=source, frame_domain_speed=float(physical / presentation),
                    source_frame_duration_us=_fraction(physical),
                    source_time_map_speed=float(map_speed),
                    source_pts_rounding_error_us=float(Fraction(b - a) - physical),
                    duration_quantization_error_us=float(presentation - duration),
                    start_quantization_error_us=float(Fraction(part["output_start_frame"] * US) / fps
                                                     - part["target_start_us"]),
                    end_quantization_error_us=float(Fraction(part["output_end_frame"] * US) / fps
                                                   - part["target_end_us"]))
        for local in range(n):
            source_time = physical_start + local * step * US / context["source_fps"]
            if source_time >= physical_end:
                raise ValueError("WOULD_REQUIRE_TAIL_FREEZE")
            frame_offset = (source_time - bounds[0]) * context["source_fps"] / US
            frame_index = frame_offset.numerator // frame_offset.denominator
            cover_end = min(physical_end, source_time + Fraction(US) / fps * map_speed)
            selected_pts = Fraction(bounds[0]) + Fraction(frame_index * US) / context["source_fps"]
            is_result = any(x <= selected_pts < y for x, y in context["results"])
            result_frames += is_result
            samples.append({"output_frame": part["output_start_frame"] + local,
                            "part_index": part_index,
                            "source_frame_index": frame_index,
                            "source_time_us": _fraction(source_time),
                            "source_coverage_end_us": _fraction(cover_end),
                            "locked": part["locked"], "definite_result": is_result})
        if part["locked"] and (speed != 1 or presentation < physical):
            raise ValueError("PROTECTED_TIME_TRUNCATED_OR_ACCELERATED")
    maximum_step = int(cap * context["normal_step"])
    actual_steps = []
    for previous, following in zip(samples, samples[1:]):
        delta = following["source_frame_index"] - previous["source_frame_index"]
        if delta <= 0:
            raise ValueError("SOURCE_FRAME_REPEAT_OR_REVERSE")
        if delta > maximum_step:
            raise ValueError("INSTANTANEOUS_SPEED_CAP_EXCEEDED")
        if (previous["part_index"] == following["part_index"] and previous["locked"]
                and delta != context["normal_step"]):
            raise ValueError("PROTECTED_SOURCE_FRAME_STEP_CHANGED")
        actual_steps.append(delta)
    if result_frames < minimum:
        raise ValueError("INSUFFICIENT_RESULT_OUTPUT_FRAMES")
    return {"samples": samples, "result_output_frames": result_frames,
            "min_result_frames": minimum, "source_sampling": "explicit-cfr-source-frame-map",
            "maximum_source_frame_step": max(actual_steps, default=0),
            "maximum_instantaneous_speed": max(actual_steps, default=0) / context["normal_step"],
            "normal_source_frame_step": context["normal_step"],
            "requires_explicit_frame_map": True,
            "sampling_is_analytical_not_decoded_render": True,
            "tail_freeze_required": False, "protected_time_map_speed": 1}


def _plan(context, start, end, source):
    target = context["target"]
    if context["exact_required"] and not target["frame_duration_exact"]:
        return _fail("TARGET_FRAME_DURATION_INCOMPATIBLE", target=target)
    try:
        runs = _runs(context, start, end)
        if end - start < target["target_duration_us"]:
            return _fail("SOURCE_TOO_SHORT", target=target)
        for tier, cap in (("PREFERRED", context["preferred"]), ("EXCEPTION", context["hard"])):
            if tier == "EXCEPTION" and cap == context["preferred"]:
                continue
            parts = _allocate(context, runs, cap)
            if parts is None:
                continue
            try:
                proof = _proof(context, parts, source, cap)
            except ValueError as error:
                # Result duration policy is a real feasibility constraint;
                # failure cannot be converted to an accepted mathematical fit.
                return _fail(str(error), target=target)
            return {"ok": True, "reason": "OK", "protocol": PROTOCOL, "preview_only": True,
                    "tier": tier, "cap": float(cap), "target": target,
                    "source_window_us": {"start": start, "end": end},
                    "parts": parts, "proof": proof,
                    "rendering_contract": "explicit-source-frame-map",
                    "maximum_speed": max(p["speed"] for p in parts),
                    "maximum_frame_domain_speed": max(p["frame_domain_speed"] for p in parts)}
        return _fail("FRAME_DOMAIN_DURATION_CONFLICT", target=target,
                     source_window_us={"start": start, "end": end})
    except ValueError as error:
        return _fail(str(error), target=target)


def plan_action_window(frame_pts_us, phases, *, source_end_us, window_start_us,
                       window_end_us, source_fps, min_result_frames, source="",
                       target_start_us, target_duration_us,
                       output_fps=30, preferred_cap=2, hard_cap=3,
                       require_exact_frame_duration=False):
    """Plan one reviewed contiguous window; no media decoding or external calls.

    PTS values and phase boundaries are integer microseconds. ``source_end_us``
    is the final decoded frame's end, not a container/audio duration. Every
    phase is reviewed; only explicit ``can_speed=True`` portions may compress.
    Boundary uncertainty is a nonnegative outward margin in microseconds.
    ``min_result_frames`` is caller policy, not an invented quality threshold.
    """
    try:
        context = _inputs(frame_pts_us, phases, source_end_us, source_fps, output_fps,
                          target_start_us, target_duration_us, min_result_frames,
                          preferred_cap, hard_cap, require_exact_frame_duration)
        if not _integer(window_start_us) or not _integer(window_end_us, minimum=1):
            return _fail("INVALID_SOURCE_WINDOW")
        return _plan(context, window_start_us, window_end_us, source)
    except (ValueError, TypeError) as error:
        return _fail(str(error))


def enumerate_action_windows(frame_pts_us, phases, *, source_end_us, source_fps,
                             min_result_frames, source="", target_start_us,
                             target_duration_us, output_fps=30,
                             preferred_cap=2, hard_cap=3, window_bounds_us=None,
                             max_candidates=20, max_windows=10_000,
                             require_exact_frame_duration=False):
    """Enumerate source-frame windows inside the reviewed span; rank <=2x first.

    A scan limit is reported as incomplete enumeration, never as no material.
    The returned candidates are preview plans, not semantic/visual acceptance.
    """
    try:
        context = _inputs(frame_pts_us, phases, source_end_us, source_fps, output_fps,
                          target_start_us, target_duration_us, min_result_frames,
                          preferred_cap, hard_cap, require_exact_frame_duration)
        if not _integer(max_candidates, minimum=1) or not _integer(max_windows, minimum=1):
            return _fail("INVALID_ENUMERATION_LIMIT")
        bounds = ({"start": phases[0]["start_us"], "end": phases[-1]["end_us"]}
                  if window_bounds_us is None else window_bounds_us)
        if (not isinstance(bounds, dict) or not _integer(bounds.get("start"))
                or not _integer(bounds.get("end"), minimum=1) or bounds["start"] >= bounds["end"]):
            return _fail("INVALID_WINDOW_BOUNDS")
        critical_start = min(a for a, _ in context["protected"])
        critical_end = max(b for _, b in context["protected"])
        starts = [p for p in context["boundaries"] if max(bounds["start"], phases[0]["start_us"]) <= p <= critical_start]
        ends = [p for p in context["boundaries"] if critical_end <= p <= min(bounds["end"], phases[-1]["end_us"])]
        plans, failures, scanned = [], {}, 0
        if len(starts) * len(ends) > max_windows:
            return _fail("ENUMERATION_LIMIT", complete=False, possible_windows=len(starts) * len(ends),
                         max_windows=max_windows, target=context["target"])
        for start in starts:
            for end in ends:
                scanned += 1
                result = _plan(context, start, end, source)
                if result["ok"]:
                    plans.append(result)
                else:
                    failures[result["reason"]] = failures.get(result["reason"], 0) + 1
        plans.sort(key=lambda p: (p["tier"] != "PREFERRED", p["maximum_frame_domain_speed"],
                                  p["maximum_speed"], len(p["parts"]),
                                  p["source_window_us"]["end"] - p["source_window_us"]["start"]))
        return {"ok": bool(plans), "reason": "OK" if plans else "NO_FEASIBLE_ACTION_WINDOW",
                "protocol": PROTOCOL, "preview_only": True, "complete": True,
                "target": context["target"], "windows_scanned": scanned,
                "feasible_count": len(plans), "failure_counts": failures,
                "plans": plans[:max_candidates], "best": plans[0] if plans else None}
    except (ValueError, TypeError) as error:
        return _fail(str(error))


def validate_action_plan(plan, frame_pts_us, phases, *, source_end_us, source_fps,
                         min_result_frames, target_start_us,
                         target_duration_us, output_fps=30,
                         preferred_cap=2, hard_cap=3):
    """Independently recheck emitted parts, not their stored success/proof flags."""
    try:
        context = _inputs(frame_pts_us, phases, source_end_us, source_fps, output_fps,
                          target_start_us, target_duration_us, min_result_frames,
                          preferred_cap, hard_cap, False)
        if not isinstance(plan, dict) or not plan.get("ok") or not isinstance(plan.get("parts"), list) or not plan["parts"]:
            return _fail("PLAN_NOT_COMPLETE")
        window = plan["source_window_us"]
        runs = _runs(context, window["start"], window["end"])
        if len(runs) != len(plan["parts"]):
            return _fail("SOURCE_PART_GAP_OR_PROTECTION_CHANGED")
        if plan.get("tier") not in {"PREFERRED", "EXCEPTION"}:
            return _fail("INVALID_SPEED_TIER")
        limit = context["preferred"] if plan["tier"] == "PREFERRED" else context["hard"]
        if plan.get("cap") != float(limit):
            return _fail("INVALID_SPEED_TIER")
        target_cursor = target_start_us
        source = plan["parts"][0].get("source")
        checked = []
        for run, original in zip(runs, plan["parts"]):
            part = dict(original)
            if not isinstance(source, str) or part.get("source") != source:
                return _fail("SOURCE_CHANGED_BETWEEN_PARTS")
            if any(part.get(k) != v for k, v in run.items()):
                return _fail("SOURCE_PART_GAP_OR_PROTECTION_CHANGED")
            duration = part.get("segment_us")
            if not _integer(duration, minimum=1) or part.get("target_start_us") != target_cursor:
                return _fail("TARGET_PART_GAP_OR_INVALID_DURATION")
            target_end = target_cursor + duration
            q0, q1 = _round_frame(target_cursor, context["fps"]), _round_frame(target_end, context["fps"])
            if (part.get("target_end_us") != target_end or part.get("output_start_frame") != q0
                    or part.get("output_end_frame") != q1 or part.get("output_frames") != q1 - q0
                    or q1 <= q0 or duration < _ceil(Fraction(US) / context["fps"])):
                return _fail("OUTPUT_FRAME_ACCOUNTING_MISMATCH")
            speed = Fraction(run["source_end_us"] - run["source_start_us"], duration)
            supplied = part.get("speed")
            if isinstance(supplied, bool) or not isinstance(supplied, (int, float)) or not math.isfinite(supplied) or abs(float(speed) - supplied) > 1e-12:
                return _fail("SPEED_DOES_NOT_MATCH_SOURCE_TIME_MAP")
            frame_speed = Fraction(run["source_end_frame"] - run["source_start_frame"]) * context["fps"] / ((q1 - q0) * context["source_fps"])
            physical_speed = Fraction((run["source_end_frame"] - run["source_start_frame"]) * US) / context["source_fps"] / duration
            if speed < 1 or speed > limit or frame_speed > limit or (not run["locked"] and physical_speed > limit):
                return _fail("SPEED_CAP_EXCEEDED_OR_SLOWDOWN")
            if run["locked"] and (speed != 1 or frame_speed > 1):
                return _fail("PROTECTED_TIME_TRUNCATED_OR_ACCELERATED")
            target_cursor = target_end
            checked.append(part)
        if target_cursor != target_start_us + target_duration_us:
            return _fail("TARGET_DURATION_CHANGED")
        proof = _proof(context, checked, str(plan["parts"][0].get("source", "")), limit)
        supplied_samples = plan.get("proof", {}).get("samples")
        if not isinstance(supplied_samples, list) or not supplied_samples:
            return _fail("SOURCE_FRAME_MAP_MISSING")
        if any(not isinstance(s, dict) or not _integer(s.get("source_frame_index")) for s in supplied_samples):
            return _fail("SOURCE_FRAME_MAP_INVALID")
        for a, b in zip(supplied_samples, supplied_samples[1:]):
            delta = b["source_frame_index"] - a["source_frame_index"]
            if delta <= 0:
                return _fail("SOURCE_FRAME_REPEAT_OR_REVERSE")
            if delta > int(limit * context["normal_step"]):
                return _fail("INSTANTANEOUS_SPEED_CAP_EXCEEDED")
            if a.get("part_index") == b.get("part_index") and a.get("locked") and delta != context["normal_step"]:
                return _fail("PROTECTED_SOURCE_FRAME_STEP_CHANGED")
        if supplied_samples != proof["samples"]:
            return _fail("SOURCE_FRAME_MAP_MISMATCH")
        return {"ok": True, "reason": "OK", "preview_only": True, "target": context["target"], "proof": proof}
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        return _fail(str(error))
