"""Production playback constraints, independent of model and editor capability.

Old observations remain useful evidence, but do not establish which part of an
action may be accelerated. Missing phase information therefore means real time.
"""
from __future__ import annotations

import math

VERSION = "production-playback-v1"
PROCESS_PREFERRED_MAX = 2.0
PROCESS_MAX = 3.0
OBSERVATION_SCHEMA = "reviewed-playback-window-v1"
PROMPT = """生产画面速度规则：关键接触、完成、提起、结果和计时证明保持原速。
只有本次看图确认的纯过程窗口可以加速，优先不超过2倍，硬上限3倍。
动作阶段未知时按原速求解；不自动慢放，不截掉动作或结果，不改写原始证据区间。
放不下先找另一段更短且动作完整的已复核窗口；仍无解如实报告时长冲突。
不能用后台支持的更高倍率、旧缓存或preview绕过规则。"""


def process_window(value) -> tuple[int, int] | None:
    if not isinstance(value, dict) or value.get("schema") != OBSERVATION_SCHEMA:
        return None
    if value.get("kind") != "process" or not str(value.get("observed") or "").strip():
        return None
    a, b = value.get("start_us"), value.get("end_us")
    if type(a) is not int or type(b) is not int or not 0 <= a < b:
        return None
    return a, b


def speed_range(*, time_sensitive=False, observation=None, override=None) -> tuple[float, float]:
    """Overrides may narrow reviewed permissions; never grant new permissions."""
    if time_sensitive or process_window(observation) is None:
        return 1.0, 1.0
    lo, hi = 1.0, PROCESS_MAX
    if isinstance(override, (list, tuple)) and len(override) == 2:
        try:
            a, b = map(float, override)
            if math.isfinite(a) and math.isfinite(b) and 0 < a <= b:
                lo, hi = max(lo, a), min(hi, b)
        except (TypeError, ValueError):
            pass
    return (lo, hi) if lo <= hi else (0.0, 0.0)


def conflicts(*, start_us, end_us, target_us, speed, time_sensitive=False,
              observation=None, index=None) -> list[dict]:
    """Use actual source/output durations, not a cached policy/version claim."""
    base = {"index": index, "policy_version": VERSION,
            "source_start_us": start_us, "source_end_us": end_us, "target_us": target_us}
    if (any(type(v) is not int for v in (start_us, end_us, target_us))
            or not 0 <= start_us < end_us or target_us <= 0
            or type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0):
        return [{**base, "code": "PLAYBACK_TIMING_INVALID"}]
    actual = (end_us - start_us) / target_us
    lo, hi = speed_range(time_sensitive=time_sensitive, observation=observation)
    window = process_window(observation)
    if window and not window[0] <= start_us < end_us <= window[1]:
        hi = lo = 1.0
    # Integer-microsecond rounding only. A whole frame must not authorize excess speed.
    tolerance = max(1e-8, 2 / target_us)
    if min(speed, actual) < lo - tolerance or max(speed, actual) > hi + tolerance:
        return [{**base, "code": "PLAYBACK_SPEED_CONFLICT", "actual_speed": actual,
                 "declared_speed": speed, "allowed": [lo, hi],
                 "minimum_target_us": math.ceil((end_us - start_us) / hi),
                 "next_action": "reselect_short_complete_window_or_review_related_group"}]
    return []
