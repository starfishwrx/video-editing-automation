"""场景规则后处理（v1.5.2，卫品指令）。

选镜（``match_manifest``）完成后，对命中场景规则的句子重新定窗：

* ``split_speed``（吸水实验）：同一条素材拆成「倒水 / 吸收 / 擦拭」三段，各段不同倍速，
  吸收段快进到 3 秒内，倒水与擦拭最高 1.6 倍，三段加起来正好等于这句配音；连续几句都在讲
  吸水时，这几句合起来演一遍实验，按句子边界切给各句；
* ``full_window``（撕包装、透灯光、荧光测试、正面指产品）：窗口必须盖住完整动作，
  允许在规则给出的范围内倍速；
* ``single_shot``（背标）：一个镜头盖住整句，不拼接，最慢 0.85 倍；
* ``require_visual``（面层）：口播提到面层时，镜头必须看得到面层。

选中的镜头满足不了，就按推荐顺序换候选；都不行时，动作确实放不进这句话的在预览稿留黑屏
（SCENE_ACTION_TOO_LONG），正式稿保留原镜头并记录；相位标不出来的保留原镜头并记录。
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
    ``absorb_max_output_s``；总长超了先提吸收段倍速，再把倒水、擦拭一起提速（≤1.6）；
    总长不够先放慢吸收段（不低于 1.0 倍、输出不超过上限），再向擦拭之后、倒水之前延伸素材。
    """
    speed = scene.get("speed") or {}
    pour_rng = _range(speed.get("pour"), (1.0, 1.6))
    wipe_rng = _range(speed.get("wipe"), (1.0, 1.6))
    absorb_cap_us = _us(speed.get("absorb_max_output_s", 3.0))
    absorb_hi = max(1.0, _number(speed.get("absorb_max_speed"), 6.0))
    fps = _fps(manifest)
    frame = timing_contract.frame_us(fps)
    names = [p["name"] for p in scene.get("phases") or []]
    if any(n not in phases for n in names) or len(names) != 3:
        return {"ok": False, "reason": SCENE_ACTION_INCOMPLETE}
    scene_in, scene_out = _us(shot.get("source_start")), _us(shot.get("source_end"))
    scene_out = min(scene_out, _material_us(shot))
    b0 = max(scene_in, _us(phases[names[0]][0]))
    b1 = max(b0, _us(phases[names[0]][1]))
    b2 = max(b1, _us(phases[names[2]][0]))
    b3 = min(scene_out, max(b2, _us(phases[names[2]][1])))
    pour_m, absorb_m, wipe_m = b1 - b0, b2 - b1, b3 - b2
    if min(pour_m, wipe_m) < MIN_PART_US:
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
            if common > min(pour_rng[1], wipe_rng[1]) + 1e-9:
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
    parts_spec.append((names[2], wipe_m, s_wipe, wipe_rng))
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
    if cursor > scene_out + frame or cursor > _material_us(shot):
        return {"ok": False, "reason": SCENE_ACTION_TOO_LONG,
                "detail": {"end_us": cursor, "scene_out_us": scene_out}}
    return {"ok": True, "parts": parts, "timeline_us": timeline,
            "action_us": b3 - b0}


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
def _candidates(seg: dict[str, Any], rec: dict[str, Any], shots_by_id: dict[str, dict],
                used: set[str]) -> list[tuple[dict[str, Any], bool]]:
    out: list[tuple[dict[str, Any], bool]] = []
    current = shots_by_id.get(str(seg.get("temporary_shot_id") or ""))
    if current:
        out.append((current, False))
    for row in (rec or {}).get("top3") or []:
        shot_id = str(row.get("shot_id") or "")
        shot = shots_by_id.get(shot_id)
        if (not shot or shot_id in used or (current and shot_id == current.get("shot_id"))
                or shot.get("status") != "ready_for_matching" or not row.get("eligible", True)):
            continue
        out.append((shot, True))
    return out


def _plain_swap(seg: dict[str, Any], shot: dict[str, Any], *, manifest: dict[str, Any],
                audio_us: int) -> dict[str, Any]:
    """换成候选镜头，窗口按常规选镜口径求解（不加场景倍速和保护区覆盖）。"""
    from .shot_analyzer import candidate_window
    solution = candidate_window(shot, audio_duration_us=audio_us, manifest=manifest,
                                time_sensitive=bool(seg.get("time_sensitive")))
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
    return {"ok": True, "update": update}


