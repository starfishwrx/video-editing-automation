"""Pure phase fitting and evidence-first allocation for directed observations."""
from copy import deepcopy

from .paper_retiming import plan_retiming
from .paper_selection import validate_observation


def fit(observation, shot, request):
    row = validate_observation(deepcopy(observation), shot, request)
    if row["verdict"] != "matched":
        return {"ok": False, "reason": "EVIDENCE_UNKNOWN"}
    lo, hi = row["window_us"]["start"], row["window_us"]["end"]
    duration = request["segment_us"]
    # Additional real source context may fill a short demonstration; it must
    # already have been observed, and the actual output is reviewed afterwards.
    if hi - lo < duration:
        bound = row["reviewed_window_us"]
        left = min(lo - bound["start"], (duration - (hi - lo) + 1) // 2)
        lo -= left
        hi = min(bound["end"], lo + duration)
        lo = max(bound["start"], hi - duration)
    protected = [{"start": p["start"], "end": p["end"]} for p in row["phases"]
        if not p["can_speed"] or (row["time_sensitive"] and p["kind"] != "setup")]
    return plan_retiming(lo, hi, protected, duration, preferred_cap=2, hard_cap=3)


def choose(candidates, used):
    feasible = [c for c in candidates if c.get("plan", {}).get("ok")]
    # Replaying a source interval is never justified by a matching label.
    feasible = [c for c in feasible if not any(c["source"] == old["source"]
        and max(c["plan"]["source_start_us"], old["start"]) < min(c["plan"]["source_end_us"], old["end"])
        for old in used)]
    if not feasible:
        return None
    return min(feasible, key=lambda c: (-len(c["observation"]["supported_ids"]),
        c["plan"]["tier"] == "EXCEPTION", c["plan"]["maximum_speed"],
        sum(c["source"] == old["source"] for old in used), -c["observation"]["confidence"]))
