"""场景规则后处理（v1.5.2，卫品指令）。

选镜（``match_manifest``）完成后，对命中场景规则的句子重新定窗：

* ``split_speed``（吸水实验）：同一条素材拆成「倒水 / 吸收 / 擦拭」三段，各段不同倍速，
  吸收段快进到 3 秒内，倒水与擦拭最高 1.6 倍，三段加起来正好等于这句配音；连续几句都在讲
  吸水时，这几句合起来演一遍实验，按句子边界切给各句；
* ``full_window``（撕包装、透灯光、荧光测试、正面指产品）：窗口必须盖住完整动作，
  允许在规则给出的范围内倍速；``text_forces_visual``（拆包）文案命中就要动作画面，
  选中的是袋面静态时换带动作的候选或素材池镜头，换不到保留原镜头记 SCENE_NO_FOOTAGE；
  拆包配置了 ``group_*`` 时与吸水一样可跨相邻句连续播完同一条拆包动作，单句够用则不硬并；
* ``single_shot``（背标）：一个镜头盖住整句，不拼接，最慢 0.85 倍；
* ``require_visual``（面层）：口播提到面层时，镜头必须看得到面层。

选中的镜头满足不了，就按推荐顺序换候选；都不行时，动作确实放不进这句话的在预览稿留黑屏
（SCENE_ACTION_TOO_LONG），素材池里根本没有该场景画面的（SCENE_NO_FOOTAGE）同样留黑；正式稿保留
原镜头并记录；相位标不出来的保留原镜头并记录。吸水实验只缺纸巾擦拭时演「倒水 → 吸收」，
状态记 ``partial``（SCENE_PHASE_MISSING），提示补拍。
所有处理结果写进 ``scene_rules_report.json``。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional

from . import phase_timing, rulepack, timing_contract
from .timing_contract import US

SCENE_ACTION_TOO_LONG = "SCENE_ACTION_TOO_LONG"
SCENE_ACTION_INCOMPLETE = "SCENE_ACTION_INCOMPLETE"
SCENE_PHASE_UNVERIFIED = "SCENE_PHASE_UNVERIFIED"
SCENE_VISUAL_MISSING = "SCENE_VISUAL_MISSING"
SCENE_SINGLE_SHOT_INFEASIBLE = "SCENE_SINGLE_SHOT_INFEASIBLE"
SCENE_SENTENCE_TOO_LONG = "SCENE_SENTENCE_LONGER_THAN_ACTION"
SCENE_NO_FOOTAGE = "SCENE_NO_FOOTAGE"
SCENE_PHASE_MISSING = "SCENE_PHASE_MISSING"
SCENE_SOURCE_DUPLICATE = "SCENE_SOURCE_DUPLICATE"
SCENE_SOURCE_BUDGET = "SCENE_SOURCE_BUDGET"
_TOO_LONG = {timing_contract.REASON_EVIDENCE_SPAN_TOO_LONG, SCENE_ACTION_TOO_LONG}
MIN_PART_US = 100_000


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _us(seconds: Any) -> int:
    return int(round(_number(seconds) * US))


def _fps(manifest: dict[str, Any]) -> float:
    return _number(manifest.get("fps"), 0.0) or timing_contract.DEFAULT_FPS


def _tail_pad_us(manifest: dict[str, Any]) -> int:
    return timing_contract.us_of(manifest.get("tail_pad_s"), timing_contract.DEFAULT_TAIL_PAD_US)


def _material_us(shot: dict[str, Any]) -> int:
    value = _number(shot.get("material_duration_us"))
    return int(value) if value > 1000 else _us(shot.get("source_end"))


def _range(spec: Any, default: tuple[float, float] = (1.0, 1.0)) -> tuple[float, float]:
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        lo, hi = _number(spec[0], default[0]), _number(spec[1], default[1])
        if lo > 0 and hi > 0:
            return (min(lo, hi), max(lo, hi))
    return default


# ── 单镜定窗 ───────────────────────────────────────────────────────────
def solve_single(shot: dict[str, Any], *, audio_us: int, manifest: dict[str, Any],
                 speed_range: tuple[float, float],
                 evidence: Optional[tuple[float, float]] = None) -> tuple[Any, list[float]]:
    """按场景要求求一个单镜窗口。返回 ``(WindowSolution, [head_guard_s, tail_guard_s])``。

    证据区间来自相位打点时，保护区缩到不越过动作起止点，避免把倒水的第一帧当废料裁掉。
    """
    scene_in, scene_out = _number(shot.get("source_start")), _number(shot.get("source_end"))
    head, tail = _head_guard_us(manifest), _tail_guard_us(manifest)
    if evidence is not None:
        head = max(0, min(head, _us(evidence[0] - scene_in)))
        tail = max(0, min(tail, _us(scene_out - evidence[1])))
        ev_start, ev_end = _us(evidence[0]), _us(evidence[1])
    else:
        ev_start = ev_end = None
    request = timing_contract.WindowRequest(
        source_in_us=_us(scene_in), source_out_us=_us(scene_out),
        audio_duration_us=int(audio_us), tail_pad_us=_tail_pad_us(manifest),
        head_guard_us=head, tail_guard_us=tail,
        evidence_start_us=ev_start, evidence_end_us=ev_end,
        speed_min=speed_range[0], speed_max=speed_range[1],
        video_duration_us=_material_us(shot), fps=_fps(manifest))
    return timing_contract.solve_window(request), [round(head / US, 6), round(tail / US, 6)]


# ── 吸水实验：同源三段不同倍速 ─────────────────────────────────────────
def plan_split(shot: dict[str, Any], phases: dict[str, list[float]], scene: dict[str, Any], *,
               audio_us: int, manifest: dict[str, Any]) -> dict[str, Any]:
    """把「倒水 → 吸收 → 擦拭」三段分到这句配音上。

    时间线总长 = 配音 + 尾留白。默认倒水、擦拭 1.0 倍，吸收段快进到不超过
    ``absorb_max_output_s``；总长超了先提吸收段倍速，再把倒水、擦拭一起提速（默认 ≤1.6）；
    口播太短、常规倒水倍速仍装不下时，再按 ``pour_burst_max`` / ``wipe_burst_max`` 提速
    （默认 4.0），宁可快一点也要用真倒液实验，不要留黑或换透光镜头凑数。
    总长不够先放慢吸收段（不低于 1.0 倍、输出不超过上限），再向擦拭之后、倒水之前延伸素材。

    擦拭在 ``optional_phases`` 里且素材没拍到时，只演「倒水 → 吸收」；时长不够就接吸收完的
    结果画面（``result`` 段），返回 ``missing_phases`` 供报告提示补拍。
    """
    speed = scene.get("speed") or {}
    pour_rng = _range(speed.get("pour"), (1.0, 1.6))
    wipe_rng = _range(speed.get("wipe"), (1.0, 1.6))
    pour_burst = max(pour_rng[1], _number(speed.get("pour_burst_max"), 4.0))
    wipe_burst = max(wipe_rng[1], _number(speed.get("wipe_burst_max"), pour_burst))
    absorb_cap_us = _us(speed.get("absorb_max_output_s", 3.0))
    absorb_hi = max(1.0, _number(speed.get("absorb_max_speed"), 6.0))
    fps = _fps(manifest)
    frame = timing_contract.frame_us(fps)
    names = [p["name"] for p in scene.get("phases") or []]
    optional = set(scene.get("optional_phases") or [])
    if len(names) != 3 or any(n not in phases for n in names if n not in optional):
        return {"ok": False, "reason": SCENE_ACTION_INCOMPLETE}
    missing = [n for n in names if n not in phases]
    has_wipe = names[2] in phases
    tail_name = names[2] if has_wipe else "result"
    scene_in, scene_out = _us(shot.get("source_start")), _us(shot.get("source_end"))
    scene_out = min(scene_out, _material_us(shot))
    b0 = max(scene_in, _us(phases[names[0]][0]))
    b1 = max(b0, _us(phases[names[0]][1]))
    if has_wipe:
        b2 = max(b1, _us(phases[names[2]][0]))
        b3 = min(scene_out, max(b2, _us(phases[names[2]][1])))
    else:
        b2 = b3 = min(scene_out, max(b1, _us(phases[names[1]][1])))
    pour_m, absorb_m, wipe_m = b1 - b0, b2 - b1, b3 - b2
    if pour_m < MIN_PART_US or (has_wipe and wipe_m < MIN_PART_US):
        return {"ok": False, "reason": SCENE_ACTION_INCOMPLETE,
                "detail": {"pour_us": pour_m, "wipe_us": wipe_m}}
    timeline = int(audio_us) + _tail_pad_us(manifest)
    absorb_lo = max(1.0, absorb_m / float(absorb_cap_us)) if absorb_m > 0 else 1.0
    if absorb_lo > absorb_hi + 1e-9:
        return {"ok": False, "reason": SCENE_ACTION_TOO_LONG, "detail": {"absorb_us": absorb_m}}
    s_pour, s_absorb, s_wipe = 1.0, absorb_lo, 1.0

    def total() -> float:
        return pour_m / s_pour + (absorb_m / s_absorb if absorb_m else 0) + wipe_m / s_wipe

    if total() > timeline:
        if absorb_m:
            room = timeline - pour_m - wipe_m
            s_absorb = absorb_hi if room <= absorb_m / absorb_hi else max(absorb_lo, absorb_m / room)
        if total() > timeline + 1:
            rest = timeline - (absorb_m / s_absorb if absorb_m else 0)
            common = (pour_m + wipe_m) / rest if rest > 0 else float("inf")
            pour_hi = pour_rng[1]
            wipe_hi = wipe_rng[1]
            if common > min(pour_hi, wipe_hi) + 1e-9:
                # 常规 1.6× 放不下：短句仍优先真实验，放开到 burst 上限。
                pour_hi, wipe_hi = pour_burst, wipe_burst
                pour_rng = (pour_rng[0], pour_hi)
                wipe_rng = (wipe_rng[0], wipe_hi)
            if common > min(pour_hi, wipe_hi) + 1e-9:
                return {"ok": False, "reason": SCENE_ACTION_TOO_LONG,
                        "detail": {"timeline_us": timeline, "action_us": b3 - b0,
                                   "needed_speed": round(common, 3)}}
            s_pour = s_wipe = max(1.0, common)
    elif total() < timeline:
        if absorb_m:
            s_absorb = max(1.0, absorb_m / float(absorb_cap_us))
        short = timeline - total()
        if short > 0:
            after = max(0, scene_out - _tail_guard_us(manifest) - b3)
            grow = min(after, int(short))
            b3 += grow
            wipe_m += grow
            short -= grow
        if short > 0:
            before = max(0, b0 - scene_in - _head_guard_us(manifest))
            grow = min(before, int(short))
            b0 -= grow
            pour_m += grow
            short -= grow
        if short > frame:
            return {"ok": False, "reason": SCENE_SENTENCE_TOO_LONG,
                    "detail": {"timeline_us": timeline, "action_us": b3 - b0}}
    parts_spec = [(names[0], pour_m, s_pour, pour_rng)]
    if absorb_m:
        parts_spec.append((names[1], absorb_m, s_absorb, (1.0, absorb_hi)))
    if wipe_m > 0:
        parts_spec.append((tail_name, wipe_m, s_wipe, wipe_rng))
    snapped = [timing_contract.frame_speed(s, rng[0], rng[1], fps=fps)
               for _n, _m, s, rng in parts_spec]
    outs = [int(round(m / s)) for (_n, m, _s, _r), s in zip(parts_spec, snapped)]
    outs[-1] = timeline - sum(outs[:-1])
    parts = []
    cursor = b0
    for (name, _m, _s, rng), s, out in zip(parts_spec, snapped, outs):
        material = int(round(s * out))
        start, end = cursor, cursor + material
        if out <= 0 or material <= 0:
            return {"ok": False, "reason": SCENE_ACTION_TOO_LONG, "detail": {"part": name}}
        parts.append({"phase": name, "speed": s, "speed_range": [rng[0], rng[1]],
                      "source_start_us": start, "source_end_us": end, "segment_us": out})
        cursor = end
    # 倍速吸附到整帧后素材用量会有出入（20 倍时最多差半帧 × 20），动作收在素材末尾时会越界：
    # 先整体前移（倒水前还有余量），不够再把最后一段收短、按原输出时长重算倍速。
    over = cursor - min(scene_out, _material_us(shot))
    if over > 0:
        shift = min(over, max(0, parts[0]["source_start_us"] - scene_in))
        for part in parts:
            part["source_start_us"] -= shift
            part["source_end_us"] -= shift
        cursor, over = cursor - shift, over - shift
    if over > 0:
        last = parts[-1]
        span = last["source_end_us"] - over - last["source_start_us"]
        if span > 0 and span / last["segment_us"] >= last["speed_range"][0] - 1e-9:
            last["source_end_us"] -= over
            last["speed"] = span / last["segment_us"]
            cursor -= over
    if cursor > scene_out + frame or cursor > _material_us(shot):
        return {"ok": False, "reason": SCENE_ACTION_TOO_LONG,
                "detail": {"end_us": cursor, "scene_out_us": scene_out}}
    return {"ok": True, "parts": parts, "timeline_us": timeline,
            "action_us": b3 - b0, "missing_phases": missing}


def slice_plan(plan: dict[str, Any], member_us: list[int], *, fps: float,
               limit_us: Optional[int] = None) -> Optional[list[list[dict[str, Any]]]]:
    """把整组的「倒水 → 吸收 → 擦拭」按句子边界切开，每句拿到同一条素材上连续的一段或几段。

    相位切点离句子切点不到一帧时对齐到句子切点，免得切出不足一帧的碎片。
    素材终点超过 ``limit_us`` 或切不开时返回 None。
    """
    parts = [dict(p) for p in plan.get("parts") or []]
    bounds, acc = [], 0
    for value in member_us:
        acc += int(value)
        bounds.append(acc)
    if not parts or not bounds or acc != sum(int(p["segment_us"]) for p in parts):
        return None
    frame = timing_contract.frame_us(fps)
    edges, acc = [0], 0
    for part in parts[:-1]:
        acc += int(part["segment_us"])
        near = min(bounds[:-1], key=lambda b: abs(b - acc), default=None)
        edges.append(near if near is not None and abs(near - acc) < frame else acc)
    edges.append(bounds[-1])
    if any(b <= a for a, b in zip(edges, edges[1:])):
        return None
    pieces, cursor = [], int(parts[0]["source_start_us"])
    for part, a, b in zip(parts, edges, edges[1:]):
        pieces.append((a, b, cursor, part))
        cursor += int(round(float(part["speed"]) * (b - a)))
    if limit_us is not None and cursor > int(limit_us) + frame:
        return None
    members, lo = [], 0
    for hi in bounds:
        chunks = []
        for a, b, src, part in pieces:
            s, e = max(a, lo), min(b, hi)
            if e <= s:
                continue
            speed = float(part["speed"])
            chunks.append({**part, "source_start_us": src + int(round(speed * (s - a))),
                           "source_end_us": src + int(round(speed * (e - a))), "segment_us": e - s})
        members.append(chunks)
        lo = hi
    return members


def _head_guard_us(manifest: dict[str, Any]) -> int:
    return timing_contract.us_of(manifest.get("head_trim_s"), timing_contract.DEFAULT_HEAD_GUARD_US)


def _tail_guard_us(manifest: dict[str, Any]) -> int:
    return timing_contract.us_of(manifest.get("tail_trim_s"), timing_contract.DEFAULT_TAIL_GUARD_US)


# ── 段落字段 ──────────────────────────────────────────────────────────
_SHOT_FIELDS = ("description", "visual_description", "visual_tags", "evidence_tags",
                "action", "object", "result")


def _shot_fields(shot: dict[str, Any]) -> dict[str, Any]:
    from . import demo_actions
    return {
        "video": shot.get("video"),
        "source_start": shot.get("source_start"),
        "source_end": shot.get("source_end"),
        "duration": shot.get("duration"),
        "material_duration_us": shot.get("material_duration_us"),
        "scene_source_start": shot.get("source_start"),
        "scene_source_end": shot.get("source_end"),
        "temporary_shot_id": shot.get("shot_id"),
        "visual_role": demo_actions.visual_role(shot),
        **{k: shot.get(k) for k in _SHOT_FIELDS},
    }


def _single_update(seg: dict[str, Any], shot: dict[str, Any], solution, guards: list[float], *,
                   speed_range: tuple[float, float], evidence: Optional[tuple[float, float]],
                   swapped: bool) -> dict[str, Any]:
    update: dict[str, Any] = _shot_fields(shot) if swapped else {}
    if evidence is not None:
        intervals = [{"start": round(evidence[0], 3), "end": round(evidence[1], 3),
                      "claim": seg.get("claim_text") or seg.get("text"),
                      "evidence_source": "phase_timing",
                      "frame_time": round((evidence[0] + evidence[1]) / 2.0, 3)}]
    elif swapped:
        from .shot_analyzer import candidate_evidence_interval
        ev_start, ev_end, frame_ts = candidate_evidence_interval(shot)
        intervals = [{"start": ev_start, "end": ev_end, "frame_path": shot.get("frame_path"),
                      "claim": seg.get("claim_text") or seg.get("text"),
                      "evidence_source": "audited_frame_neighborhood", "frame_time": frame_ts}]
    else:
        intervals = seg.get("evidence_intervals")
    update.update({
        "evidence_intervals": intervals,
        "video_speed": float(solution.speed),
        "window_solution": solution.to_dict(),
        "window_reason": "",
        "speed_range_override": [speed_range[0], speed_range[1]],
        "guard_override": guards,
        "head_waste": 0.0, "tail_waste": 0.0,
        "multi_shot": False, "span_parts": None, "span_solution": None,
    })
    return update


def _split_update(seg: dict[str, Any], shot: dict[str, Any], parts: list[dict[str, Any]],
                  plan: dict[str, Any], *, swapped: bool) -> dict[str, Any]:
    update: dict[str, Any] = _shot_fields(shot) if swapped else {}
    span_parts = []
    for index, part in enumerate(parts):
        start_s, end_s = part["source_start_us"] / US, part["source_end_us"] / US
        span_parts.append({
            "ok": True, "speed": part["speed"], "video_speed": part["speed"],
            "source_start": round(start_s, 6), "source_end": round(end_s, 6),
            "segment_s": round(part["segment_us"] / US, 6),
            "material_s": round((part["source_end_us"] - part["source_start_us"]) / US, 6),
            "handle_room_s": 0.0, "reason": "", "detail": {"scene_phase": part["phase"]},
            "shot_id": f"{shot.get('shot_id')}#{part['phase']}",
            "video": shot.get("video"),
            "scene_source_start": shot.get("source_start"),
            "scene_source_end": shot.get("source_end"),
            "speed_range_override": part["speed_range"],
            "evidence_intervals": [{"start": round(start_s, 3), "end": round(end_s, 3),
                                    "claim": seg.get("claim_text") or seg.get("text"),
                                    "evidence_source": "phase_timing",
                                    "frame_time": round((start_s + end_s) / 2.0, 3),
                                    "scene_phase": part["phase"]}],
            "span_index": index,
        })
    first = span_parts[0]
    update.update({
        "multi_shot": True,
        "span_parts": span_parts,
        "span_solution": {"ok": True, "scope": "scene_split_speed",
                          "timeline_s": round(sum(p["segment_us"] for p in parts) / US, 6),
                          "group_timeline_s": round(plan["timeline_us"] / US, 6),
                          "action_s": round(plan["action_us"] / US, 6)},
        "video_speed": first["speed"],
        "window_solution": {k: first[k] for k in ("ok", "speed", "source_start", "source_end",
                                                  "segment_s", "material_s", "handle_room_s",
                                                  "reason", "detail")},
        "evidence_intervals": first["evidence_intervals"],
        "window_reason": "",
        "head_waste": 0.0, "tail_waste": 0.0,
    })
    return update


# ── 主流程 ────────────────────────────────────────────────────────────
def _contract(seg: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
    from . import demo_actions
    text = str(seg.get("text") or "")
    return demo_actions.sentence_contract(
        text, str(seg.get("claim_text") or text), category=str(manifest.get("category") or ""),
        product=str(manifest.get("product") or ""))


def _candidates(seg: dict[str, Any], rec: dict[str, Any], shots_by_id: dict[str, dict],
                used: set[str], contract: Optional[dict[str, Any]] = None,
                others: Optional[dict[str, int]] = None) -> list[tuple[dict[str, Any], bool]]:
    from . import demo_actions
    out: list[tuple[dict[str, Any], bool]] = []
    current = shots_by_id.get(str(seg.get("temporary_shot_id") or ""))
    if current:
        out.append((current, False))
    swaps = []
    for row in (rec or {}).get("top3") or []:
        shot_id = str(row.get("shot_id") or "")
        shot = shots_by_id.get(shot_id)
        if (not shot or shot_id in used or (current and shot_id == current.get("shot_id"))
                or shot.get("status") != "ready_for_matching" or not row.get("eligible", True)
                or (contract and not demo_actions.shot_fit(shot, contract))):
            continue
        swaps.append((shot, True))
    # 展示句换镜时，别的任务用得少的排前面（候选原本按 AI 分排，排序稳定）。
    swaps.sort(key=lambda item: _other_uses(item[0], contract, others))
    return out + swaps


def _source_budget(manifest: dict[str, Any]) -> int:
    """整稿最多几个素材文件：选镜上限和本机容量校准的安全上限取小的（引擎按后者拦）。"""
    limit = max(1, int(manifest.get("max_final_materials", 24) or 24))
    try:
        from . import capacity
        safe = int((capacity.load_profile() or {}).get("safe_source_limit") or 0)
    except Exception:  # noqa: BLE001 —— 没校准过就只按选镜上限
        safe = 0
    return min(limit, safe) if safe > 0 else limit


def _source_clash(segments: list, position: int, video: str, budget: int) -> str:
    """换上这个素材会不会和别的句子同名（素材库里同一文件常有几份拷贝），或让整稿素材数超上限。"""
    others = [s for i, s in enumerate(segments)
              if i != position and isinstance(s, dict) and s.get("video") and not s.get("visual_missing")]
    if Path(video).name.casefold() in {Path(str(s["video"])).name.casefold() for s in others}:
        return SCENE_SOURCE_DUPLICATE
    sources = {str(s["video"]).casefold() for s in others} | {video.casefold()}
    placeholder = any(isinstance(s, dict) and s.get("visual_missing") for s in segments)
    if len(sources) + int(placeholder) > budget:
        return SCENE_SOURCE_BUDGET
    return ""


def _other_uses(shot: dict[str, Any], contract: Optional[dict[str, Any]],
                others: Optional[dict[str, int]]) -> int:
    """展示句（非效果演示）里这个镜头被别的任务用过几次；演示句不计。"""
    from . import demo_actions, usage_ledger
    if not others or demo_actions.is_demo_contract(contract):
        return 0
    return others.get(usage_ledger.shot_key(shot.get("video"), shot.get("source_start")), 0)


def _pool_fill(seg: dict[str, Any], rules: dict[str, Any], shots_by_id: dict[str, dict],
               used: set[str], segments: list, limit: int = 8,
               contract: Optional[dict[str, Any]] = None,
               others: Optional[dict[str, int]] = None) -> list[tuple[dict[str, Any], bool]]:
    """带动作的镜头放不完整、候选也用完时，从素材池找没用过、不带待完成动作的镜头。

    功能句（抽取、不破、湿度、擦拭、冲散……）只取画面主题对得上的，不管是不是补位句；
    展示句里，补位句（``degraded``）任何镜头都可以，有明确意思的只取画面和文案对得上的
    （报品牌的句子算产品展示镜头对得上）。跳过已经进成片的镜头和素材文件；开了相似画面去重时，
    同机位同动作的排后面；展示句里别的任务用得少的靠前；产品展示优先。
    """
    from . import demo_actions
    from .shot_analyzer import _candidate_score, _look_key
    text = str(seg.get("text") or "")
    claim = str(seg.get("claim_text") or text)
    contract = contract or {}
    functional = contract.get("kind") == "functional"
    category = str(contract.get("category") or "")
    related_only = "degraded" not in str(seg.get("selection_mode") or "")
    intents = set(demo_actions.infer_intents(text, claim, category))
    wants_quantity = bool({"cta", "quantity"} & intents)
    taken = [shots_by_id.get(str(s.get("temporary_shot_id") or "")) or {}
             for s in segments if isinstance(s, dict)]
    videos = {str(s.get("video") or "") for s in segments if isinstance(s, dict) and s.get("video")}
    looks = {_look_key(s) for s in taken if _look_key(s)}
    rows = []
    for shot_id, shot in shots_by_id.items():
        if (shot_id in used or str(shot.get("video") or "") in videos
                or shot.get("status") != "ready_for_matching" or shot.get("action_complete") is not True
                or demo_actions.is_empty_shot(shot) or rulepack.scene_for(rules, text, shot) is not None):
            continue
        fit = demo_actions.shot_fit(shot, contract) if contract else 1
        if functional and not fit:
            continue
        matched = len(_candidate_score(claim, shot)[1])
        if wants_quantity and demo_actions.cta_quantity_eligible(shot, text, category)[0]:
            matched += 1
        if "brand" in intents and demo_actions.visual_role(shot) == "product_display":
            matched += 1
        if related_only and not functional and not matched:
            continue
        similar = bool(rules.get("avoid_similar_shots")) and _look_key(shot) in looks
        rows.append((similar, -fit, -matched, _other_uses(shot, contract, others),
                     demo_actions.visual_role(shot) != "product_display", shot_id, shot))
    rows.sort(key=lambda row: row[:6])
    return [(row[6], True) for row in rows[:limit]]


def _visual_candidates(scene: dict[str, Any], candidates: list[tuple[dict[str, Any], bool]],
                       shots_by_id: dict[str, dict], used: set[str], segments: list, position: int,
                       contract: Optional[dict[str, Any]] = None) -> list[tuple[dict[str, Any], bool]]:
    """文案强制要动作画面的规则（拆包）：只留带撕开/取出动作的换镜候选，并从素材池补几条。

    排序：同一条素材拆完接着展开看面层的最前，其次其他带动作的，原镜头若只是袋面静态排最后
    （留着它只为在报告里记下为什么换掉）。素材池按 ``pool_search_limit``（默认 8）取，
    跳过已进成片的镜头和别的句子已经用了的素材文件。
    """
    from . import demo_actions
    seen = {str(shot.get("shot_id")) for shot, _swapped in candidates}
    videos = {str(s.get("video") or "").casefold() for i, s in enumerate(segments)
              if i != position and isinstance(s, dict) and s.get("video")}
    pool = []
    for shot_id, shot in shots_by_id.items():
        if (shot_id in seen or shot_id in used or shot.get("status") != "ready_for_matching"
                or str(shot.get("video") or "").casefold() in videos
                or not rulepack.shot_has_visual(scene, shot)
                or (contract and demo_actions.contradiction(shot, contract))):
            continue
        pool.append((not rulepack.shot_extends(scene, shot), -rulepack.shot_keyword_hits(scene, shot),
                     -_number(shot.get("duration")), shot_id, shot))
    pool.sort(key=lambda row: row[:4])
    limit = max(0, int(scene.get("pool_search_limit", 8) or 0))
    merged = [(shot, swapped) for shot, swapped in candidates
              if not swapped or rulepack.shot_has_visual(scene, shot)]
    merged += [(row[4], True) for row in pool[:limit]]

    def rank(item: tuple[dict[str, Any], bool]) -> tuple[bool, bool, bool]:
        shot, swapped = item
        visual = rulepack.shot_has_visual(scene, shot)
        return (not visual, not (visual and rulepack.shot_extends(scene, shot)), swapped)

    return sorted(merged, key=rank)


def _plain_swap(seg: dict[str, Any], shot: dict[str, Any], *, manifest: dict[str, Any],
                audio_us: int, fill: bool = False) -> dict[str, Any]:
    """换成候选镜头，窗口按常规选镜口径求解（不加场景倍速和保护区覆盖）。

    ``fill``：补位镜不当卖点证据，跟预览补镜一样只罩住审核帧，罩不住再按画面长度摆。
    """
    from .shot_analyzer import candidate_evidence_interval, candidate_window, narrow_evidence
    probes: list[tuple[dict[str, Any], Optional[dict[str, Any]]]] = [(shot, None)]
    if fill:
        _start, _end, frame_ts = candidate_evidence_interval(shot)
        focus = {"start": max(_number(shot.get("source_start")), frame_ts - 0.034),
                 "end": min(_number(shot.get("source_end"), frame_ts), frame_ts + 0.034),
                 "frame_time": frame_ts}
        probes = [({**shot, "evidence_intervals": [focus]}, focus), (shot, None)]
    solution, focused = None, None
    for index, (probe, focus) in enumerate(probes):
        solution = candidate_window(probe, audio_duration_us=audio_us, manifest=manifest,
                                    time_sensitive=bool(seg.get("time_sensitive")),
                                    **({"cover_evidence": index == 0} if fill else {}))
        if solution.ok:
            focused = focus
            break
    if (not solution.ok and not seg.get("time_sensitive")
            and solution.reason == timing_contract.REASON_EVIDENCE_SPAN_TOO_LONG):
        narrowed = narrow_evidence(shot, (solution.detail or {}).get("max_window_us"))
        if narrowed is not None:
            retry = candidate_window(narrowed, audio_duration_us=audio_us, manifest=manifest,
                                     time_sensitive=False)
            if retry.ok:
                shot, solution = narrowed, retry
    if not solution.ok:
        return {"ok": False, "reason": solution.reason}
    lo, hi = timing_contract.speed_range_for_role(
        _shot_fields(shot)["visual_role"], time_sensitive=bool(seg.get("time_sensitive")))
    update = _single_update(seg, shot, solution, [
        round(_head_guard_us(manifest) / US, 6), round(_tail_guard_us(manifest) / US, 6)],
        speed_range=(lo, hi), evidence=None, swapped=True)
    for key in ("guard_override", "speed_range_override"):
        update.pop(key, None)
    update["head_waste"], update["tail_waste"] = shot.get("head_waste"), shot.get("tail_waste")
    if focused is not None:
        update["evidence_intervals"][0].update(
            {"start": round(focused["start"], 3), "end": round(focused["end"], 3)})
    if fill:
        update.update({"degraded_no_match": True, "action_complete": True})
    return {"ok": True, "update": update}


def _settled_plan(seg: dict[str, Any], shot: dict[str, Any], action_end: float, *, audio_us: int,
                  manifest: dict[str, Any], speed_range: tuple[float, float],
                  swapped: bool) -> Optional[dict[str, Any]]:
    """整段动作塞不进这句时，只用动作做完、产品已经定住之后的画面，不切在动作中途。"""
    start = max(_number(shot.get("source_start")), action_end)
    return _outside_plan(seg, shot, {"source_start": start}, "settled", audio_us=audio_us,
                         manifest=manifest, speed_range=speed_range, swapped=swapped)


def _outside_action(scene: dict[str, Any], seg: dict[str, Any], shot: dict[str, Any],
                    span: list[float], *, audio_us: int, manifest: dict[str, Any],
                    speed_range: tuple[float, float], swapped: bool) -> Optional[dict[str, Any]]:
    kw = {"audio_us": audio_us, "manifest": manifest, "speed_range": speed_range, "swapped": swapped}
    plan = _settled_plan(seg, shot, span[1], **kw)
    if plan is None and scene.get("lead_in_fallback"):
        plan = _lead_in_plan(seg, shot, span[0], **kw)
    return plan


def _lead_in_plan(seg: dict[str, Any], shot: dict[str, Any], action_start: float, *,
                  audio_us: int, manifest: dict[str, Any], speed_range: tuple[float, float],
                  swapped: bool) -> Optional[dict[str, Any]]:
    """做完之后也没有定住的画面时，用动作开始之前的画面，同样不切在动作中途。"""
    end = min(_number(shot.get("source_end")), action_start)
    return _outside_plan(seg, shot, {"source_end": end}, "lead_in", audio_us=audio_us,
                         manifest=manifest, speed_range=speed_range, swapped=swapped)


def _outside_plan(seg: dict[str, Any], shot: dict[str, Any], bounds: dict[str, float], mode: str, *,
                  audio_us: int, manifest: dict[str, Any], speed_range: tuple[float, float],
                  swapped: bool) -> Optional[dict[str, Any]]:
    clipped = {**shot, **bounds}
    if _number(clipped.get("source_end")) - _number(clipped.get("source_start")) <= 0:
        return None
    solution, guards = solve_single(clipped, audio_us=audio_us, manifest=manifest,
                                    speed_range=speed_range)
    if not solution.ok:
        return None
    window = (solution.source_start_us / US, solution.source_end_us / US)
    update = _single_update(seg, shot, solution, guards, speed_range=speed_range,
                            evidence=window, swapped=swapped)
    update["evidence_intervals"][0]["scene_phase"] = mode
    return {"ok": True, "update": update, "mode": mode}


def _segment_phases(scene: dict[str, Any], shot: dict[str, Any],
                    contract: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """视觉分析已按时间切好的动作分段里，取和这条规则、这句话主题都对得上的那一段。

    一个镜头常先翻盖、再抽纸、再合盖；「单手一抽」只需要把抽纸那段放完整，
    不必把整条镜头的动作都塞进这句话。分段边界来自稀疏抽帧，两端各外扩半个抽帧间隔。
    """
    from . import demo_actions
    from .vision_analyzer import SAMPLE_STEP_S, segment_completed
    phases = scene.get("phases") or []
    rows = [row for row in shot.get("action_segments") or [] if isinstance(row, dict)]
    if scene.get("kind") != "full_window" or not phases or not rows:
        return None

    def row_text(row: dict[str, Any]) -> str:
        return rulepack.strip_shot_ignore(scene, f"{row.get('action') or ''}{row.get('result') or ''}")

    extend_words = [w for w in scene.get("extend_keywords") or [] if w]
    if (contract or {}).get("topics") and not extend_words:
        # 功能句只认演这句主题的那段；找不到就交给相位打点，不拿别的动作顶替。
        hits = demo_actions.topic_segments(shot, contract)
    else:
        # 拆包句即使同时讲面层，也要先盖住撕开/取出那段，不能只放面层或袋面静态。
        scene_words = [w for w in scene.get("shot_keywords") or [] if w]
        hits = [row for row in rows if any(w in row_text(row) for w in scene_words)]
    # 只有写明结果的段才算做完；进行中的段交给相位打点去认证，不在这里造 complete。
    hits = [row for row in hits if segment_completed(row)]
    if not hits:
        return None
    pad = SAMPLE_STEP_S / 2.0
    start, end = _number(hits[0].get("start")), _number(hits[0].get("end"))
    action_end = end
    if extend_words:
        # 同一条素材拆完紧接着展开看面层：窗口连着放到面层展示；放不下时 tight 只留拆包段。
        for row in sorted(rows, key=lambda r: _number(r.get("start"))):
            row_start, row_end = _number(row.get("start")), _number(row.get("end"))
            if row_start < start or row_end <= end:
                continue
            if row_start > end + 2 * pad or not (row in hits or any(w in row_text(row) for w in extend_words)):
                break
            end = row_end
    lo = max(_number(shot.get("source_start")), start - pad)
    hi = min(_number(shot.get("source_end")), end + pad)
    if hi <= lo:
        return None
    return {"ok": True, "complete": True, "phases": {phases[0]["name"]: [round(lo, 3), round(hi, 3)]},
            "tight": [round(max(lo, start), 3), round(min(hi, action_end), 3)],
            "source": "action_segments"}


def _plan_for(scene: dict[str, Any], seg: dict[str, Any], shot: dict[str, Any], *,
              swapped: bool, manifest: dict[str, Any], report_dir: Path,
              locate: Callable[..., dict], settled_ok: bool = False,
              contract: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    kind = scene.get("kind")
    audio_us = int(seg.get("audio_duration_us") or 0)
    if audio_us <= 0:
        return {"ok": False, "reason": timing_contract.REASON_NO_AUDIO}
    speed = scene.get("speed") or {}
    if kind == "require_visual":
        if not rulepack.shot_has_visual(scene, shot):
            return {"ok": False, "reason": SCENE_VISUAL_MISSING}
        if not swapped:
            return {"ok": True, "update": {}}
        return _plain_swap(seg, shot, manifest=manifest, audio_us=audio_us)
    if scene.get("text_forces_visual") and not rulepack.shot_has_visual(scene, shot):
        # 拆包句选到的只是袋面静态：不送相位打点，直接换下一个候选。
        return {"ok": False, "reason": SCENE_VISUAL_MISSING}
    if (swapped and not scene.get("text_keywords")
            and rulepack.scene_for({"scenes": [scene]}, str(seg.get("text") or ""), shot) is None):
        # 只由画面触发的规则（正面指产品）：换上来的镜头本身不含该动作，规则就不适用。
        return _plain_swap(seg, shot, manifest=manifest, audio_us=audio_us)
    if kind == "single_shot":
        rng = _range(speed.get("range"), (0.85, 1.2))
        if not swapped and not seg.get("multi_shot") and rng[0] - 1e-6 <= _number(
                seg.get("video_speed"), 1.0) <= rng[1] + 1e-6:
            return {"ok": True, "update": {}}
        solution, guards = solve_single(shot, audio_us=audio_us, manifest=manifest,
                                        speed_range=rng)
        if not solution.ok:
            return {"ok": False, "reason": SCENE_SINGLE_SHOT_INFEASIBLE,
                    "detail": {"solver": solution.reason}}
        return {"ok": True, "update": _single_update(
            seg, shot, solution, guards, speed_range=rng, evidence=None, swapped=swapped)}
    located = _segment_phases(scene, shot, contract) or locate(shot, scene, report_dir)
    if not located.get("ok"):
        return {"ok": False, "reason": SCENE_PHASE_UNVERIFIED, "soft": True,
                "detail": {"phase": located.get("reason")}}
    phases = located.get("phases") or {}
    if not located.get("complete"):
        name = (scene.get("phases") or [{}])[0].get("name")
        if settled_ok and kind == "full_window" and phases.get(name):
            # 动作开头没拍全，但做完之后定住的画面还在。
            settled = _outside_action(scene, seg, shot, phases[name], audio_us=audio_us,
                                      manifest=manifest,
                                      speed_range=_range(speed.get("range"), (1.0, 1.6)),
                                      swapped=swapped)
            if settled is not None:
                return {**settled, "phases": phases}
        return {"ok": False, "reason": SCENE_ACTION_INCOMPLETE, "phases": phases}
    if kind == "full_window":
        name = (scene.get("phases") or [{}])[0].get("name")
        span = phases.get(name)
        if not span:
            return {"ok": False, "reason": SCENE_ACTION_INCOMPLETE}
        hold = _number(scene.get("hold_after_s"))
        end = min(_number(shot.get("source_end")), span[1] + hold)
        evidence = (span[0], end)
        rng = _range(speed.get("range"), (1.0, 1.6))
        solution, guards = solve_single(shot, audio_us=audio_us, manifest=manifest,
                                        speed_range=rng, evidence=evidence)
        tight = located.get("tight")
        if not solution.ok and tight and solution.reason in _TOO_LONG:
            # 外扩是给稀疏抽帧的边界留余量；放不下时用分段本身的起止再试一次。
            evidence = (tight[0], min(_number(shot.get("source_end")), tight[1] + hold))
            solution, guards = solve_single(shot, audio_us=audio_us, manifest=manifest,
                                            speed_range=rng, evidence=evidence)
        if not solution.ok:
            reason = (SCENE_ACTION_TOO_LONG if solution.reason in _TOO_LONG
                      else solution.reason)
            if reason == SCENE_ACTION_TOO_LONG and settled_ok:
                settled = _outside_action(scene, seg, shot, span, audio_us=audio_us,
                                          manifest=manifest, speed_range=rng, swapped=swapped)
                if settled is not None:
                    return {**settled, "phases": phases}
            return {"ok": False, "reason": reason, "detail": solution.detail, "phases": phases}
        return {"ok": True, "phases": phases, "update": _single_update(
            seg, shot, solution, guards, speed_range=rng, evidence=evidence, swapped=swapped)}
    return {"ok": False, "reason": "SCENE_KIND_UNKNOWN"}


# ── 吸水实验 / 拆包按句组处理 ─────────────────────────────────────────
def _scene_uses_groups(scene: dict[str, Any]) -> bool:
    """``split_speed`` 始终组句；其它场景（如拆包 full_window）配置了 ``group_*`` 才组句。"""
    if scene.get("kind") == "split_speed":
        return True
    return any(k in scene for k in ("group_bridge", "group_min_s", "group_extend_max", "group_max"))


def _split_groups(segments: list, rules: dict[str, Any],
                  shots_by_id: dict[str, dict], *,
                  category: str = "") -> list[dict[str, Any]]:
    """连续几句都在讲同一场景动作时，合起来演一遍完整动作，而不是每句各演一遍。

    吸水：文案提到吸水类卖点就要用实验画面；``trigger`` 记录组内是否已有选中镜头本身
    就是实验画面。拆包：完整撕开/取出动作放不进单句时，联系上下句连续播同一条素材。

    紧跟在组后的效果句（``follow_keywords``）续进组里，它们自己不开组。有画面缺失的句子
    把组切断。组里夹着不超过 ``group_bridge`` 句别的话、后面还在讲同一场景时一起并进来；
    整组配音仍短于 ``group_min_s`` 时，顺延后面相邻、没被其他场景（荧光/面层/促销等）占用的
    句子，最多 ``group_extend_max`` 句。还没用完的顺延名额记在 ``spare``：所有候选都因动作
    太长放不下时，再逐句借进来重试。单句配音已够 ``group_min_s`` 时不硬并邻居。
    """
    groups: list[dict[str, Any]] = []
    scenes = list(rules.get("scenes") or [])
    for scene in scenes:
        if not _scene_uses_groups(scene):
            continue
        follow = {"text_keywords": scene.get("follow_keywords") or []}
        others = [s for s in scenes if s is not scene]
        bridge = max(0, int(scene.get("group_bridge", 0) or 0))
        min_us = _us(scene.get("group_min_s", 0))
        extend_max = max(0, int(scene.get("group_extend_max", 0) or 0))
        max_n = max(0, int(scene.get("group_max", 0) or 0))

        def usable(seg: Any) -> bool:
            return isinstance(seg, dict) and not seg.get("visual_missing") and bool(seg.get("video"))

        def role(seg: Any) -> str:
            if not usable(seg):
                return ""
            if rulepack.text_hits_scene(scene, seg.get("text")):
                return "hit"
            if any(rulepack.text_hits_scene(other, seg.get("text")) for other in others):
                return ""
            return "follow" if rulepack.text_hits_scene(follow, seg.get("text")) else ""

        def free(seg: Any) -> bool:
            # 自己有要证明的卖点（量大锁中央、透气……）时不能当桥接句并进组，
            # 否则会跟着播倒水段，画面和那句证据对不上。
            if not usable(seg) or any(rulepack.text_hits_scene(o, seg.get("text")) for o in others):
                return False
            from . import demo_actions
            text, claim = str(seg.get("text") or ""), str(seg.get("claim_text") or "")
            contract = demo_actions.sentence_contract(text, claim, category=category)
            return (contract.get("kind") != "functional"
                    and not ({"cta", "quantity", "brand"} & set(contract.get("intents") or []))
                    and not any(c.evidence_required for c in demo_actions.requirements_for(
                        text, claim, category)))

        count = len(segments)
        position = 0
        while position < count:
            if role(segments[position]) != "hit":
                position += 1
                continue
            run = [position]
            cursor = position + 1
            while cursor < count and (not max_n or len(run) < max_n):
                if role(segments[cursor]):
                    run.append(cursor)
                    cursor += 1
                    continue
                ahead = next((j for j in range(cursor + 1, min(count, cursor + bridge + 1))
                              if role(segments[j])), None)
                if ahead is None or (max_n and ahead - position + 1 > max_n):
                    break
                if not all(free(segments[j]) for j in range(cursor, ahead)):
                    break
                run.extend(range(cursor, ahead + 1))
                cursor = ahead + 1
            if max_n and len(run) > max_n:
                run = run[:max_n]
                cursor = run[-1] + 1
            audio = [int(segments[p].get("audio_duration_us") or 0) for p in run]
            extended = 0
            while (min_us and all(audio) and sum(audio) < min_us and extended < extend_max
                   and (not max_n or len(run) < max_n)
                   and cursor < count and free(segments[cursor])
                   and int(segments[cursor].get("audio_duration_us") or 0) > 0):
                run.append(cursor)
                audio.append(int(segments[cursor]["audio_duration_us"]))
                cursor += 1
                extended += 1
            spare, probe = [], cursor
            while (extended + len(spare) < extend_max and (not max_n or len(run) + len(spare) < max_n)
                   and probe < count and free(segments[probe])
                   and not role(segments[probe])
                   and int(segments[probe].get("audio_duration_us") or 0) > 0):
                spare.append(probe)
                probe += 1
            # 选中镜头本身是否已有该场景画面（拆包 text_forces 时 scene_for 只看文案，不能用来判 trigger）。
            hit = any(
                rulepack.shot_has_visual(
                    scene, shots_by_id.get(str(segments[p].get("temporary_shot_id") or "")) or {})
                for p in run if role(segments[p]) == "hit")
            # full_window 单句无邻句可借时继续走普通单句入口，避免重复占组。
            if (len(run) > 1 or spare
                    or scene.get("id") not in {"fluorescence", "ultra_thin"}):
                groups.append({"scene": scene, "positions": run, "spare": spare,
                               "trigger": "shot" if hit else "text"})
            position = cursor
    return groups


def _group_candidates(scene: dict[str, Any], members: list[dict[str, Any]], positions: list[int],
                      recs: dict[int, dict], shots_by_id: dict[str, dict],
                      used: set[str]) -> list[tuple[dict[str, Any], str]]:
    """``[(镜头, 来源)]``：组内各句选中的 → 各句 AI 前三 → 素材池里画面像实验的。

    AI 给吸水句挑的是「吸收效果」或卖点画面，完整拍了倒水→吸收→擦拭的素材常不在前三，
    所以再按场景画面关键词从整池补几条（`pool_search_limit`，默认 8，按命中词数、时长排）。
    画面描述里没有任何实验关键词的镜头不送去相位打点。
    """
    own = {str(m.get("temporary_shot_id") or "") for m in members}
    seen: set[str] = set()
    out: list[tuple[dict[str, Any], str]] = []
    for member in members:
        shot = shots_by_id.get(str(member.get("temporary_shot_id") or ""))
        if shot and shot["shot_id"] not in seen and rulepack.shot_has_visual(scene, shot):
            seen.add(shot["shot_id"])
            out.append((shot, "selected"))
    for position in positions:
        for row in (recs.get(position + 1) or {}).get("top3") or []:
            shot_id = str(row.get("shot_id") or "")
            shot = shots_by_id.get(shot_id)
            if (not shot or shot_id in seen or (shot_id in used and shot_id not in own)
                    or shot.get("status") != "ready_for_matching" or not row.get("eligible", True)
                    or not rulepack.shot_has_visual(scene, shot)):
                continue
            seen.add(shot_id)
            out.append((shot, "top3"))
    pool = []
    for shot_id, shot in shots_by_id.items():
        if (shot_id in seen or (shot_id in used and shot_id not in own)
                or shot.get("status") != "ready_for_matching"):
            continue
        hits = rulepack.shot_keyword_hits(scene, shot)
        if hits:
            # 拆包：同一条素材拆完接着展面层的排最前（与单句 _visual_candidates 一致）。
            pool.append((not rulepack.shot_extends(scene, shot), -hits,
                         _number(shot.get("duration")), shot_id, shot))
    pool.sort(key=lambda row: row[:4])
    limit = max(0, int(scene.get("pool_search_limit", 8) or 0))
    out.extend((row[4], "pool_search") for row in pool[:limit])
    if scene.get("extend_keywords"):
        out.sort(key=lambda item: (not rulepack.shot_extends(scene, item[0]),
                                   item[1] != "selected", item[1] != "top3"))
    # 先试更短且完整的实验，避免 selected 的几十秒静置尾抢走池里几秒就演完的素材。
    # 后续仍逐条验证相位完整与窗口可解，短片本身不获得通过资格。
    out.sort(key=lambda item: (
        not rulepack.shot_extends(scene, item[0]) if scene.get("extend_keywords") else False,
        _number(item[0].get("duration"), float("inf")),
        item[1] != "selected", str(item[0].get("shot_id") or "")))
    return out


def _plan_full_window_group(scene: dict[str, Any], members: list[dict[str, Any]],
                            shot: dict[str, Any], *, manifest: dict[str, Any],
                            report_dir: Path, locate: Callable[..., dict]) -> dict[str, Any]:
    """拆包等 full_window：整组配音连续播完同一条素材上的完整动作，再按句切开。"""
    audio = [int(m.get("audio_duration_us") or 0) for m in members]
    if min(audio) <= 0:
        return {"ok": False, "reason": timing_contract.REASON_NO_AUDIO}
    if scene.get("text_forces_visual") and not rulepack.shot_has_visual(scene, shot):
        return {"ok": False, "reason": SCENE_VISUAL_MISSING}
    located = _segment_phases(scene, shot, None) or locate(shot, scene, report_dir)
    if not located.get("ok"):
        return {"ok": False, "reason": SCENE_PHASE_UNVERIFIED, "soft": True,
                "detail": {"phase": located.get("reason")}}
    phases = located.get("phases") or {}
    if not located.get("complete"):
        return {"ok": False, "reason": SCENE_ACTION_INCOMPLETE, "phases": phases}
    name = (scene.get("phases") or [{}])[0].get("name")
    span = phases.get(name)
    if not span:
        return {"ok": False, "reason": SCENE_ACTION_INCOMPLETE, "phases": phases}
    pad = _tail_pad_us(manifest)
    total_us = sum(audio) + pad * max(0, len(audio) - 1)
    hold = _number(scene.get("hold_after_s"))
    evidence = (span[0], min(_number(shot.get("source_end")), span[1] + hold))
    rng = _range((scene.get("speed") or {}).get("range"), (1.0, 1.6))
    solution, guards = solve_single(shot, audio_us=total_us, manifest=manifest,
                                    speed_range=rng, evidence=evidence)
    tight = located.get("tight")
    if not solution.ok and tight and solution.reason in _TOO_LONG:
        evidence = (tight[0], min(_number(shot.get("source_end")), tight[1] + hold))
        solution, guards = solve_single(shot, audio_us=total_us, manifest=manifest,
                                        speed_range=rng, evidence=evidence)
    if not solution.ok:
        reason = (SCENE_ACTION_TOO_LONG if solution.reason in _TOO_LONG
                  else solution.reason)
        return {"ok": False, "reason": reason, "detail": solution.detail, "phases": phases}
    # 句间 pad 计入时间轴；按各句时间轴长度切连续源区间（末句吸收舍入误差）。
    timeline = [a + (pad if i < len(audio) - 1 else 0) for i, a in enumerate(audio)]
    speed = float(solution.speed)
    cursor = int(solution.source_start_us)
    end_us = int(solution.source_end_us)
    updates = []
    for index, (member, piece_us) in enumerate(zip(members, timeline)):
        piece_end = end_us if index == len(members) - 1 else min(
            end_us, cursor + int(round(speed * piece_us)))
        if piece_end <= cursor:
            return {"ok": False, "reason": SCENE_ACTION_TOO_LONG, "phases": phases,
                    "detail": {"slice": "SCENE_SLICE_FAILED"}}
        piece_evidence = (cursor / US, piece_end / US)
        piece_sol, piece_guards = solve_single(
            shot, audio_us=audio[index], manifest=manifest,
            speed_range=(speed, speed), evidence=piece_evidence)
        # 连续切段时源区间按倍速四舍五入，可能比句长多 1µs，收 1µs 再试。
        if (not piece_sol.ok and piece_sol.reason in _TOO_LONG and piece_end > cursor + 1):
            piece_evidence = (cursor / US, (piece_end - 1) / US)
            piece_sol, piece_guards = solve_single(
                shot, audio_us=audio[index], manifest=manifest,
                speed_range=(speed, speed), evidence=piece_evidence)
        if not piece_sol.ok:
            reason = (SCENE_ACTION_TOO_LONG if piece_sol.reason in _TOO_LONG
                      else piece_sol.reason)
            return {"ok": False, "reason": reason, "detail": piece_sol.detail,
                    "phases": phases}
        swapped = str(member.get("temporary_shot_id") or "") != str(shot.get("shot_id"))
        update = _single_update(member, shot, piece_sol, piece_guards,
                                speed_range=rng, evidence=piece_evidence, swapped=swapped)
        update["evidence_intervals"][0]["scene_phase"] = name
        updates.append(update)
        cursor = piece_end
    return {"ok": True, "updates": updates, "phases": phases, "missing_phases": [],
            "plan": [{"phase": name, "speed": speed,
                      "segment_s": round(total_us / US, 3)}]}

def _plan_group(scene: dict[str, Any], members: list[dict[str, Any]], shot: dict[str, Any], *,
                manifest: dict[str, Any], report_dir: Path,
                locate: Callable[..., dict]) -> dict[str, Any]:
    if scene.get("kind") == "full_window":
        return _plan_full_window_group(scene, members, shot, manifest=manifest,
                                       report_dir=report_dir, locate=locate)
    audio = [int(m.get("audio_duration_us") or 0) for m in members]
    if min(audio) <= 0:
        return {"ok": False, "reason": timing_contract.REASON_NO_AUDIO}
    located = locate(shot, scene, report_dir)
    if not located.get("ok"):
        return {"ok": False, "reason": SCENE_PHASE_UNVERIFIED, "soft": True,
                "detail": {"phase": located.get("reason")}}
    phases = located.get("phases") or {}
    optional = set(scene.get("optional_phases") or [])
    required = [p.get("name") for p in scene.get("phases") or [] if p.get("name") not in optional]
    if not located.get("complete") and any(name not in phases for name in required):
        return {"ok": False, "reason": SCENE_ACTION_INCOMPLETE, "phases": phases}
    pad = _tail_pad_us(manifest)
    plan = plan_split(shot, phases, scene, audio_us=sum(audio) + pad * (len(audio) - 1),
                      manifest=manifest)
    if not plan.get("ok"):
        return {**plan, "phases": phases}
    limit = min(_us(shot.get("source_end")), _material_us(shot))
    sliced = slice_plan(plan, [a + pad for a in audio], fps=_fps(manifest), limit_us=limit)
    if sliced is None:
        return {"ok": False, "reason": SCENE_ACTION_TOO_LONG, "phases": phases,
                "detail": {"slice": "SCENE_SLICE_FAILED"}}
    updates = []
    for member, audio_us, chunks in zip(members, audio, sliced):
        swapped = str(member.get("temporary_shot_id") or "") != str(shot.get("shot_id"))
        if len(chunks) >= 2:
            updates.append(_split_update(member, shot, chunks, plan, swapped=swapped))
            continue
        chunk = chunks[0]
        speed = float(chunk["speed"])
        evidence = (chunk["source_start_us"] / US + 0.001, chunk["source_end_us"] / US - 0.001)
        solution, guards = solve_single(shot, audio_us=audio_us, manifest=manifest,
                                        speed_range=(speed, speed), evidence=evidence)
        if not solution.ok:
            return {"ok": False, "reason": solution.reason, "detail": solution.detail,
                    "phases": phases}
        update = _single_update(member, shot, solution, guards, speed_range=(speed, speed),
                                evidence=evidence, swapped=swapped)
        update["evidence_intervals"][0]["scene_phase"] = chunk["phase"]
        updates.append(update)
    return {"ok": True, "updates": updates, "phases": phases,
            "missing_phases": plan.get("missing_phases") or [],
            "plan": [{"phase": p["phase"], "speed": p["speed"],
                      "segment_s": round(p["segment_us"] / US, 3)} for p in plan["parts"]]}


def _apply_group(group: dict[str, Any], segments: list, *, recs: dict[int, dict],
                 shots_by_id: dict[str, dict], used: set[str], manifest: dict[str, Any],
                 report_dir: Path, locate: Callable[..., dict], preview: bool) -> dict[str, Any]:
    scene, positions = group["scene"], list(group["positions"])
    spare = list(group.get("spare") or [])
    borrowed: list[int] = []
    while True:
        members = [segments[p] for p in positions]
        attempts, applied = [], None
        for shot, origin in _group_candidates(scene, members, positions, recs, shots_by_id, used):
            try:
                result = _plan_group(scene, members, shot, manifest=manifest,
                                     report_dir=report_dir, locate=locate)
            except Exception as exc:  # noqa: BLE001 —— 规则失败不拦整稿，如实记录
                result = {"ok": False, "reason": f"SCENE_RULE_ERROR:{type(exc).__name__}",
                          "detail": {"error": str(exc)[:300]}}
            attempts.append({"shot_id": shot.get("shot_id"), "source": origin,
                             "swapped": shot.get("shot_id") != members[0].get("temporary_shot_id"),
                             "ok": bool(result.get("ok")), "reason": result.get("reason", ""),
                             "detail": result.get("detail"), "phases": result.get("phases"),
                             "missing_phases": result.get("missing_phases") or None})
            if result.get("ok") and not applied:
                applied = (shot, result)
            if result.get("ok") and not result.get("missing_phases"):
                applied = (shot, result)
                break
        if applied or not spare or not any(a["reason"] == SCENE_ACTION_TOO_LONG for a in attempts):
            break
        borrowed.append(spare[0])
        positions.append(spare.pop(0))
    if not applied and borrowed:
        positions = positions[:-len(borrowed)]
        members = [segments[p] for p in positions]
        borrowed = []
    group["positions"] = positions
    indices = [p + 1 for p in positions]
    row = {"index": indices[0], "indices": indices,
           **({"borrowed": [p + 1 for p in borrowed]} if borrowed else {}),
           "text": " / ".join(str(m.get("text") or "") for m in members),
           "scene": scene.get("id"), "scene_label": scene.get("label"),
           "trigger": group.get("trigger", "shot"),
           "requirement": scene.get("requirement"), "attempts": attempts}
    if applied:
        shot, result = applied
        missing = result.get("missing_phases") or []
        labels = {p.get("name"): p.get("label") for p in scene.get("phases") or []}
        status = "partial" if missing else "applied"
        extra = ({"reason": f"{SCENE_PHASE_MISSING}:{','.join(missing)}",
                  "operator_note": f"素材里没有{'、'.join(labels.get(n) or n for n in missing)}画面，"
                                   "只演了前面几段，请补拍后重跑"} if missing else {})
        for order, (position, member, update) in enumerate(zip(positions, members, result["updates"])):
            swapped = str(member.get("temporary_shot_id") or "") != str(shot.get("shot_id"))
            member.update(update)
            member["scene_rule"] = {"id": scene.get("id"), "label": scene.get("label"),
                                    "status": status, "swapped": swapped,
                                    "shot_id": shot.get("shot_id"), "phases": result.get("phases"),
                                    "group": indices, "group_order": order, **extra}
            if swapped:
                member["selection_mode"] = f"{member.get('selection_mode') or 'auto'}+scene_swap"
        used.add(str(shot.get("shot_id")))
        row.update({"status": status, "shot_id": shot.get("shot_id"), "plan": result.get("plan"),
                    **extra})
        return row
    reasons = [a["reason"] for a in attempts]
    hard_codes = (SCENE_ACTION_TOO_LONG, SCENE_ACTION_INCOMPLETE, SCENE_NO_FOOTAGE)
    code = next((r for r in reasons if r in hard_codes),
                reasons[0] if reasons else SCENE_NO_FOOTAGE)
    forced = bool(scene.get("text_forces_visual"))
    if forced and all(r in (SCENE_VISUAL_MISSING, SCENE_SOURCE_DUPLICATE, SCENE_SOURCE_BUDGET)
                      for r in reasons):
        code = SCENE_NO_FOOTAGE
    # 拆包文案强制画面：没拆包素材时保留原镜头记 unmet，不留黑（与单句路径一致）。
    if preview and code in hard_codes and not forced:
        message = (f"{scene.get('label')}：素材池里没有{scene.get('label')}画面"
                   if code == SCENE_NO_FOOTAGE else
                   f"{scene.get('label')}：{scene.get('requirement')}，候选素材都演不完整")
        for position in positions:
            segments[position] = _visual_missing(segments[position], message, code)
        row.update({"status": "visual_missing", "reason": code})
    else:
        note = ""
        if forced and code == SCENE_NO_FOOTAGE:
            note = (f"{scene.get('label')}：这句要看到撕开/取出动作，候选和素材池里没有能放进这组的"
                    "拆包画面，暂时保留原镜头，请补拍拆包镜头后重跑该句")
        for member in members:
            member["scene_rule"] = {"id": scene.get("id"), "label": scene.get("label"),
                                    "status": "unmet", "reason": code, "group": indices,
                                    **({"operator_note": note} if note else {})}
        row.update({"status": "unmet", "reason": code,
                    **({"operator_note": note} if note else {})})
    return row


def _visual_missing(seg: dict[str, Any], reason: str, code: str) -> dict[str, Any]:
    keep = {k: v for k, v in seg.items()
            if k not in {"video", "source_start", "source_end", "duration", "span_parts",
                         "span_solution", "window_solution", "evidence_intervals",
                         "temporary_shot_id", "speed_range_override", "guard_override"}}
    audio_s = _number(seg.get("audio_duration_us")) / US
    return {**keep, "video": None, "source_start": 0.0, "source_end": 0.0,
            "duration": round(max(1.0, audio_s + 0.12), 6), "evidence_intervals": [],
            "temporary_shot_id": None, "visual_missing": True,
            "visual_missing_reason": reason, "visual_missing_reason_code": code,
            "operator_note": f"{reason}；已留黑画面，保留旁白和字幕。补拍或换素材后重跑该句。",
            "selection_mode": "visual_missing", "action_complete": False,
            "video_speed": 1.0, "window_solution": None, "window_reason": code,
            "multi_shot": False, "span_parts": None, "span_solution": None,
            "preview_unresolved": True, "preview_reason": reason}


def _sync_visual_missing(matched: dict[str, Any], out_manifest: dict[str, Any],
                         segments: list) -> None:
    """场景规则留黑的句子也记进选镜报告的留空清单，回执里的留空数才对得上。"""
    items = list(matched.get("visual_missing_matches") or [])
    known = {int(item.get("index") or 0) for item in items if isinstance(item, dict)}
    for position, seg in enumerate(segments):
        if (not isinstance(seg, dict) or not seg.get("visual_missing")
                or not str(seg.get("visual_missing_reason_code") or "").startswith("SCENE_")
                or position + 1 in known):
            continue
        items.append({"index": position + 1, "text": seg.get("text"),
                      "claim_text": seg.get("claim_text") or seg.get("text"),
                      "reason": seg.get("visual_missing_reason"),
                      "reason_code": seg.get("visual_missing_reason_code"),
                      "operator_note": seg.get("operator_note"),
                      "audio_duration_us": int(seg.get("audio_duration_us") or 0),
                      "source": "scene_rules"})
    items.sort(key=lambda item: int(item.get("index") or 0))
    matched["visual_missing_matches"] = items
    matched["visual_missing_count"] = len(items)
    out_manifest["visual_missing_matches"] = items


def _prefetch_phases(segments: list, rules: dict[str, Any], shots_by_id: dict[str, dict],
                     report_dir: Path, locate: Callable[..., dict]) -> None:
    """选中镜头里需要相位打点的先并发打一遍，结果进缓存；后面逐句处理时直接命中。"""
    from concurrent.futures import ThreadPoolExecutor
    jobs: dict[tuple[str, str], tuple[dict, dict]] = {}
    for seg in segments:
        if not isinstance(seg, dict) or seg.get("visual_missing") or not seg.get("video"):
            continue
        shot = shots_by_id.get(str(seg.get("temporary_shot_id") or ""))
        scene = rulepack.scene_for(rules, str(seg.get("text") or ""), shot) if shot else None
        if scene and scene.get("text_forces_visual") and not rulepack.shot_has_visual(scene, shot):
            continue
        if scene and scene.get("phases") and scene.get("kind") in ("full_window", "split_speed"):
            jobs.setdefault((str(shot.get("shot_id")), str(scene.get("id"))), (shot, scene))
    if len(jobs) < 2:
        return
    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
        for future in [pool.submit(locate, shot, scene, report_dir) for shot, scene in jobs.values()]:
            try:
                future.result()
            except Exception:  # noqa: BLE001 —— 预热失败不影响逐句处理
                pass


def apply(manifest: dict[str, Any], matched: dict[str, Any], analysis: dict[str, Any],
          report_dir: Path, *, locate: Optional[Callable[..., dict]] = None) -> dict[str, Any]:
    rules = rulepack.for_manifest(manifest)
    report: dict[str, Any] = {"category": rules.get("category"), "rulepacks": rules.get("rulepacks"),
                              "versions": rules.get("versions"), "rows": []}
    out_manifest = matched.get("manifest") if isinstance(matched.get("manifest"), dict) else None
    segments = (out_manifest or {}).get("segments")
    if not rules.get("scenes") or not isinstance(segments, list):
        return report
    if locate is None:
        locate = lambda shot, scene, rdir: phase_timing.locate(shot, scene, rdir)  # noqa: E731
        prefetch = True
    else:
        prefetch = False
    preview = str(manifest.get("delivery_mode") or "").lower() in ("preview", "preview_only", "预览")
    shots_by_id = {str(s.get("shot_id")): s for s in analysis.get("shots") or []
                   if isinstance(s, dict) and s.get("shot_id")}
    recs = {int(r.get("index")): r for r in matched.get("recommendations") or []
            if isinstance(r, dict) and str(r.get("index", "")).isdigit()}
    used: set[str] = set()
    for seg in segments:
        if isinstance(seg, dict) and seg.get("temporary_shot_id"):
            used.add(str(seg["temporary_shot_id"]))
    from . import usage_ledger
    others = usage_ledger.other_task_counts(manifest)
    if prefetch:
        _prefetch_phases(segments, rules, shots_by_id, report_dir, locate)
    handled: set[int] = set()
    budget = _source_budget(manifest)
    for group in _split_groups(segments, rules, shots_by_id,
                               category=str(manifest.get("category") or "")):
        report["rows"].append(_apply_group(
            group, segments, recs=recs, shots_by_id=shots_by_id, used=used, manifest=manifest,
            report_dir=report_dir, locate=locate, preview=preview))
        handled.update(group["positions"])
    for position, seg in enumerate(segments):
        if (position in handled or not isinstance(seg, dict) or seg.get("visual_missing")
                or not seg.get("video")):
            continue
        index = position + 1
        shot = shots_by_id.get(str(seg.get("temporary_shot_id") or "")) or {}
        contract = _contract(seg, manifest)
        text = str(seg.get("text") or "")
        scene = rulepack.scene_for(rules, text, shot)
        # 功能句（芯体、透气、不磨腿……）不能被「镜头里刚好有人指包装」拐去正面指产品。
        if scene and scene.get("id") == "point_front" and contract.get("kind") == "functional":
            scene = None
        trigger = "text" if scene and scene.get("text_keywords") else "shot"
        # 品牌、促销、报数量的句子本来就该给外包装（运营反馈「结尾改为产品外包装」），不跟着强制拆包。
        # 文案已命中荧光/透光等强制场景时也不要被「连续袋面→拆包」抢走
        # （「不含荧光剂」选中袋面镜头时 fluorescence 以前要对上画面才命中，会被 prior_static_pack 拐走）。
        if ((not scene or not scene.get("text_keywords")) and contract.get("kind") != "functional"
                and not {"cta", "quantity", "brand"} & set(contract.get("intents") or [])
                and not any(sc.get("text_forces_visual") and rulepack.text_hits_scene(sc, text)
                            for sc in (rules.get("scenes") or []))):
            previous = segments[position - 1] if position > 0 else None
            prev_shot = (shots_by_id.get(str(previous.get("temporary_shot_id") or ""))
                         if isinstance(previous, dict) and not previous.get("visual_missing") else None)
            follow = rulepack.followup_scene(rules, text, prev_shot, shot)
            if follow is not None:
                scene, trigger = follow, "prior_static_pack"
        if not scene:
            continue
        attempts = []
        applied = None
        # 文案强制要动作画面、但选中的只是袋面静态：换不到时保留原镜头记 unmet，不留黑。
        forced = bool(scene.get("text_forces_visual")) and not rulepack.shot_has_visual(scene, shot)
        candidates = _candidates(seg, recs.get(index, {}), shots_by_id, used, contract, others)
        if scene.get("text_forces_visual"):
            candidates = _visual_candidates(scene, candidates, shots_by_id, used, segments,
                                            position, contract)
        # 先在所有候选里找能演完整动作的；都不行再退到「动作做完后定住的画面」；
        # 最后换素材池里没用过的静态镜头（只由画面触发的规则才换，文案要求的场景不能换走）。
        # 开了相似画面去重时，和成片里已有画面同机位同动作的换镜候选挪到素材池之后再试。
        repeats: list[tuple[dict[str, Any], bool]] = []
        if rules.get("avoid_similar_shots"):
            from .shot_analyzer import _look_key
            looks = {_look_key(shots_by_id.get(str(s.get("temporary_shot_id") or "")) or {})
                     for i, s in enumerate(segments) if i != position and isinstance(s, dict)} - {""}
            repeats = [c for c in candidates if c[1] and _look_key(c[0]) in looks]
            candidates = [c for c in candidates if c not in repeats]
        stages = [(False, candidates, "")]
        # settled / lead_in / pool_fill 只允许预览返工；formal 必须完整动作或 unmet。
        if preview and scene.get("settled_fallback"):
            stages.append((True, candidates, ""))
        if preview and not scene.get("text_keywords") and scene.get("settled_fallback"):
            stages.append((False, _pool_fill(seg, rules, shots_by_id, used, segments,
                                             contract=contract, others=others), "pool_fill"))
        if repeats:
            stages.append((False, repeats, ""))
            if preview and scene.get("settled_fallback"):
                stages.append((True, repeats, ""))
        from .shot_analyzer import focus_on_topic
        for settled_ok, stage, origin in stages:
            for candidate, swapped in stage:
                if swapped:
                    candidate = focus_on_topic(candidate, contract)
                video = str(candidate.get("video") or "")
                clash = ((swapped or settled_ok or origin == "pool_fill") and video
                         and _source_clash(segments, position, video, budget))
                if clash and (clash == SCENE_SOURCE_DUPLICATE or video != str(seg.get("video") or "")):
                    attempts.append({"shot_id": candidate.get("shot_id"), "swapped": swapped,
                                     "ok": False, "reason": clash,
                                     **({"pass": "settled"} if settled_ok else {}),
                                     **({"source": origin} if origin else {})})
                    continue
                try:
                    if origin == "pool_fill":
                        result = _plain_swap(seg, candidate, manifest=manifest, fill=True,
                                             audio_us=int(seg.get("audio_duration_us") or 0))
                    else:
                        result = _plan_for(scene, seg, candidate, swapped=swapped, manifest=manifest,
                                           report_dir=report_dir, locate=locate, settled_ok=settled_ok,
                                           contract=contract)
                except Exception as exc:  # noqa: BLE001 —— 单句规则失败不拦整稿，如实记录
                    result = {"ok": False, "reason": f"SCENE_RULE_ERROR:{type(exc).__name__}",
                              "detail": {"error": str(exc)[:300]}}
                attempts.append({"shot_id": candidate.get("shot_id"), "swapped": swapped,
                                 "ok": bool(result.get("ok")), "reason": result.get("reason", ""),
                                 "detail": result.get("detail"), "phases": result.get("phases"),
                                 **({"pass": "settled"} if settled_ok else {}),
                                 **({"source": origin} if origin else {})})
                if result.get("ok"):
                    applied = (candidate, swapped, result)
                    break
            if applied:
                break
        row = {"index": index, "text": seg.get("text"), "scene": scene.get("id"),
               "scene_label": scene.get("label"), "requirement": scene.get("requirement"),
               "trigger": trigger, "attempts": attempts}
        if applied:
            from .shot_analyzer import annotate_sentence_fit
            candidate, swapped, result = applied
            seg.update(result.get("update") or {})
            # applied 只说明动作放完整了；换镜后这句口播还对不对得上另记 outcome：
            # original 原镜头放完整，related_swap 换成同主题画面，display_swap 展示句换展示画面。
            if not swapped:
                outcome = "original"
            elif contract.get("kind") == "functional":
                outcome = "related_swap"
            else:
                outcome = "display_swap"
            seg["scene_rule"] = {"id": scene.get("id"), "label": scene.get("label"),
                                 "status": "applied", "swapped": swapped,
                                 "shot_id": candidate.get("shot_id"),
                                 "phases": result.get("phases"),
                                 "mode": result.get("mode", "full"), "outcome": outcome}
            if swapped:
                used.add(str(candidate.get("shot_id")))
                seg["selection_mode"] = f"{seg.get('selection_mode') or 'auto'}+scene_swap"
                annotate_sentence_fit(seg, contract, candidate)
            row.update({"status": "applied", "shot_id": candidate.get("shot_id"),
                        "swapped": swapped, "mode": result.get("mode", "full"), "outcome": outcome,
                        "sentence_fit": (seg.get("sentence_fit") or {}).get("fit")})
        else:
            reasons = [a["reason"] for a in attempts]
            hard = any(r in (SCENE_ACTION_TOO_LONG, SCENE_ACTION_INCOMPLETE,
                             SCENE_VISUAL_MISSING, SCENE_SINGLE_SHOT_INFEASIBLE) for r in reasons)
            code = next((r for r in reasons if r in (SCENE_ACTION_TOO_LONG, SCENE_ACTION_INCOMPLETE,
                                                     SCENE_VISUAL_MISSING,
                                                     SCENE_SINGLE_SHOT_INFEASIBLE)),
                        reasons[0] if reasons else SCENE_PHASE_UNVERIFIED)
            if forced and all(r in (SCENE_VISUAL_MISSING, SCENE_SOURCE_DUPLICATE, SCENE_SOURCE_BUDGET)
                              for r in reasons):
                code = SCENE_NO_FOOTAGE
            if hard and preview and scene.get("kind") in ("split_speed", "full_window") and not forced:
                message = f"{scene.get('label')}：{scene.get('requirement')}，候选素材都放不进这句话"
                segments[position] = _visual_missing(seg, message, code)
                segments[position]["sentence_fit"] = {**(seg.get("sentence_fit") or {}), "fit": "missing"}
                row.update({"status": "visual_missing", "reason": code})
            else:
                seg["scene_rule"] = {"id": scene.get("id"), "label": scene.get("label"),
                                     "status": "unmet", "reason": code}
                if forced:
                    note = (f"{scene.get('label')}：这句要看到撕开/取出动作，候选和素材池里没有能放进这句的"
                            "拆包画面，暂时保留原镜头，请补拍拆包镜头后重跑该句")
                    seg["scene_rule"]["operator_note"] = note
                    row["operator_note"] = note
                row.update({"status": "unmet", "reason": code})
        report["rows"].append(row)
    _sync_visual_missing(matched, out_manifest, segments)
    report["applied"] = sum(1 for r in report["rows"] if r.get("status") == "applied")
    report["partial"] = sum(1 for r in report["rows"] if r.get("status") == "partial")
    report["unmet"] = sum(1 for r in report["rows"] if r.get("status") not in ("applied", "partial"))
    Path(report_dir).mkdir(parents=True, exist_ok=True)
    (Path(report_dir) / "scene_rules_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