def _plan_for(scene: dict[str, Any], seg: dict[str, Any], shot: dict[str, Any], *,
              swapped: bool, manifest: dict[str, Any], report_dir: Path,
              locate: Callable[..., dict]) -> dict[str, Any]:
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
    located = locate(shot, scene, report_dir)
    if not located.get("ok"):
        return {"ok": False, "reason": SCENE_PHASE_UNVERIFIED, "soft": True,
                "detail": {"phase": located.get("reason")}}
    if not located.get("complete"):
        return {"ok": False, "reason": SCENE_ACTION_INCOMPLETE, "phases": located.get("phases")}
    phases = located.get("phases") or {}
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
        if not solution.ok:
            reason = (SCENE_ACTION_TOO_LONG if solution.reason in _TOO_LONG
                      else solution.reason)
            return {"ok": False, "reason": reason, "detail": solution.detail, "phases": phases}
        return {"ok": True, "phases": phases, "update": _single_update(
            seg, shot, solution, guards, speed_range=rng, evidence=evidence, swapped=swapped)}
    return {"ok": False, "reason": "SCENE_KIND_UNKNOWN"}


# ── 吸水实验按句组处理 ────────────────────────────────────────────────
def _split_groups(segments: list, rules: dict[str, Any],
                  shots_by_id: dict[str, dict]) -> list[dict[str, Any]]:
    """连续几句都在讲吸水时，它们合起来演一遍完整实验，而不是每句各演一遍。

    组内至少一句的选中镜头本身就是实验画面才处理；有画面缺失的句子把组切断。
    """
    groups: list[dict[str, Any]] = []
    for scene in rules.get("scenes") or []:
        if scene.get("kind") != "split_speed":
            continue
        run: list[tuple[int, bool]] = []
        for position, seg in enumerate([*segments, None]):
            usable = isinstance(seg, dict) and not seg.get("visual_missing") and seg.get("video")
            if usable and rulepack.text_hits_scene(scene, seg.get("text")):
                shot = shots_by_id.get(str(seg.get("temporary_shot_id") or "")) or {}
                run.append((position, rulepack.scene_for(rules, str(seg.get("text") or ""), shot) is scene))
                continue
            if any(hit for _p, hit in run):
                groups.append({"scene": scene, "positions": [p for p, _hit in run]})
            run = []
    return groups


def _group_candidates(members: list[dict[str, Any]], positions: list[int], recs: dict[int, dict],
                      shots_by_id: dict[str, dict], used: set[str]) -> list[dict[str, Any]]:
    own = {str(m.get("temporary_shot_id") or "") for m in members}
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for member in members:
        shot = shots_by_id.get(str(member.get("temporary_shot_id") or ""))
        if shot and shot["shot_id"] not in seen:
            seen.add(shot["shot_id"])
            out.append(shot)
    for position in positions:
        for row in (recs.get(position + 1) or {}).get("top3") or []:
            shot_id = str(row.get("shot_id") or "")
            shot = shots_by_id.get(shot_id)
            if (not shot or shot_id in seen or (shot_id in used and shot_id not in own)
                    or shot.get("status") != "ready_for_matching" or not row.get("eligible", True)):
                continue
            seen.add(shot_id)
            out.append(shot)
    return out


def _plan_group(scene: dict[str, Any], members: list[dict[str, Any]], shot: dict[str, Any], *,
                manifest: dict[str, Any], report_dir: Path,
                locate: Callable[..., dict]) -> dict[str, Any]:
    audio = [int(m.get("audio_duration_us") or 0) for m in members]
    if min(audio) <= 0:
        return {"ok": False, "reason": timing_contract.REASON_NO_AUDIO}
    located = locate(shot, scene, report_dir)
    if not located.get("ok"):
        return {"ok": False, "reason": SCENE_PHASE_UNVERIFIED, "soft": True,
                "detail": {"phase": located.get("reason")}}
    if not located.get("complete"):
        return {"ok": False, "reason": SCENE_ACTION_INCOMPLETE, "phases": located.get("phases")}
    phases = located.get("phases") or {}
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
            "plan": [{"phase": p["phase"], "speed": p["speed"],
                      "segment_s": round(p["segment_us"] / US, 3)} for p in plan["parts"]]}


