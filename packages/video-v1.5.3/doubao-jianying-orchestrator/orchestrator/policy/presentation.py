"""Pixel-reviewed presentation phases, independent of claims and speed permits.

A semantic evidence interval can be a static label. A presentation additionally
protects the useful change into that view and its first readable landing. Old
observations without phases stay usable; they are never promoted to phases.
"""
from __future__ import annotations

SCHEMA = "reviewed-presentation-phases-v1"
VERSION = "presentation-in-out-v3"
FIELDS = ("start_us", "reveal_us", "readable_end_us", "end_us")
PROMPT = """
另观察原素材内的一次商品展示动作，而不只找清晰静帧。可返回presentation_observation：
{"schema":"reviewed-presentation-phases-v1","kind":"dynamic",
"start_us":100000,"reveal_us":700000,"readable_end_us":1100000,"end_us":1900000,
"action":"实际看见的有效变化","result":"第一次展示成立及辨认依据",
"start_censored":false,"end_censored":false}。
start_us=本次拿起/举入/转正/揭开/摆出已开始产生有效变化的入点，不含找东西、犹豫和无目的准备。
reveal_us=商品正面/数量/展示内容首次到位；readable_end_us=到位后实际足够辨认的最早出点，按包装/数量所需时间判断，不统一套固定停留。
end_us=本次展示可用的最晚出点，在重复摆弄、长时间无新增信息或放回复位之前。
必须start_us<reveal_us<readable_end_us<=end_us且在scene_us内；只选一次动作，不横跨重复周期。
拿取和转向中的短暂模糊不自动当废料，须看它是否导向明确展示。不要把相机乱晃当主体动作。
边界在附图外、采样不足以定位或结果看不清时，kind用uncertain并说明；静态展示用static，不编造动态。
性能实验仍按原证据合同，不用一般展示阶段替代卖点。阶段本身不授予任何加速许可。
"""


def normalize(value, window, frame_times=None):
    """Validate one observed cycle. Timestamps always use the original source."""
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise ValueError("PRESENTATION_SCHEMA_INVALID")
    if value.get("kind") != "dynamic":
        raise ValueError("PRESENTATION_DYNAMIC_UNOBSERVED")
    if not isinstance(window, dict) or any(type(window.get(k)) is not int for k in ("start", "end")):
        raise ValueError("PRESENTATION_REVIEW_WINDOW_INVALID")
    if any(type(value.get(k)) is not int for k in FIELDS):
        raise ValueError("PRESENTATION_TIMING_INVALID")
    a, b, c, d = (value[k] for k in FIELDS)
    if not 0 <= window["start"] <= a < b < c <= d <= window["end"]:
        raise ValueError("PRESENTATION_PHASE_ORDER_INVALID")
    if value.get("start_censored") is not False or value.get("end_censored") is not False:
        raise ValueError("PRESENTATION_BOUNDARY_UNOBSERVED")
    if any(not isinstance(value.get(k), str) or not value[k].strip() for k in ("action", "result")):
        raise ValueError("PRESENTATION_FACTS_MISSING")
    times = frame_times if frame_times is not None else value.get("frame_times_us")
    if (not isinstance(times, list) or any(type(t) is not int or not window["start"] <= t <= window["end"] for t in times)
            or len(set(t for t in times if a <= t <= c)) < 2
            or not any(b <= t <= c for t in times)):
        raise ValueError("PRESENTATION_SAMPLES_INSUFFICIENT")
    return {"schema": SCHEMA, "kind": "dynamic", **{k: value[k] for k in FIELDS},
            "action": value["action"], "result": value["result"],
            "start_censored": False, "end_censored": False,
            "review_window_us": dict(window), "frame_times_us": list(times)}


def get(value):
    if value is None:
        return None
    return normalize(value, value.get("review_window_us") if isinstance(value, dict) else None)


def cut_errors(value, start_us, end_us):
    """No speed/identity judgement here; callers apply their existing contracts."""
    try:
        p = get(value)
    except ValueError as exc:
        return [str(exc)]
    if p is None:
        return []
    if type(start_us) is not int or type(end_us) is not int:
        return ["PRESENTATION_CUT_INVALID"]
    errors = []
    if start_us != p["start_us"]:
        errors.append("PRESENTATION_ONSET_CUT_AWAY" if start_us > p["start_us"] else "PRESENTATION_PREPARATION_INCLUDED")
    if end_us < p["readable_end_us"]:
        errors.append("PRESENTATION_LANDING_CUT_AWAY")
    if end_us > p["end_us"]:
        errors.append("PRESENTATION_RESET_OR_REPEAT_INCLUDED")
    return errors


def group_errors(group):
    if not isinstance(group, dict):
        return []
    context = group.get("input") or {}
    phases = context.get("presentation_observations", [])
    if not isinstance(phases, list):
        return ["PRESENTATION_GROUP_INVALID"]
    window = context.get("window_us") or {}
    return list(dict.fromkeys(error for p in phases
        for error in cut_errors(p, window.get("start"), window.get("end"))))
