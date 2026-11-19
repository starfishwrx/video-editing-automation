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
ABSORPTION_SPEED_GUIDANCE = (
    "口播中的“几秒吸完”“三秒吸完”“瞬吸”都形容吸收快。看液体被吸收的变化和可辨认的结果，"
    "只看到倒液仍属证据不足。无需计时器或实测秒数，也不因这类措辞要求全程原速。"
    "脚本另行要求展示秒表读数或实拍计时过程时，单独核对画面中的读数或过程。"
)
PROMPT = f"""生产画面速度规则：计时证明保持原速；普通完整短窗与实验分相使用各自许可。
{ABSORPTION_SPEED_GUIDANCE}
已复核完整的放置短窗允许在该窗口内最高2倍速；这是普通短窗的独立许可，不要求先标纯过程。
普通已复看的纯过程硬上限3倍。卫品吸水组以完整三阶段联合复核为准，倒液、吸收和纸巾验证均可加速，无固定倍速上限。
没有过程或完整短窗许可时按原速求解。不自动慢放，不截掉动作或结果，不改写原始证据区间。
放不下先找另一段更短且动作完整的已复核窗口；再对同商品同一演示同卖点的相邻句联合复核连续承接，仍无解如实报告时长冲突。
不能用后台支持的更高倍率、旧缓存或preview绕过规则。"""


def task_absorb_speed_cap(manifest) -> float | None:
    """Validate an explicit task cap without widening any playback permission.

    Ordinary AI observations do not yet establish whether every physical
    window is unrelated to an absorb demonstration. Callers must conservatively
    apply this cap to unknown AI windows rather than classify from keywords.
    """
    raw = (manifest or {}).get("max_absorb_demo_speed")
    if raw is None:
        return None
    try:
        cap = float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("SCENE_SPEED_LIMIT_INVALID") from exc
    if not math.isfinite(cap) or not 1 <= cap <= 20:
        raise ValueError("SCENE_SPEED_LIMIT_INVALID")
    return cap


def capped_range(bounds: tuple[float, float], cap: float | None) -> tuple[float, float]:
    if cap is None:
        return bounds
    lo, hi = bounds
    hi = min(hi, cap)
    return (lo, hi) if lo <= hi else (0.0, 0.0)


def process_window(value) -> tuple[int, int] | None:
    if not isinstance(value, dict) or value.get("schema") != OBSERVATION_SCHEMA:
        return None
    if value.get("kind") != "process" or not str(value.get("observed") or "").strip():
        return None
    a, b = value.get("start_us"), value.get("end_us")
    if type(a) is not int or type(b) is not int or not 0 <= a < b:
        return None
    return a, b


def placement_fit_window(value) -> tuple[int, int] | None:
    """A reviewed short crop may speed up only inside that crop, and only to 2x."""
    if not isinstance(value, dict) or value.get("schema") != OBSERVATION_SCHEMA:
        return None
    if value.get("kind") != "placement_fit" or not str(value.get("observed") or "").strip():
        return None
    a, b = value.get("start_us"), value.get("end_us")
    if type(a) is not int or type(b) is not int or not 0 <= a < b:
        return None
    return a, b


def experiment_phase_window(value) -> tuple[int, int] | None:
    if (not isinstance(value, dict) or value.get("schema") != OBSERVATION_SCHEMA
            or value.get("kind") != "experiment_phase" or value.get("group_policy") != "visible_three_stages"
            or value.get("phase") not in {"context", "pour", "absorb", "press", "lift", "result"}
            or not str(value.get("observed") or "").strip()):
        return None
    a, b = value.get("start_us"), value.get("end_us")
    if type(a) is not int or type(b) is not int or not 0 <= a < b:
        return None
    return a, b


def speed_range(*, time_sensitive=False, observation=None, override=None) -> tuple[float, float]:
    """Overrides may narrow reviewed permissions; never grant new permissions."""
    if time_sensitive and not experiment_phase_window(observation):
        return 1.0, 1.0
    if experiment_phase_window(observation):
        # A positive integer-microsecond target is the only mathematical
        # bound here; task and group authorization are checked by the caller.
        a, b = experiment_phase_window(observation)
        lo, hi = 1.0, float(b - a)
    elif placement_fit_window(observation):
        lo, hi = 1.0, PROCESS_PREFERRED_MAX
    elif process_window(observation) is None:
        return 1.0, 1.0
    else:
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
              observation=None, index=None, task_speed_cap=None) -> list[dict]:
    """Use actual source/output durations, not a cached policy/version claim."""
    base = {"index": index, "policy_version": VERSION,
            "source_start_us": start_us, "source_end_us": end_us, "target_us": target_us}
    if (any(type(v) is not int for v in (start_us, end_us, target_us))
            or not 0 <= start_us < end_us or target_us <= 0
            or type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0):
        return [{**base, "code": "PLAYBACK_TIMING_INVALID"}]
    actual = (end_us - start_us) / target_us
    lo, hi = speed_range(time_sensitive=time_sensitive, observation=observation)
    lo, hi = capped_range((lo, hi), task_speed_cap)
    window = process_window(observation) or placement_fit_window(observation) or experiment_phase_window(observation)
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
