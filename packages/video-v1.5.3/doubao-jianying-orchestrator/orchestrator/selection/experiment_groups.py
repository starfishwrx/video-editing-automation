"""Default same-experiment pairing and phase fits, using existing pixel observers.

The result is the ordinary per-sentence/multi-shot manifest. Narration, captions,
music and unrelated rows stay in the existing production and recovery chain.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace

from .. import ai_editor_contract as identity
from ..policy import continuity
from ..rulepack import absorption_experiment_role as inferred_role
from . import experiment_contract, experiment_events, timing_contract as timing

ROLES = {"absorption", "rewet", "followup", "bridge", "breathability", "fluorescence",
         "sku_change", "promotion", "other", "uncertain"}
PAIR_LIMIT = 6
ADJACENT_EXTRA = 4


def groups(sentences, *, require_rewet=False):
    """Find adjacent experiment clauses, including one clause carrying the whole demonstration."""
    current = []
    for row in [*sentences, {}]:
        role = inferred_role(row)
        row = {**row, "experiment_role": role}
        roles = [s["experiment_role"] for s in current]
        allowed = role in {"absorption", "rewet", "followup", "bridge"}
        if not allowed or (role == "absorption" and "rewet" in roles):
            if ({"absorption", "rewet"}.intersection(roles)
                    and (not require_rewet or {"absorption", "rewet"} <= set(roles))
                    and continuity.adjacent([s["index"] for s in current])):
                yield current
            current = []
        if allowed:
            current.append(row)


def require_complete(sentences, segments):
    for members in groups(sentences):
        indices = [m["index"] for m in members]
        selected = [segments[i - 1].get("continuity_group") for i in indices]
        group = selected[0]
        if (not continuity.is_experiment(group) or not group["input"].get("allow_experiment_phase_speedup")
                or [m["index"] for m in group["input"]["members"]] != indices
                or any(other != group for other in selected[1:])):
            raise ValueError(f"AI_EDITOR_EXPERIMENT_GROUP_PENDING:{indices[0]}:three_visible_stages_required")


def _scope(manifest, analysis):
    allowed = manifest.get("material_sources")
    if allowed is None:
        allowed = list(dict.fromkeys(s["video"] for s in analysis["shots"]))
    return {**manifest, "task_id": manifest.get("task_id") or "experiment-" + identity.digest({
        "product": manifest.get("product"), "segments": timing.split_sentences(manifest)})[:20],
        "product": manifest.get("product", ""), "category": "weipin", "material_sources": allowed}


def _samples(shot, directory, *, ffmpeg, ffprobe, font_path):
    from ..media import temporal_samples
    key = identity.digest(identity.source_identity(shot["video"]))[:24]
    root = directory / "media" / key
    saved = root / "samples.json"
    if saved.is_file():
        return temporal_samples.validate(json.loads(saved.read_text("utf-8")))
    # Preserve an interrupted normalization; never overwrite source or evidence.
    if root.exists():
        root = directory / "media" / (key + "-" + str(len(list((directory / "media").iterdir()))))
    return temporal_samples.prepare(shot["video"], root, ffmpeg=ffmpeg, ffprobe=ffprobe, font_path=font_path)


def _window(shot, samples):
    lo, hi = round(shot["source_start"] * 1e6), round(shot["source_end"] * 1e6)
    ids = [i for i, f in enumerate(samples["frames"])
           if lo <= f["pts_us"] and min(samples["source_end_us"], f["pts_us"] + 16_667) <= hi]
    if len(ids) < 2:
        raise ValueError("EXPERIMENT_PAIR_WINDOW_INVALID")
    return ids[0], ids[-1]


def _pour_focus_ranges(manifest, first, directory):
    """Located pour range of the A source, used only to choose which frames to show.

    The phase locator is a retrieval hint; the pair observer still has to see
    and report the pour itself. Any locator failure falls back to even sampling.
    """
    try:
        from .. import rulepack
        from ..vision import phase_timing
        scene = next((s for s in rulepack.for_manifest(manifest)["scenes"] if s.get("id") == "absorb_test"), None)
        if scene is None:
            return {}
        report = Path(directory)
        report = report.parents[2] if len(report.parents) > 2 else report
        located = phase_timing.locate(first, scene, report)
        pour = (located.get("phases") or {}).get("pour") if isinstance(located, dict) else None
        if not isinstance(pour, (list, tuple)) or len(pour) != 2:
            return {}
        lo, hi = float(pour[0]), float(pour[1])
        if not lo <= hi:
            return {}
        return {"A": [(round((lo - 0.5) * 1e6), round((hi + 0.5) * 1e6))]}
    except (OSError, ValueError, TypeError, KeyError, StopIteration, IndexError):
        return {}


def observe_pair(manifest, analysis, first, second, directory, *, focus="complete_pour"):
    """Pair actual source frames, then locate/refine events on the configured route."""
    from ..runtime.platform_env import find_ffmpeg
    from ..vision import experiment_pair_review, experiment_event_review, experiment_evidence
    ffmpeg, ffprobe = find_ffmpeg()
    if not ffmpeg or not ffprobe:
        raise ValueError("AI_EDITOR_FFMPEG_MISSING")
    candidates = [manifest.get("subtitle_font_path"), os.environ.get("JY_HEADLESS_FONT"),
                  "C:/Windows/Fonts/arial.ttf"]
    font = next((Path(p) for p in candidates if p and Path(p).is_file()), None)
    if font is None:
        raise ValueError("EXPERIMENT_FONT_MISSING")
    scoped = _scope(manifest, analysis)
    if first["shot_id"] == second["shot_id"]:
        # One complete source can also need local phase compression. Reuse the
        # same phase locator as the single-source strategy to retrieve disjoint
        # A/B windows; the pair and event observers must still confirm them.
        from .. import rulepack
        from ..vision import phase_timing
        scene = next(s for s in rulepack.for_manifest(manifest)["scenes"] if s.get("id") == "absorb_test")
        located = phase_timing.locate(first, scene, directory)
        phases = located.get("phases") or {}
        if not located.get("ok") or any(name not in phases for name in ("pour", "absorb", "wipe")):
            raise ValueError("EXPERIMENT_SINGLE_SOURCE_PHASE_UNLOCATED")
        first = {**first, "source_start": max(first["source_start"], min(phases["pour"][0], phases["absorb"][0])),
                 "source_end": min(first["source_end"], max(phases["pour"][1], phases["absorb"][1]))}
        second = {**second, "source_start": max(second["source_start"], phases["wipe"][0]),
                  "source_end": min(second["source_end"], phases["wipe"][1])}
    samples = [_samples(s, directory, ffmpeg=ffmpeg, ffprobe=ffprobe, font_path=font)
               for s in (first, second)]
    windows = [_window(first, samples[0]), _window(second, samples[1])]
    cache_key = experiment_evidence.pair_key(scoped, samples, windows)
    with experiment_evidence.pair_lock(cache_key):
        return _observe_bound_pair(scoped, manifest, first, second, samples, windows, directory,
                                   cache_key, focus, ffmpeg, ffprobe, font)


def _observe_bound_pair(scoped, manifest, first, second, samples, windows, directory,
                        cache_key, focus, ffmpeg, ffprobe, font):
    from ..vision import experiment_pair_review, experiment_event_review, experiment_evidence
    saved_pair = directory / "pair.pair.json"
    if saved_pair.is_file():
        pair = json.loads(saved_pair.read_text("utf-8"))
        if [m["window_frame_ids"] for m in pair["members"]] != [list(w) for w in windows]:
            raise ValueError("EXPERIMENT_PAIR_REVIEW_INPUT_CHANGED")
    else:
        pair = experiment_evidence.reuse_pair(scoped, cache_key, samples, directory)
        if pair is None:
            focus_ranges = _pour_focus_ranges(manifest, first, directory) if first["shot_id"] != second["shot_id"] else {}
            pair = experiment_pair_review.review(scoped, *samples, *windows, directory / "pair.json",
                ffmpeg=ffmpeg, ffprobe=ffprobe, font_path=font, focus_ranges_us=focus_ranges)
    if [member.get("samples") for member in pair.get("members", [])] != samples:
        raise ValueError("EXPERIMENT_PAIR_REVIEW_INPUT_CHANGED")
    if not pair.get("reused_from_task"):
        experiment_evidence.remember_pair(scoped, cache_key, pair)
    experiment_contract.validate_pair_evidence(scoped, pair, require_disjoint_windows=False)
    # GLM and Qwen both use the existing image client; no implicit switch to a
    # separate Qwen video service is needed by the default production entry.
    output = directory / ("events-" + focus + ".json")
    saved_motion = output.with_suffix(".motion.json")
    if saved_motion.is_file():
        from ..vision.experiment_motion_review import validate
        motion = json.loads(saved_motion.read_text("utf-8"))
        validate(scoped, pair, motion)
    else:
        motion = experiment_evidence.reuse_motion(scoped, cache_key, focus, pair, directory)
        if motion is None:
            motion = experiment_event_review.review(scoped, pair, output,
                focus=focus, frames_only=True, ffmpeg=ffmpeg, ffprobe=ffprobe, font_path=font)
    if not motion.get("reused_from_task"):
        experiment_evidence.remember_motion(scoped, cache_key, focus, pair, motion, directory)
    return pair, motion


def _align_short_phase_fragments(planned, sentence_targets, fps):
    """Keep tiny phase fragments on one side of a sentence boundary when possible."""
    frame_sliver = round(2_000_000 / fps)
    boundaries, total = [], 0
    for duration in sentence_targets[:-1]:
        total += duration
        boundaries.append(total)
    offset = 0
    for fit in planned:
        parts = fit['retiming']['parts']
        phase_min = {(p['start'], p['end']): p['minimum_us'] for p in fit['phase_intervals']}
        for boundary in boundaries:
            cursor = offset
            for i, part in enumerate(parts):
                end = cursor + part['segment_us']
                fragment = boundary - cursor
                if 0 < fragment <= frame_sliver and boundary < end and i:
                    # Preserve every source interval and phase. Shift output
                    # time from this phase to an earlier phase that can take it
                    # without slowing down or reducing result visibility.
                    available = part['segment_us'] - phase_min[(part['source_start_us'], part['source_end_us'])]
                    donor = next((p for p in reversed(parts[:i])
                                  if p['source_end_us'] - p['source_start_us'] - p['segment_us'] >= fragment), None)
                    if donor is not None and available >= fragment:
                        donor['segment_us'] += fragment
                        part['segment_us'] -= fragment
                    break
                cursor = end
        cursor = 0
        for part in parts:
            part['target_start_us'] = cursor
            part['speed'] = (part['source_end_us'] - part['source_start_us']) / part['segment_us']
            cursor += part['segment_us']
        fit['retiming']['maximum_speed'] = max(p['speed'] for p in parts)
        offset += cursor


def _context(manifest, analysis, members, narration_timing, pair, motion, shot_ids):
    from ..vision.experiment_motion_review import validate as validate_motion
    shots = {s["shot_id"]: s for s in analysis["shots"]}
    scoped = _scope(manifest, analysis)
    experiment_contract.validate_pair_evidence(scoped, pair, require_disjoint_windows=False)
    observations = validate_motion(scoped, pair, motion)
    originals = timing.split_sentences(manifest)
    pad = timing.us_of(manifest.get("tail_pad_s"), 0)
    allocated = []
    for sentence in members:
        duration, certainty = timing.duration_for(narration_timing, sentence["index"],
            segment=originals[sentence["index"] - 1])
        if certainty == "estimated" or duration <= 0:
            raise ValueError("EXPERIMENT_REAL_NARRATION_REQUIRED")
        sentence = {**sentence, "experiment_role": inferred_role(sentence)}
        allocated.append({k: copy.deepcopy(sentence[k]) for k in (
            "index", "text", "kind", "selling_points", "scene_ids", "experiment_role")})
        allocated[-1].update(audio_duration_us=duration, target_us=duration + pad, parts=[])
    roles = [{"sentence_id": str(s["index"]), "role": s["experiment_role"]} for s in allocated]
    selected = [SimpleNamespace(sentence_id=str(s["index"]), text=s["text"]) for s in allocated]
    rules = experiment_contract.contract_for(scoped)
    allow_speedup = rules.get("phase_speed_policy") == "visible_three_stages"
    experiment_events.validate_focus(selected, roles, observations, allow_phase_speedup=allow_speedup, members=pair["members"])
    rewet = next((i for i, s in enumerate(allocated) if s["experiment_role"] == "rewet"), None)
    boundary = sum(s["target_us"] for s in allocated[:rewet]) if rewet else None
    total = sum(s["target_us"] for s in allocated)
    fps = float(manifest.get("fps", 30))
    task_cap = None if allow_speedup else identity.playback.task_absorb_speed_cap(manifest)
    max_speed = None if allow_speedup else min(identity.playback.PROCESS_MAX, task_cap or identity.playback.PROCESS_MAX)
    planned = experiment_events.fit_phase_windows(pair["members"], observations,
        rules["minimum_result_frames"], total, boundary,
        fps=fps, max_speed=max_speed, allow_phase_speedup=allow_speedup)
    if allow_speedup:
        _align_short_phase_fragments(planned, [s['target_us'] for s in allocated], fps)
    sources = {}
    for sid, member in zip(shot_ids, pair["members"]):
        shot = shots[sid]
        source = identity.source_identity(shot["video"])
        if source != member["samples"]["original"]:
            raise ValueError("EXPERIMENT_PAIR_SOURCE_CHANGED")
        sources[sid] = {"identity": source, "start_us": round(shot["source_start"] * 1e6),
                        "end_us": round(shot["source_end"] * 1e6)}
    # Split constant-speed physical parts at the existing sentence boundaries.
    # Both sides use one rounded source cut, so there is neither a gap nor replay.
    sentence_index, used = 0, 0
    for sid, fit in zip(shot_ids, planned):
        for physical in fit["retiming"]["parts"]:
            start, end, duration = physical["source_start_us"], physical["source_end_us"], physical["segment_us"]
            consumed, cursor = 0, start
            while consumed < duration:
                sentence = allocated[sentence_index]
                take = min(duration - consumed, sentence["target_us"] - used)
                right = end if consumed + take == duration else start + round((end - start) * (consumed + take) / duration)
                if take <= 0 or right <= cursor:
                    raise ValueError("EXPERIMENT_PHASE_DURATION_CONFLICT")
                part = {"shot_id": sid, "source_identity": sources[sid]["identity"],
                    "start_us": cursor, "end_us": right, "target_us": take,
                    "process": not physical["locked"] if not allow_speedup else False}
                if allow_speedup:
                    part["experiment_phase"] = physical["phase"]
                sentence["parts"].append(part)
                consumed += take; used += take; cursor = right
                if used == sentence["target_us"]:
                    sentence_index += 1; used = 0
    if sentence_index != len(allocated) or used:
        raise ValueError("EXPERIMENT_PHASE_DURATION_CONFLICT")
    # Coarse observation windows may overlap at a sampled boundary. Only the
    # event-refined, actually selected cuts must be disjoint and ordered.
    paired_paths = tuple(m["samples"]["original"]["path"] for m in pair["members"])
    continuity.source_chain([(p["source_identity"]["path"], p["start_us"], p["end_us"])
                             for m in allocated for p in m["parts"]], (paired_paths,))
    context = {"product": manifest.get("product"), "category": manifest.get("category"),
        "rules": identity.rules_identity(manifest), "pair_shot_ids": list(shot_ids), "sources": sources,
        "members": allocated, "fps": fps, "evidence_digest": identity.digest([pair, motion]),
        "source_selection": [p.get("source_selection") for p in planned]}
    if task_cap is not None:
        context["task_absorb_speed_cap"] = task_cap
    if allow_speedup:
        context["allow_experiment_phase_speedup"] = True
    return context


def review_group(manifest, context, directory):
    """Independent joint placement review of the exact proposed sentence cuts."""
    from PIL import Image, ImageDraw
    from . import ai_editor as editor
    # The allocation and receipt retain the cap. Identical physical cuts,
    # evidence and sentence targets present the same question to the reviewer.
    context = {k: v for k, v in context.items() if k != "task_absorb_speed_cap"}
    rows, tiles = [], []
    for member in context["members"]:
        for i, cut in enumerate(member["parts"]):
            media = editor._review_media({"video": cut["source_identity"]["path"],
                "source_start": cut["start_us"] / 1e6, "source_end": cut["end_us"] / 1e6},
                directory, dense=False)
            if media["before"] != cut["source_identity"]:
                raise ValueError("EXPERIMENT_PAIR_SOURCE_CHANGED")
            tiles.append(media["sheet"])
            rows.append({"sentence_index": member["index"], "part_index": i + 1, "cut": cut})
    images = [Image.open(p).convert("RGB") for p in tiles]
    width = max(im.width for im in images)
    sheet = Image.new("RGB", (width, sum(im.height + 30 for im in images)), "#222222")
    draw = ImageDraw.Draw(sheet); y = 0
    for row, image in zip(rows, images):
        draw.text((8, y + 4), f"sentence {row['sentence_index']} part {row['part_index']}", fill="white")
        sheet.paste(image, (0, y + 30)); y += image.height + 30
    path = directory / ("group-" + identity.digest(context)[:24] + ".jpg")
    path.parent.mkdir(parents=True, exist_ok=True); sheet.save(path, quality=95)
    prompt = identity.policy_prompt(manifest) + "\n联合复核同次实验在固定逐句切片中的实际画面。\n" + json.dumps(
        context, ensure_ascii=False) + """
