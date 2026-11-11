"""Pixel-reviewed presentation phases, independent of claims and speed permits.

A semantic evidence interval can be a static label. A presentation additionally
protects the useful change into that view and its first readable landing. Old
observations without phases stay usable; they are never promoted to phases.
"""
from __future__ import annotations

SCHEMA = "reviewed-presentation-phases-v3"
LEGACY_SCHEMA = "reviewed-presentation-phases-v1"
LEGACY_SCHEMAS = {LEGACY_SCHEMA, "reviewed-presentation-phases-v2"}
VERSION = "presentation-in-out-v6"
CHANGES = {'enter', 'orientation', 'reveal', 'arrange', 'extract'}
FIELDS = ("start_us", "reveal_us", "readable_end_us", "end_us")
PROMPT = """
另观察原素材内的一次商品展示动作，而不只找清晰静帧。可返回presentation_observation：
{"schema":"reviewed-presentation-phases-v3","kind":"dynamic",
"start_us":100000,"reveal_us":700000,"readable_end_us":1100000,"end_us":1900000,
"action":"实际看见的有效变化","result":"第一次展示成立及辨认依据",
"change":{"kind":"orientation","actor":"product","before":"商品侧面朝向镜头","after":"商品正面朝向镜头","product_state_changed":true},
"start_censored":false,"end_censored":false}。
start_us=本次拿起/举入/转正/揭开/摆出已开始产生有效变化的入点，不含找东西、犹豫和无目的准备。
reveal_us=商品正面/数量/展示内容首次到位；readable_end_us=到位后实际足够辨认的最早出点，按包装/数量所需时间判断，不统一套固定停留。
end_us=本次展示可用的最晚出点，在重复摆弄、长时间无新增信息或放回复位之前。
必须start_us<reveal_us<readable_end_us<=end_us且在scene_us内；只选一次动作，不横跨重复周期。
拿取和转向中的短暂模糊不自动当废料，须看它是否导向明确展示。不要把相机乱晃当主体动作。
dynamic必须说明商品本身的前后可见状态变化。change.kind只允许enter(商品入画)、orientation(转正)、reveal(遮挡移开后露出)、arrange(数量摆出)、extract(取出)。
另填change.actor=product|occluder|camera|hand_only。product是商品自身移动/转动/摆放，occluder是箱盖等遮挡物移开露出商品；camera是仅镜头移动，hand_only是只手指指点或抚过。只有product或occluder可用作dynamic，不能把镜头扫到商品伪称商品自身入画。
商品早已摆好，仅手指指点、手掌摸过、托着不动、相机平移或背景移动，返回static，不是商品有效变化。
change.before/after必须分别描述附图中实际看见的商品状态，product_state_changed必须为true；两端商品状态相同不得报dynamic。
入点必须已经发生商品有效变化；空桌、箱子尚未露出商品的悬停和伸手准备不能放进入点至首次到位区间。
边界在附图外、采样不足以定位或结果看不清时，kind用uncertain并说明；静态展示用static，不编造动态。
性能实验仍按原证据合同，不用一般展示阶段替代卖点。阶段本身不授予任何加速许可。
"""


def normalize(value, window, frame_times=None, *, allow_legacy=False):
    """Validate one observed cycle. Timestamps always use the original source."""
    if not isinstance(value, dict) or value.get("schema") not in ({SCHEMA} | LEGACY_SCHEMAS if allow_legacy else {SCHEMA}):
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
    change = value.get('change')
    if value['schema'] != LEGACY_SCHEMA:
        if (not isinstance(change, dict) or change.get('kind') not in CHANGES
                or (value['schema'] == SCHEMA and change.get('actor') not in {'product', 'occluder'})
                or change.get('product_state_changed') is not True
                or any(not isinstance(change.get(k), str) or not change[k].strip() for k in ('before', 'after'))
                or change['before'].strip() == change['after'].strip()):
            raise ValueError('PRESENTATION_PRODUCT_CHANGE_UNOBSERVED')
    times = frame_times if frame_times is not None else value.get("frame_times_us")
    if (not isinstance(times, list) or any(type(t) is not int or not window["start"] <= t <= window["end"] for t in times)
            or len(set(t for t in times if a <= t <= c)) < 2
            or not any(b <= t <= c for t in times)):
        raise ValueError("PRESENTATION_SAMPLES_INSUFFICIENT")
    return {"schema": value['schema'], "kind": "dynamic", **{k: value[k] for k in FIELDS},
            "action": value["action"], "result": value["result"],
            **({'change': dict(change)} if value['schema'] != LEGACY_SCHEMA else {}),
            "start_censored": False, "end_censored": False,
            "review_window_us": dict(window), "frame_times_us": list(times)}


def get(value):
    if value is None:
        return None
    return normalize(value, value.get("review_window_us") if isinstance(value, dict) else None, allow_legacy=True)


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