def _apply_group(group: dict[str, Any], segments: list, *, recs: dict[int, dict],
                 shots_by_id: dict[str, dict], used: set[str], manifest: dict[str, Any],
                 report_dir: Path, locate: Callable[..., dict], preview: bool) -> dict[str, Any]:
    scene, positions = group["scene"], group["positions"]
    members = [segments[p] for p in positions]
    indices = [p + 1 for p in positions]
    attempts, applied = [], None
    for shot in _group_candidates(members, positions, recs, shots_by_id, used):
        try:
            result = _plan_group(scene, members, shot, manifest=manifest, report_dir=report_dir,
                                 locate=locate)
        except Exception as exc:  # noqa: BLE001 —— 规则失败不拦整稿，如实记录
            result = {"ok": False, "reason": f"SCENE_RULE_ERROR:{type(exc).__name__}",
                      "detail": {"error": str(exc)[:300]}}
        attempts.append({"shot_id": shot.get("shot_id"),
                         "swapped": shot.get("shot_id") != members[0].get("temporary_shot_id"),
                         "ok": bool(result.get("ok")), "reason": result.get("reason", ""),
                         "detail": result.get("detail"), "phases": result.get("phases")})
        if result.get("ok"):
            applied = (shot, result)
            break
    row = {"index": indices[0], "indices": indices,
           "text": " / ".join(str(m.get("text") or "") for m in members),
           "scene": scene.get("id"), "scene_label": scene.get("label"),
           "requirement": scene.get("requirement"), "attempts": attempts}
    if applied:
        shot, result = applied
        for order, (position, member, update) in enumerate(zip(positions, members, result["updates"])):
            swapped = str(member.get("temporary_shot_id") or "") != str(shot.get("shot_id"))
            member.update(update)
            member["scene_rule"] = {"id": scene.get("id"), "label": scene.get("label"),
                                    "status": "applied", "swapped": swapped,
                                    "shot_id": shot.get("shot_id"), "phases": result.get("phases"),
                                    "group": indices, "group_order": order}
            if swapped:
                member["selection_mode"] = f"{member.get('selection_mode') or 'auto'}+scene_swap"
        used.add(str(shot.get("shot_id")))
        row.update({"status": "applied", "shot_id": shot.get("shot_id"), "plan": result.get("plan")})
        return row
    reasons = [a["reason"] for a in attempts]
    hard_codes = (SCENE_ACTION_TOO_LONG, SCENE_ACTION_INCOMPLETE)
    code = next((r for r in reasons if r in hard_codes),
                reasons[0] if reasons else SCENE_PHASE_UNVERIFIED)
    if preview and code in hard_codes:
        message = f"{scene.get('label')}：{scene.get('requirement')}，候选素材都演不完整"
        for position in positions:
            segments[position] = _visual_missing(segments[position], message, code)
        row.update({"status": "visual_missing", "reason": code})
    else:
        for member in members:
            member["scene_rule"] = {"id": scene.get("id"), "label": scene.get("label"),
                                    "status": "unmet", "reason": code, "group": indices}
        row.update({"status": "unmet", "reason": code})
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


