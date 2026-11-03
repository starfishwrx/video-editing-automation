"""Pure phase fitting and evidence-first allocation for directed observations."""
from copy import deepcopy

from .paper_retiming import plan_retiming
from .paper_selection import validate_observation
from . import shot_usability


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
    quality=None
    if shot_usability.is_display(request):
        facts=row.get('shot_usability');bound=row['reviewed_window_us']
        quality=shot_usability.assess(facts,request,{'start':lo,'end':hi},reviewed_window=bound)
        if quality['status']!='USABLE_FOR_COMPARISON':
            return {'ok':False,'reason':'DISPLAY_SHOT_NOT_USABLE','usability':quality}
        original=row['window_us']
        if (not shot_usability.covers(facts,lo,original['start'])
                or not shot_usability.covers(facts,original['end'],hi)):
            return {'ok':False,'reason':'DISPLAY_CHANGED_WINDOW_REQUIRES_REVIEW'}
        protected.extend({'start':max(lo,p['start']),'end':min(hi,p['end'])}
            for p in facts['clear_intervals_us'] if max(lo,p['start'])<min(hi,p['end']))
    plan=plan_retiming(lo, hi, protected, duration, preferred_cap=2, hard_cap=3)
    if plan['ok'] and quality is not None:plan['usability']=quality
    return plan


def eligible(candidates,used):
    feasible = [c for c in candidates if c.get("plan", {}).get("ok")]
    # Replaying a source interval is never justified by a matching label.
    feasible = [c for c in feasible if not any(c["source"] == old["source"]
        and max(c["plan"]["source_start_us"], old["start"]) < min(c["plan"]["source_end_us"], old["end"])
        for old in used)]
    return feasible


def choose(candidates, used):
    feasible=eligible(candidates,used)
    if not feasible:
        return None
    return min(feasible, key=lambda c: (-len(c["observation"]["supported_ids"]),shot_usability.rank(c),
        c["plan"]["tier"] == "EXCEPTION", c["plan"]["maximum_speed"],
        sum(c["source"] == old["source"] for old in used), -c["observation"]["confidence"]))
