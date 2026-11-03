"""Choose a real decoder frame for a fixed-duration real-time display window."""
from bisect import bisect_left


def display_start(requested_us, duration_us, frame_pts_us, source_duration_us, *, maximum_shift_us=16667):
    if not frame_pts_us or duration_us <= 0 or any(a >= b for a,b in zip(frame_pts_us,frame_pts_us[1:])):
        raise ValueError("DISPLAY_FRAME_LEDGER_INVALID")
    index=bisect_left(frame_pts_us,requested_us)
    candidates=frame_pts_us[max(0,index-1):index+1]
    candidates=[p for p in candidates if abs(p-requested_us) <= maximum_shift_us and p+duration_us <= source_duration_us]
    if not candidates:
        raise ValueError("DISPLAY_FRAME_WINDOW_INFEASIBLE")
    return min(candidates,key=lambda p:(abs(p-requested_us),p))