def apply(manifest: dict[str, Any], matched: dict[str, Any], analysis: dict[str, Any],
          report_dir: Path, *, locate: Optional[Callable[..., dict]] = None) -> dict[str, Any]:
    rules = rulepack.for_manifest(manifest)
    report: dict[str, Any] = {"category": rules.get("category"), "rulepacks": rules.get("rulepacks"),
                              "versions": rules.get("versions"), "rows": []}
    out_manifest = matched.get("manifest") if isinstance(matched.get("manifest"), dict) else None
    segments = (out_manifest or {}).get("segments")
    if not rules.get("scenes") or not isinstance(segments, list):
        return report
    locate = locate or (lambda shot, scene, rdir: phase_timing.locate(shot, scene, rdir))
    preview = str(manifest.get("delivery_mode") or "").lower() in ("preview", "preview_only", "预览")
    shots_by_id = {str(s.get("shot_id")): s for s in analysis.get("shots") or []
                   if isinstance(s, dict) and s.get("shot_id")}
    recs = {int(r.get("index")): r for r in matched.get("recommendations") or []
            if isinstance(r, dict) and str(r.get("index", "")).isdigit()}
    used: set[str] = set()
    for seg in segments:
        if isinstance(seg, dict) and seg.get("temporary_shot_id"):
            used.add(str(seg["temporary_shot_id"]))
    handled: set[int] = set()
    for group in _split_groups(segments, rules, shots_by_id):
        handled.update(group["positions"])
        report["rows"].append(_apply_group(
            group, segments, recs=recs, shots_by_id=shots_by_id, used=used, manifest=manifest,
            report_dir=report_dir, locate=locate, preview=preview))
    for position, seg in enumerate(segments):
        if (position in handled or not isinstance(seg, dict) or seg.get("visual_missing")
                or not seg.get("video")):
            continue
        index = position + 1
        shot = shots_by_id.get(str(seg.get("temporary_shot_id") or "")) or {}
        scene = rulepack.scene_for(rules, str(seg.get("text") or ""), shot)
        if not scene:
            continue
        attempts = []
        applied = None
        for candidate, swapped in _candidates(seg, recs.get(index, {}), shots_by_id, used):
            try:
                result = _plan_for(scene, seg, candidate, swapped=swapped, manifest=manifest,
                                   report_dir=report_dir, locate=locate)
            except Exception as exc:  # noqa: BLE001 —— 单句规则失败不拦整稿，如实记录
                result = {"ok": False, "reason": f"SCENE_RULE_ERROR:{type(exc).__name__}",
                          "detail": {"error": str(exc)[:300]}}
            attempts.append({"shot_id": candidate.get("shot_id"), "swapped": swapped,
                             "ok": bool(result.get("ok")), "reason": result.get("reason", ""),
                             "detail": result.get("detail"), "phases": result.get("phases")})
            if result.get("ok"):
                applied = (candidate, swapped, result)
                break
        row = {"index": index, "text": seg.get("text"), "scene": scene.get("id"),
               "scene_label": scene.get("label"), "requirement": scene.get("requirement"),
               "attempts": attempts}
        if applied:
            candidate, swapped, result = applied
            seg.update(result.get("update") or {})
            seg["scene_rule"] = {"id": scene.get("id"), "label": scene.get("label"),
                                 "status": "applied", "swapped": swapped,
                                 "shot_id": candidate.get("shot_id"),
                                 "phases": result.get("phases")}
            if swapped:
                used.add(str(candidate.get("shot_id")))
                seg["selection_mode"] = f"{seg.get('selection_mode') or 'auto'}+scene_swap"
            row.update({"status": "applied", "shot_id": candidate.get("shot_id"),
                        "swapped": swapped})
        else:
            reasons = [a["reason"] for a in attempts]
            hard = any(r in (SCENE_ACTION_TOO_LONG, SCENE_ACTION_INCOMPLETE,
                             SCENE_VISUAL_MISSING, SCENE_SINGLE_SHOT_INFEASIBLE) for r in reasons)
            code = next((r for r in reasons if r in (SCENE_ACTION_TOO_LONG, SCENE_ACTION_INCOMPLETE,
                                                     SCENE_VISUAL_MISSING,
                                                     SCENE_SINGLE_SHOT_INFEASIBLE)),
                        reasons[0] if reasons else SCENE_PHASE_UNVERIFIED)
            if hard and preview and scene.get("kind") in ("split_speed", "full_window"):
                message = f"{scene.get('label')}：{scene.get('requirement')}，候选素材都放不进这句话"
                segments[position] = _visual_missing(seg, message, code)
                row.update({"status": "visual_missing", "reason": code})
            else:
                seg["scene_rule"] = {"id": scene.get("id"), "label": scene.get("label"),
                                     "status": "unmet", "reason": code}
                row.update({"status": "unmet", "reason": code})
        report["rows"].append(row)
    _sync_visual_missing(matched, out_manifest, segments)
    report["applied"] = sum(1 for r in report["rows"] if r.get("status") == "applied")
    report["unmet"] = sum(1 for r in report["rows"] if r.get("status") != "applied")
    Path(report_dir).mkdir(parents=True, exist_ok=True)
    (Path(report_dir) / "scene_rules_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