图片按sentence/part顺序排列，每个part使用该源片段的实际时间戳。倒液、吸收、按压和提纸必须来自同一场景的同次实验，跨文件或换机位仍要核对布景、台面和巾体状态连续。换布景或无法确认同场时不能通过。
逐句检查实际承担的阶段和口播是否相符，不把总长度可行当成逐句对应。
有独立吸收和反渗句时，反渗句起点对上纸巾接触。单句承担整个实验时，依次看清倒液、吸收和纸巾验证。关键动作、提起和结果要完整；
结果可在同主题后续句承接，但不能借透气/促销句，也不能把后句结果说成前句已经出现。
每句至少一个具体卖点成立，其他只备注；同一组允许各part承担动作的不同阶段，无需每片重播完整实验。
只按本次画面判断商品、同次实验、完整性与逐句支持，不接收旧选镜的通过结论。
输出JSON {"verdict":"matched|mismatch|uncertain","same_product":true,"same_demonstration":true,
"same_selling_point":true,"semantic_continuity":true,"complete_action":true,"result_preserved":true,
"shared_point":"共同具体卖点","product_observation":"商品依据","demonstration_observation":"对应依据",
"observation":"整组可见事实","reason":"时序与完整性依据","confidence":0.9,
"members":[{"index":1,"verdict":"matched","same_product":true,"basis":"editorial_fit",
"phase":"本句阶段","observation":"本句事实","reason":"支持理由","supported_point_ids":["p1"],
"time_sensitive":false,"scene_ids":[],"parts":[{"observation":"本part实际事实",
"evidence_intervals_us":[{"start":0,"end":100000}]}]}]}。
members和parts必须与输入逐个同序；证据区间只填各part实际源窗内的可见阶段。
不能确认同次实验或关键动作结果、阶段错位时拒绝，不因素材不足放行。
"""
    if context.get("allow_experiment_phase_speedup") is True:
        prompt += ("\n卫品吸水组必须完整呈现倒液、液体吸收和纸巾接触后提起展示结果，三个阶段均可按需加速且无固定倍速上限。"
                   "仍须根据实际切片确认各阶段和结果清楚可辨，不将加速画面作为真实耗时证据。")
    return editor._call(prompt, path.with_suffix(".json"), path)


def install(plan, key, group):
    continuity.validate(group)
    plan.setdefault("continuity_reviews", {})[key] = copy.deepcopy(group)
    for member in group["input"]["members"]:
        pad = member["target_us"] - member["audio_duration_us"]
        parts = [{"shot_id": p["shot_id"], "source_start_us": p["start_us"],
                  "audio_duration_us": p["target_us"] - (pad if n == len(member["parts"]) - 1 else 0)}
                 for n, p in enumerate(member["parts"])]
        if any(p["audio_duration_us"] <= 0 for p in parts):
            raise ValueError("EXPERIMENT_PHASE_DURATION_CONFLICT")
        row = {"index": member["index"], "continuity_key": key, "parts": parts,
               "reason": "reviewed same-experiment phase fit"}
        plan["rows"] = [row if r["index"] == member["index"] else r for r in plan["rows"]]


def validate_plan(manifest, analysis, plan, group, narration_timing):
    members = [next(s for s in plan["sentences"] if s["index"] == m["index"])
               for m in group["input"]["members"]]
    context = _context(manifest, analysis, members, narration_timing, group["evidence"]["pair"],
                       group["evidence"]["motion"], group["input"]["pair_shot_ids"])
    # Existing bounded groups predate the digest. Their underlying pair/motion
    # artifacts were validated while reconstructing the allocation above.
    if "evidence_digest" not in group["input"]:
        context.pop("evidence_digest")
    if context != group["input"]:
        raise ValueError("CONTINUITY_INPUT_CHANGED")
    from . import ai_editor as editor
    rejected = editor.qc_rejected_shots(manifest)
    for member in context["members"]:
        feedback = (manifest.get("ai_refill_feedback") or {}).get(str(member["index"])) or {}
        for part in member["parts"]:
            row = {"video": part["source_identity"]["path"], "ai_selection": part,
                   "source_start": part["start_us"] / 1e6, "source_end": part["end_us"] / 1e6}
            if editor._repeats_failed_cut(row, feedback):
                raise ValueError("AI_REFILL_EXPERIMENT_REPEATED_FAILED_WINDOW")
        if any(p["shot_id"] in rejected.get(member["index"], set()) for p in member["parts"]):
            raise ValueError("CONTINUITY_INPUT_CHANGED")


def _clear_superseded_scene(segment):
    """A verified paired cut supersedes the earlier single-source absorption decision."""
    rule = segment.get("scene_rule") or {}
    if rule and rule.get("id") != "absorb_test":
        return segment
    reason = segment.get("visual_missing_reason_code") or ""
    placeholder = segment.get("visual_missing") and reason.startswith("SCENE_")
    if not rule and not placeholder:
        return segment
    row = dict(segment)
    row.pop("scene_rule", None)
    if placeholder or row.get("scene_preview_unresolved"):
        for key in ("scene_preview_unresolved", "preview_unresolved", "preview_reason"):
            row.pop(key, None)
        row["degraded_no_match"] = False
    if placeholder:
        for key in ("visual_missing", "visual_missing_reason_code", "visual_missing_reason"):
            row.pop(key, None)
        if row.get("window_reason") == reason:
            row.pop("window_reason")
        if (row.get("operator_note") or "").endswith("已留黑画面，保留旁白和字幕。补拍或换素材后重跑该句。"):
            row.pop("operator_note")
    return row


def assemble_member(manifest, plan, group, index, original):
    member = next(m for m in group["input"]["members"] if m["index"] == index)
    observed = next(m for m in group["review"]["members"] if m["index"] == index)
    missing = [p for p in member["selling_points"] if p["id"] not in observed["supported_point_ids"]]
    parts = []
    for cut, fact in zip(member["parts"], observed["parts"]):
        start, end, target = cut["start_us"], cut["end_us"], cut["target_us"]
        observation = {**observed, **fact, "confidence": group["review"]["confidence"]}
        playback = ({"schema": identity.playback.OBSERVATION_SCHEMA, "kind": "process",
            "observed": fact["observation"], "start_us": start, "end_us": end} if cut["process"] else None)
        if group["input"].get("allow_experiment_phase_speedup") is True:
            playback = {"schema": identity.playback.OBSERVATION_SCHEMA, "kind": "experiment_phase",
                "observed": fact["observation"], "start_us": start, "end_us": end,
                "phase": cut["experiment_phase"], "group_policy": "visible_three_stages"}
        observation["playback_observation"] = playback
        item = {"ok": True, "video": cut["source_identity"]["path"], "shot_id": cut["shot_id"],
            "temporary_shot_id": cut["shot_id"], "text": member["text"],
            "source_start": start / 1e6, "source_end": end / 1e6, "duration": (end - start) / 1e6,
            "source_start_us": start, "source_end_us": end, "segment_us": target,
            "segment_s": target / 1e6, "material_s": (end - start) / 1e6,
            "speed": (end - start) / target, "video_speed": (end - start) / target,
            # The group has already solved the shared budget. A one-part
            # member must not be independently cropped/re-timed by the engine.
            "speed_range_override": [(end - start) / target, (end - start) / target],
            "preferred_speed": 1.0, "playback_observation": playback, "time_sensitive": observed["time_sensitive"],
            "visual_role": "direct_evidence" if observed["time_sensitive"] else "product_display",
            "guard_override": [0, 0], "scene_source_start": start / 1e6, "scene_source_end": end / 1e6,
            "guard_source_start": start / 1e6, "guard_source_end": end / 1e6,
            "continuity_group": group, "action_completeness_scope": "paired_experiment_group",
            "selling_points": member["selling_points"], "uncovered_selling_points": missing,
            "evidence_intervals": [{"start": e["start"] / 1e6, "end": e["end"] / 1e6,
                "claim": fact["observation"], "evidence_source": "ai_editor_pixel_review"}
                for e in fact["evidence_intervals_us"]]}
        item["ai_selection"] = identity.make_receipt(item, index=index, sentence=member,
            observation=observation, source_identity=cut["source_identity"], identity=plan["identity"],
            task_speed_cap=group["input"].get("task_absorb_speed_cap"))
        parts.append(item)
    segment = {**original, **parts[0], "selection_mode": "ai_editor", "sentence_kind": member["kind"],
        "claim_text": member["text"], "audio_duration_us": member["audio_duration_us"], "timing_certainty": "audio",
        "action_complete": True, "has_subject": True, "persist_to_library": False,
        "degraded_no_match": False, "formal_rejected": False,
        "multi_shot": len(parts) > 1, "span_parts": parts if len(parts) > 1 else None,
        "window_solution": {k: parts[0][k] for k in ("source_start_us", "source_end_us", "segment_s", "speed")}}
    if len(parts) > 1:
        observation = {**observed, **observed["parts"][0], "confidence": group["review"]["confidence"],
                       "playback_observation": parts[0]["playback_observation"]}
        segment["ai_selection"] = identity.make_receipt(segment, index=index, sentence=member,
            observation=observation, source_identity=member["parts"][0]["source_identity"], identity=plan["identity"],
            task_speed_cap=group["input"].get("task_absorb_speed_cap"))
    check = identity.validate_segment_receipt(segment, index=index, manifest=manifest, verify_source=True)
    if not check["ok"]:
        raise ValueError("CONTINUITY_" + ";".join(check["reasons"]))
    return _clear_superseded_scene(segment)


def repair(manifest, analysis, plan, directory, narration_timing):
    """Keep the single-source strategies; add bounded pairing for uncovered groups."""
    from . import ai_editor as editor
    if identity.rules_identity(manifest)["category"] != "weipin":
        return False
    shots = {s["shot_id"]: s for s in analysis.get("shots", []) if s.get("status") == "ready_for_matching"}
    rows = {r["index"]: r for r in plan.get("rows") or [] if isinstance(r, dict) and "index" in r}
    if len(rows) != len(plan["sentences"]):
        return False
    changed = False
    for members in groups(plan["sentences"]):
        indices = [s["index"] for s in members]
        keys = {rows[i].get("continuity_key") for i in indices}
        if len(keys) == 1 and continuity.is_experiment(
                (plan.get("continuity_reviews") or {}).get(next(iter(keys)))):
            continue
        targets = set(manifest.get("ai_refill_indices") or [])
        if targets and not set(indices) <= targets:
            continue  # A directed single-row refill may not replace accepted neighbours.
        rejected = editor.qc_rejected_shots(manifest)
        def candidates(role):
            result = []
            for sentence in members:
                if sentence["experiment_role"] != role:
                    continue
                i = sentence["index"]
                result += [p.get("shot_id") for p in rows[i].get("parts", [])]
                result += [r["shot_id"] for r in plan.get("reviews", {}).get(str(i), []) if r.get("verdict") == "matched"]
                result += sentence.get("candidate_ids", [])[:editor.ROUND_SIZE]
            return [sid for sid in dict.fromkeys(result) if sid in shots
                    and not any(sid in rejected.get(i, set()) for i in indices)][:editor.ROUND_SIZE]
        first_ids, second_ids = candidates("absorption"), candidates("rewet")
        # A one-clause group may have no spoken cue for the other side. Retrieve
        # verification and pour sources from the task pool, including non-neighbours.
        def other_side(seed, hints):
            def score(shot):
                facts = " ".join(str(shot.get(key, "")) for key in ("description", "action", "result",
                                  "evidence_tags", "action_segments"))
                return -sum(word in facts for word in hints)
            pool = [sid for sid in sorted(shots, key=lambda sid: score(shots[sid]))
                    if not any(sid in rejected.get(i, set()) for i in indices)]
            return list(dict.fromkeys(seed + pool))[:editor.ROUND_SIZE]
        if not second_ids:
            second_ids = other_side([], ("纸巾", "按压", "擦拭", "提起", "反渗")) + first_ids[:1]
        if not first_ids:
            first_ids = other_side([], ("倾倒", "倒液", "倒水", "吸收", "蓝水")) + second_ids[:1]
        # Consecutive files in one folder (IMG_218 -> IMG_219) are usually the
        # pour and its verification. Retrieve them even when the per-sentence
        # shortlist missed one side; the joint pixel review still decides.
        def neighbours(ids, forward):
            found = []
            for sid in ids:
                for other, shot in shots.items():
                    if other in ids or other in found or any(other in rejected.get(i, set()) for i in indices):
                        continue
                    a, b = (shots[sid]["video"], shot["video"]) if forward else (shot["video"], shots[sid]["video"])
                    if experiment_contract.adjacency_hint(a, b)["adjacent"]:
                        found.append(other)
            return found[:ADJACENT_EXTRA]
        first_base, second_base = list(first_ids), list(second_ids)
        second_ids = second_base + neighbours(first_base, True)
        first_ids = first_base + neighbours(second_base, False)
        # A bounded search should cover different hypotheses rather than spend
        # every review on one pour paired with several verification sources;
        # adjacent-file pairs are tried first.
        from ..vision import experiment_evidence
        preferred = [pair for pair in experiment_evidence.preferred_pairs(_scope(manifest, analysis), shots)
                     if not any(sid in rejected.get(i, set()) for sid in pair for i in indices)]
        pending_pairs = list(dict.fromkeys(preferred + [(a, b) for a in first_ids for b in second_ids]))
        preferred_set = set(preferred)
        pairs, seen_a, seen_b = [], set(), set()
        while pending_pairs:
            chosen = min(pending_pairs, key=lambda p: (
                p not in preferred_set,
                not experiment_contract.adjacency_hint(shots[p[0]]["video"], shots[p[1]]["video"])["adjacent"],
                (p[0] in seen_a) + (p[1] in seen_b),
                (first_ids.index(p[0]) if p[0] in first_ids else len(first_ids))
                + (second_ids.index(p[1]) if p[1] in second_ids else len(second_ids))))
            pending_pairs.remove(chosen); pairs.append(chosen)
            seen_a.add(chosen[0]); seen_b.add(chosen[1])
        scope = identity.digest({"indices": indices, "sentences": members,
                                 "rules": identity.rules_identity(manifest),
                                 "sampling_protocol": "dense-pair-v2"})[:24]
        attempts = plan.setdefault("experiment_attempts", {}).setdefault(scope, {})
        for first, second in pairs:
            key = identity.digest([first, second, identity.source_identity(shots[first]["video"]),
                identity.source_identity(shots[second]["video"]),
                [shots[s][k] for s in (first, second) for k in ("source_start", "source_end")]])[:24]
            previous = attempts.get(key)
            if previous and previous.get("status") == "rejected":
                continue
            if not previous and len(attempts) >= PAIR_LIMIT:
                break
            attempts[key] = {"status": "reviewing", "shot_ids": [first, second]}
            editor._save(directory / "plan.json", plan)
            root = directory / "experiments" / scope / key
            try:
                pair, motion = observe_pair(manifest, analysis, shots[first], shots[second], root)
                try:
                    context = _context(manifest, analysis, members, narration_timing, pair, motion, (first, second))
                except ValueError as exc:
                    absorption_text = "".join(s["text"] for s in members if s["experiment_role"] == "absorption")
                    if ("DURATION_CONFLICT" not in str(exc)
                            or not experiment_events.allows_absorption_focus(absorption_text)):
                        raise
                    pair, motion = observe_pair(manifest, analysis, shots[first], shots[second], root,
                                                focus="absorption_focus")
                    context = _context(manifest, analysis, members, narration_timing, pair, motion, (first, second))
                for member in context["members"]:
                    locked = plan.get("_locked_members", {}).get(member["index"])
                    if locked is not None and member["parts"] != locked:
                        raise ValueError("EXPERIMENT_ACCEPTED_NEIGHBOUR_CHANGED")
                reviewed = review_group(manifest, context, root)
                group = continuity.validate({"schema": continuity.EXPERIMENT_SCHEMA, "input": context,
                    "review": reviewed, "evidence": {"pair": pair, "motion": motion}})
                group_key = identity.digest([continuity.EXPERIMENT_SCHEMA, context])[:24]
                trial = copy.deepcopy(plan); install(trial, group_key, group)
                try:
                    editor._assemble(manifest, analysis, trial, narration_timing=narration_timing, verify_sources=True)
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    from .ai_editor_recovery import repairable_index, only_outside_sequence_conflicts
                    other_index = repairable_index(exc)
                    if other_index in indices or other_index is None:
                        checked = editor._assemble(manifest, analysis, trial, narration_timing=narration_timing,
                                                   verify_sources=True, validate_sequence=False)
                        if not only_outside_sequence_conflicts(checked["segments"], manifest, set(indices)):
                            raise exc
                plan.update(trial)
                plan["experiment_attempts"][scope][key]["status"] = "accepted"
                plan.setdefault("continuity_repairs", []).append({"indices": indices, "review_key": group_key,
                    "reason": "paired experiment with observed events and local phase speeds"})
                editor._save(directory / "plan.json", plan)
                changed = True
                break
            except (ValueError, KeyError, TypeError, OSError) as exc:
                transient = editor.failure_fields(str(exc)).get("failure_class") == "vision"
                attempts[key].update(status="retryable" if transient else "rejected", reason=str(exc))
                editor._save(directory / "plan.json", plan)
                if transient:
                    raise
        if not all(continuity.is_experiment((plan.get("continuity_reviews") or {}).get(
                r.get("continuity_key"))) for r in plan["rows"] if r["index"] in indices):
            # Independently plausible shots cannot bypass a required same-experiment check.
            raise ValueError(f"AI_EDITOR_EXPERIMENT_GROUP_PENDING:{indices[0]}:pairing_or_phase_fit_unconfirmed")
    return changed


def certify_rule_groups(manifest, analysis, matched, directory, narration_timing):
    """Bind legacy and mixed-mode absorption groups to the same source and cut review."""
    from . import ai_editor as editor
    from .. import rulepack
    from ..policy import scene_rules
    if identity.rules_identity(manifest)["category"] != "weipin":
        return
    segments = matched.get("segments") or (matched.get("manifest") or {}).get("segments") or []
    rules = rulepack.for_manifest(manifest)
    shots = {s["shot_id"]: s for s in analysis.get("shots", [])}
    detected = [g["positions"] for g in scene_rules._split_groups(segments, rules, shots, category="weipin")
                if g["scene"]["id"] == "absorb_test"]
    # A plain match or preview gap can lack scene_rule and a selected video.
    # A clear narrated experiment still needs the same complete evidence.
    identified = [{"index": i, "text": row.get("text"), "scene_ids": (row.get("ai_selection") or {}).get("sentence_scene_ids")}
                  for i, row in enumerate(segments, 1)]
    detected.extend([m["index"] - 1 for m in run] for run in groups(identified))
    existing = {tuple(i - 1 for i in rule["group"]) for row in segments
                if (rule := row.get("scene_rule") or {}).get("id") == "absorb_test" and rule.get("group")}
    covered = set()
    candidate_groups = sorted(existing) + [tuple(p) for p in detected]
    # NOTE: A single-source placeholder can split a complete narrated experiment into smaller runs.
    for positions in sorted(candidate_groups, key=len, reverse=True):
        if not positions or covered.intersection(positions):
            continue
        covered.update(positions)
        first = positions[0]
        old = segments[first].get("continuity_group")
        if (continuity.is_experiment(old) and old["input"].get("allow_experiment_phase_speedup")
                and all(segments[i].get("continuity_group") == old for i in positions)):
            if [m["index"] for m in old["input"]["members"]] == [i + 1 for i in positions] and all(
                    identity.validate_segment_receipt(segments[i], index=i + 1, manifest=manifest,
                                                      verify_source=True)["ok"] for i in positions):
                for i in positions:
                    segments[i] = _clear_superseded_scene(segments[i])
                continue
        sentences, rows, reviews = [], [], {}
        for pos, row in enumerate(segments):
            index = pos + 1
            sentence = editor._heuristic_sentence(index, row.get("text", ""))
            sid = row.get("temporary_shot_id")
            candidates = editor._rule_shortlist_ids(matched, analysis, index)
            sentence.update(scene_ids=[], experiment_role="other",
                            candidate_ids=list(dict.fromkeys(([sid] if sid else []) + candidates)))
            if pos not in positions:
                sentence["experiment_role"] = "frozen"
            if pos in positions:
                sentence["experiment_role"] = ("rewet" if
                    editor._absorb_sentence_phase(sentence["text"]) == "wipe" else "absorption")
            sentences.append(sentence)
            rows.append({"index": index, "parts": [{"shot_id": sid}] if sid else []})
            reviews[str(index)] = []
        plan = {"sentences": sentences, "rows": rows, "reviews": reviews, "display_reviews": [], "coverage": {},
                "identity": {"model": editor._editor_model(), "editorial_rules": identity.rules_identity(manifest)},
                "_allow_frozen_rule_segments": True,
                "_frozen_segments": {i + 1: s for i, s in enumerate(segments) if i not in positions}}
        root = directory / ("absorb-group-" + str(first + 1))
        repair(manifest, analysis, plan, root, narration_timing)
        result = editor._assemble(manifest, analysis, plan, narration_timing=narration_timing, verify_sources=True)
        require_complete(sentences, result["segments"])
        for i in positions:
            segments[i] = result["segments"][i]
    if covered:
        matched["selection_mode"] = matched["manifest"]["selection_mode"] = "rule_then_ai"
        matched["segments"] = matched["manifest"]["segments"] = segments
        for field, counter, flag, marker, value in (
            ("visual_missing_matches", "visual_missing_count", "visual_missing", "source", "scene_rules"),
            ("preview_unresolved", "preview_unresolved_count", "preview_unresolved", "type", "SCENE_RELATED_PREVIEW_UNCERTIFIED"),
        ):
            resolved = {str(i + 1) for i in covered if not segments[i].get(flag)}
            for report in (matched, matched["manifest"]):
                if field in report:
                    report[field] = [item for item in report[field]
                                     if not isinstance(item, dict) or str(item.get("index")) not in resolved
                                     or item.get(marker) != value]
                    if counter in report:
                        report[counter] = len(report[field])


def refill_groups(manifest, analysis, by_index, wanted, directory, narration_timing):
    """Recheck the whole pair while freezing accepted neighbours and all outside cuts."""
    from . import ai_editor as editor
    affected = {}
    for index in wanted:
        group = by_index.get(index, {}).get("continuity_group")
        if continuity.is_experiment(group):
            affected[identity.digest(group)] = group
    replaced, errors = set(), {}
    originals = timing.split_sentences(manifest)
    shots = {s["shot_id"]: s for s in analysis.get("shots", [])}
    for old in affected.values():
        indices = {m["index"] for m in old["input"]["members"]}
        targets = indices.intersection(wanted)
        plan_path = directory / ("group-" + identity.digest(sorted(indices))[:16])
        try:
            for i, item in by_index.items():
                if (i in indices or identity.is_ai_segment(item)) and not identity.validate_segment_receipt(
                        item, index=i, manifest=manifest, verify_source=True)["ok"]:
                    raise ValueError("AI_REFILL_FROZEN_INVALID")
            sentences, rows, reviews = [], [], {}
            old_members = {m["index"]: m for m in old["input"]["members"]}
            for index, original in enumerate(originals, 1):
                selected = by_index[index]
                member = old_members.get(index)
                receipt = selected.get("ai_selection")
                if receipt:
                    sentence = {"index": index, "text": timing.sentence_text(original),
                        "kind": receipt["sentence_kind"], "selling_points": receipt["selling_points"],
                        "scene_ids": receipt["sentence_scene_ids"]}
                else:
                    sentence = editor._heuristic_sentence(index, timing.sentence_text(original))
                sentence.update(experiment_role=member["experiment_role"] if member else "other",
                    candidate_ids=list(shots))
                sentences.append(sentence)
                duration, _ = timing.duration_for(narration_timing, index, segment=original)
                physical = selected.get("span_parts") or [selected]
                rows.append({"index": index, "parts": [{"shot_id": p["ai_selection"]["shot_id"],
                    "audio_duration_us": max(1, round(duration / len(physical)))} for p in physical] if receipt else []})
                reviews[str(index)] = [{**p["ai_selection"], "verdict": "matched", "same_product": True}
                                      for p in physical] if receipt else []
            plan = {"sentences": sentences, "rows": rows, "reviews": reviews, "display_reviews": [],
                "coverage": {}, "identity": {"model": editor._editor_model(),
                    "editorial_rules": identity.rules_identity(manifest)},
                "_allow_frozen_rule_segments": True,
                "_frozen_segments": {i: copy.deepcopy(row) for i, row in by_index.items() if i not in indices},
                "_locked_members": {m["index"]: copy.deepcopy(m["parts"]) for m in old["input"]["members"]
                                    if m["index"] not in targets}}
            # Resume the finite pairing budget of this directed group repair.
            try:
                previous = json.loads((plan_path / "plan.json").read_text("utf-8"))
                if previous.get("refill_input") == identity.digest([manifest, old, sorted(targets)]):
                    plan["experiment_attempts"] = previous.get("experiment_attempts", {})
            except (OSError, ValueError):
                pass
            plan["refill_input"] = identity.digest([manifest, old, sorted(targets)])
            repair({**manifest, "ai_refill_indices": sorted(indices)}, analysis, plan, plan_path, narration_timing)
            result = editor._assemble(manifest, analysis, plan, narration_timing=narration_timing, verify_sources=True)
            for index in indices:
                by_index[index] = result["segments"][index - 1]
            replaced.update(targets)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            for index in targets:
                errors[index] = f"AI_REFILL_EXPERIMENT_GROUP_FAILED:{index}:{exc}"
    return replaced, errors
