"""Pixel-grounded candidate review followed by selling-point-first editing.

Legacy semantic scores only retrieve candidates. No legacy claim gate is used
here, and failure never falls back to a keyword-selected packaging shot.
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
from itertools import combinations
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import ai_editor_contract as contract, timing_contract

ROUND_SIZE = 8
# Explore at most one additional round for an editorial alternative after a
# feasible selling-point match. Matching and evidence still take precedence.
ALTERNATIVE_REVIEW_LIMIT = 2 * ROUND_SIZE


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _editor_model() -> str:
    from . import vision_analyzer as vision
    if vision._vision_transport() == "qwen":
        return vision._qwen_settings()["model"] or "qwen-unconfigured"
    if os.environ.get("JY_AI_EDITOR_QWEN", "").strip() == "1":
        return os.environ.get("JY_VISION_TEXT_MODEL", "").strip() or "qwen-unconfigured"
    return os.environ.get("JY_VISION_MODEL") or "glm-5.3-flash"


def _call(prompt: str, path: Path, sheet: Path | None = None) -> dict:
    from . import vision_analyzer as vision, material_cache
    from .file_safety import file_lock
    transport = vision._vision_transport()
    if transport not in {"volc_glm", "qwen"}:
        raise ValueError("AI_EDITOR_REQUIRES_DIRECT_GLM")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Pixel reviews (a sheet) use the text-read Qwen route. Text-only edit
    # planning stays on the visual GLM route. Completed reviews stay valid
    # when only the model route changes.
    editor_qwen = transport == "qwen" or os.environ.get("JY_AI_EDITOR_QWEN", "").strip() == "1"
    text_cfg = (vision._qwen_settings() if transport == "qwen" else
                vision._text_settings() if sheet is not None or editor_qwen else None)
    if editor_qwen and (not text_cfg or not all(text_cfg.values())):
        raise ValueError("AI_EDITOR_QWEN_ROUTE_MISSING")
    if text_cfg:
        route_model, route_endpoint = text_cfg["model"], text_cfg["base_url"]
    else:
        volc = vision._volc_settings()
        route_model, route_endpoint = volc["model"], volc["base_url"]
    identity = {"prompt_sha256": contract.digest(prompt),
                "image_sha256": contract.file_sha256(sheet) if sheet else None,
                "model": route_model, "endpoint": route_endpoint}
    if text_cfg:
        identity["thinking"] = "enable_thinking=false"
    request_path = path.with_suffix(".request.json")
    if path.is_file() and request_path.is_file():
        try:
            previous = json.loads(request_path.read_text(encoding="utf-8"))
            same_input = (isinstance(previous, dict)
                          and previous.get("prompt_sha256") == identity["prompt_sha256"]
                          and previous.get("image_sha256") == identity["image_sha256"])
            if same_input and (not editor_qwen or previous == identity):
                cached = vision._json_from_text(path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    json.dumps(cached, allow_nan=False)
                    return cached
        except (OSError, ValueError, TypeError):
            pass
    (path.with_suffix(".prompt.txt")).write_text(prompt, encoding="utf-8")
    def valid(payload):
        if not isinstance(payload, dict):
            return False
        json.dumps(payload, allow_nan=False)
        return True
    shared = material_cache.cache_root(path.parent) / "editor_calls" / f"{contract.digest(identity)}.json"
    # One transport owns retries and account slots, including after restart.
    with file_lock(shared.with_suffix(".editor"), timeout=7200):
        # Parse timeout from environment, accepting float-formatted seconds such as "360.0".
        raw_timeout = os.environ.get("JY_AI_EDITOR_TIMEOUT_S", os.environ.get("JY_VISION_TIMEOUT", "360"))
        try:
            timeout_s = int(float(str(raw_timeout).strip()))
        except (TypeError, ValueError):
            timeout_s = 360
        if transport == "qwen":
            ok, text = vision._run_qwen_chat(sheet, prompt, shared, validator=valid, timeout=timeout_s)
        elif text_cfg:
            ok, text = vision._run_text_chat(sheet, prompt, shared, validator=valid, timeout=timeout_s)
        else:
            ok, text = vision._run_volc_chat(sheet, prompt, shared, validator=valid, timeout=timeout_s)
    if not ok:
        raise ValueError("AI_EDITOR_CALL_FAILED:" + str(text)[-500:])
    payload = vision._json_from_text(text)
    if not valid(payload):
        raise ValueError("AI_EDITOR_RESPONSE_NOT_OBJECT")
    path.write_text(text, encoding="utf-8")
    _save(request_path, identity)
    return payload


def _inventory(manifest: dict, shots: list[dict], directory: Path) -> list[dict]:
    rows = timing_contract.split_sentences(manifest)
    catalog = [{"shot_id": s["shot_id"], "description": s.get("visual_description") or s.get("description"),
                "action": s.get("action"), "object": s.get("object"), "result": s.get("result"),
                "duration_us": round((float(s["source_end"]) - float(s["source_start"])) * 1e6)} for s in shots]
    prompt = contract.policy_prompt(manifest) + "\n你负责整稿用途分类和宽召回，本轮描述只是索引，不是画面证据。\n" + json.dumps({
        "product": manifest.get("product"), "category": manifest.get("category"),
        "general_notes": manifest.get("shot_notes_general", []),
        "sentences": [{"index": i, "text": timing_contract.sentence_text(s),
                       "notes": s.get("shot_note") or s.get("visual_requirements")} for i, s in enumerate(rows, 1)],
        "catalog": catalog}, ensure_ascii=False) + """
输出JSON对象 {"sentences":[{"index":1,"kind":"selling_point|product_intro|hook",
"selling_points":[{"id":"p1","text":"本句具体卖点"}],"candidate_ids":["最相关shot_id", "其余候选"],"scene_ids":[]}],
"display_pool_ids":["适合包装/拆包/产品展示的shot_id"]}。
每句恰好一行，不改变原文。selling_point列出本句全部具体卖点，其他kind的selling_points为空。
纸品scene_ids必须给出本句语义适用的semantic_contracts场景id，不适用时为空；不能只因某候选有擦拭就把包装抽数句判为清洁。
每句candidate_ids只返回最相关的前8个，display_pool_ids也只返回前8个，避免响应被整库ID挤满。
程序会把未列出的镜头加入后续逐轮扩展。不能因描述不确定就宣布无素材，不用A/B强度或旧合同词表淘汰。
"""
    data = _call(prompt, directory / "inventory.json")
    found = data.get("sentences")
    if not isinstance(found, list) or len(found) != len(rows):
        raise ValueError("AI_EDITOR_SENTENCE_COVERAGE")
    by_index = {}
    known = {str(s["shot_id"]) for s in shots}

    def resolve_ids(ids: list[str]) -> tuple[list[str], list[str], dict[str, str]]:
        resolved, ignored, aliases = [], [], {}
        for item in ids:
            if item in known:
                resolved.append(item)
                continue
            # Some models abbreviate a source_###_shot_### ID to source_###.
            # Only a unique exact source prefix is unambiguous; all candidates
            # still go through the same independent pixel review afterwards.
            matches = ([shot_id for shot_id in known
                        if re.fullmatch(re.escape(item) + r"_shot_\d+", shot_id)]
                       if re.fullmatch(r"source_\d+", item) else [])
            if len(matches) == 1:
                aliases[item] = matches[0]
                resolved.append(matches[0])
            else:
                ignored.append(item)
        return resolved, ignored, aliases
    for row in found:
        index = row.get("index") if isinstance(row, dict) else None
        if type(index) is not int or not 1 <= index <= len(rows) or index in by_index or row.get("kind") not in contract.KINDS:
            raise ValueError("AI_EDITOR_CLASSIFICATION_INVALID")
        points = row.get("selling_points")
        known_scenes = {scene["id"] for scene in contract.editorial_context(manifest)["rules"].get("semantic_contracts", [])}
        scene_ids = row.get("scene_ids", [] if not known_scenes else None)
        if not isinstance(scene_ids, list) or any(scene not in known_scenes for scene in scene_ids):
            raise ValueError("AI_EDITOR_SENTENCE_SCENE_INVALID")
        row["scene_ids"] = scene_ids
        if not isinstance(points, list) or any(not isinstance(p, dict) or not p.get("id") or not p.get("text") for p in points):
            raise ValueError("AI_EDITOR_POINTS_INVALID")
        if len({p["id"] for p in points}) != len(points) or (row["kind"] == "selling_point") != bool(points):
            raise ValueError("AI_EDITOR_POINTS_INVALID")
        ids = row.get("candidate_ids")
        if not isinstance(ids, list) or any(not isinstance(s, str) for s in ids):
            raise ValueError("AI_EDITOR_CANDIDATE_UNKNOWN")
        ids, row["inventory_ignored_candidate_ids"], row["inventory_resolved_candidate_ids"] = resolve_ids(ids)
        by_index[index] = {**row, "text": timing_contract.sentence_text(rows[index - 1]),
                           "candidate_ids": list(dict.fromkeys(ids + [str(s["shot_id"]) for s in shots]))}
    display = data.get("display_pool_ids")
    if not isinstance(display, list) or any(not isinstance(s, str) for s in display):
        raise ValueError("AI_EDITOR_DISPLAY_POOL_INVALID")
    display, ignored_display, resolved_display = resolve_ids(display)
    result = [by_index[i] for i in range(1, len(rows) + 1)]
    for row in result:
        row["inventory_ignored_display_ids"] = ignored_display
        row["inventory_resolved_display_ids"] = resolved_display
        if row["kind"] != "selling_point":
            row["candidate_ids"] = list(dict.fromkeys(display + [str(s["shot_id"]) for s in shots]))
    return result


def _review_media(shot: dict, directory: Path, *, dense: bool = False) -> dict:
    from . import vision_analyzer as vision, platform_env, shot_analyzer, material_cache
    from .runtime_policy import media_slot
    start, end = float(shot["source_start"]), float(shot["source_end"])
    if not math.isfinite(start + end) or not 0 <= start < end:
        raise ValueError("AI_EDITOR_SOURCE_WINDOW_INVALID")
    source = str(shot["video"])
    before = contract.source_identity(source)
    count = 16 if dense else 8
    last = end - min(.04, (end - start) / 10)
    times = [start + (last - start) * n / (count - 1) for n in range(count)]
    def prepare_frames(frame_dir):
        ffmpeg, _ = platform_env.find_ffmpeg()
        if not ffmpeg:
            raise ValueError("AI_EDITOR_FFMPEG_MISSING")
        frames = []
        with media_slot(timeout=600) as handle:
            if not handle.acquired:
                raise ValueError("AI_EDITOR_MEDIA_SLOT_TIMEOUT")
            for i, timestamp in enumerate(times):
                target = frame_dir / f"{i:02d}.jpg"
                shot_analyzer.extract_frame(source, timestamp, target, ffmpeg=ffmpeg)
                if not target.is_file() or target.stat().st_size == 0:
                    raise ValueError("AI_EDITOR_FRAME_EXTRACTION_FAILED")
                frames.append({"path": str(target), "timestamp": timestamp})
        return frames
    frames = material_cache.cached_frames(shot, directory, prepare_frames,
        protocol=f"candidate-even-window-{count}-v1")
    tiles = [{**frame, "label": f"t={frame['timestamp']:.3f}s"} for frame in frames]
    def prepare_sheet(target):
        with media_slot(timeout=600) as handle:
            if not handle.acquired:
                raise ValueError("AI_EDITOR_MEDIA_SLOT_TIMEOUT")
            return vision._write_sheet(tiles, target, columns=4, tile_width=420, tile_height=300,
                                       ffmpeg=platform_env.find_ffmpeg()[0])
    sheet = material_cache.cached_sheet(tiles, directory, prefix=f"candidate{count}",
        parameters={"columns": 4, "tile_width": 420, "tile_height": 300}, prepare=prepare_sheet)
    if sheet is None:
        raise ValueError("AI_EDITOR_CONTACT_SHEET_FAILED")
    if before != contract.source_identity(source):
        raise ValueError("AI_EDITOR_SOURCE_CHANGED_DURING_REVIEW")
    return {"start": start, "end": end, "before": before, "times": times, "sheet": sheet}


def _prepare_review_round(shots: list[dict], directory: Path, *, dense: bool = False) -> None:
    from .runtime_policy import load_policy
    with ThreadPoolExecutor(max_workers=load_policy().prep_workers) as pool:
        futures = [pool.submit(_review_media, shot, directory, dense=dense) for shot in shots]
        # Finish every preparation before the first review request is admitted.
        # A preparation error is reported by that candidate's review below.
        for future in futures:
            try:
                future.result()
            except (ValueError, OSError, TypeError, KeyError, TimeoutError):
                pass


def _review(manifest: dict, sentence: dict, shot: dict, directory: Path, *, dense: bool = False) -> dict:
    media = _review_media(shot, directory, dense=dense)
    start, end, before, times, sheet = (media[k] for k in ("start", "end", "before", "times", "sheet"))
    key = contract.digest([sentence["index"], shot["shot_id"], before, dense])[:24]
    prompt = contract.policy_prompt(manifest) + "\n只根据附图判断，所有时间戳均为原素材秒数。\n" + json.dumps({
        "product": manifest.get("product"), "category": manifest.get("category"),
        "sentence": sentence, "shot_id": shot["shot_id"],
        "whole_script": [timing_contract.sentence_text(s) for s in timing_contract.split_sentences(manifest)],
        "scene_us": {"start": round(start * 1e6), "end": round(end * 1e6)}}, ensure_ascii=False) + """
输出JSON对象：{"verdict":"matched|mismatch|uncertain", "same_product":true,
"basis":"editorial_fit|visible_result|readable_label|product_display", "observation":"实际可见事实",
"reason":"为什么适合或不适合", "confidence":0.8, "supported_point_ids":["p1"],
"evidence_intervals_us":[{"start":1000000,"end":2000000}], "time_sensitive":false, "scene_ids":[]}。
scene_ids只列出本句语义适用且本镜实际支持的纸品semantic_contracts场景id；不支持或本句不涉及的场景不能列入，不适用时为空。
共享展示池句子index=0时，判断是否为同商品包装/拆包/取出/手持或本体展示，不承担功能证明。
卖点可以只支持其中几项，必须如实返回支持的id，不能凭口播推断。区间必须在提供的scene_us内，
包含支持判断必需的完整动作和结果；感受性展示按实际贴切区间。看不清返回uncertain，不能凭模型强度标签推断。
具体香型、成分、数量和数值须有画面可读的对应标称或可确认的直接证据；香薰、花朵等氛围只能表达一般香味体验，不能证明某种具体香型。检测仪外观或数字本身也不能证明仪器用途与检测结论。
相邻口播可连续承接同一实验；结合全稿，只返回本句实际承担的动作阶段/结果区间，不要求每一句从头重播整个实验。
sentence.audio_durations_us是真实配音时长。请给最短但足够支持判断的一个紧凑片段，不要把整条素材或多次重复演示全部列为必需证据。
优先返回足以体现卖点的完整源区间，不因比配音长而否决正确镜头。程序允许按需变速，最高20倍。
轻薄/柔软/舒适展示不要求从第一次持握一直播到最后一次抚摸，也不要为凑配音时长裁掉关键动作与结果。
evidence_intervals_us内各区间会被当作全部必须保留，不能把多个可替代的重复演示都列入；选最贴切的一次。
性能实验仍要保留本句必要过程和可见结果，无法在时长内支持时如实返回不确定或部分卖点，不为缩短而删除必需证据。
"""
    result = _call(prompt, directory / "reviews" / f"{key}.json", sheet)
    if result.get("verdict") not in {"matched", "mismatch", "uncertain"} or type(result.get("same_product")) is not bool:
        raise ValueError("AI_EDITOR_REVIEW_INVALID")
    confidence = result.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("AI_EDITOR_REVIEW_CONFIDENCE_INVALID")
    if not all(isinstance(result.get(k), str) and result[k].strip() for k in ("observation", "reason")):
        raise ValueError("AI_EDITOR_REVIEW_OBSERVATION_MISSING")
    if not result["same_product"] or confidence < .6:
        result["verdict"] = "uncertain" if result["same_product"] else "mismatch"
    if result["verdict"] == "matched":
        context = contract.editorial_context(manifest)
        scenes = context["rules"].get("semantic_contracts", [])
        known_scenes = {scene["id"] for scene in scenes}
        scene_ids = result.get("scene_ids", [] if not known_scenes else None)
        if not isinstance(scene_ids, list) or any(scene not in known_scenes for scene in scene_ids):
            raise ValueError("AI_EDITOR_SCENE_INVALID")
        result["preferred_speed"] = _scene_preference(manifest, sentence, result)
        ids = result.get("supported_point_ids")
        allowed = {p["id"] for p in sentence.get("selling_points", [])}
        if (not isinstance(ids, list) or any(p not in allowed for p in ids)
                or (sentence["kind"] == "selling_point" and not ids) or result.get("basis") not in contract.BASES
                or type(result.get("time_sensitive")) is not bool):
            raise ValueError("AI_EDITOR_REVIEW_SUPPORT_INVALID")
        intervals = result.get("evidence_intervals_us")
        if (isinstance(intervals, list) and len(intervals) == 2
                and all(type(value) is int for value in intervals)):
            intervals = [{"start": intervals[0], "end": intervals[1]}]
            result["evidence_intervals_us"] = intervals
        if not isinstance(intervals, list) or not intervals:
            raise ValueError("AI_EDITOR_EVIDENCE_INVALID")
        for ev in intervals:
            if (not isinstance(ev, dict) or any(type(ev.get(k)) is not int for k in ("start", "end"))
                    or not round(start * 1e6) <= ev["start"] < ev["end"] <= round(end * 1e6)):
                raise ValueError("AI_EDITOR_EVIDENCE_INVALID")
    durations = sentence.get("audio_durations_us") or []
    valid_durations = [v for v in durations if type(v) is int and v > 0]
    intervals = result.get("evidence_intervals_us") or []
    broad = (result["verdict"] == "matched" and valid_durations and intervals
             and max(ev["end"] for ev in intervals) - min(ev["start"] for ev in intervals)
             > 2 * min(valid_durations))
    return {**result, "dense_required": not dense and (result["verdict"] == "uncertain" or bool(broad)),
            "shot_id": shot["shot_id"], "source_identity": before,
            "frame_times_us": [round(t * 1e6) for t in times], "sheet": str(sheet)}


def _review_pool(manifest: dict, sentence: dict, shots: dict, directory: Path, audio: dict, *,
                 exhaustive: bool = False,
                 existing_reviews: list[dict] | None = None) -> tuple[list[dict], dict]:
    from . import shot_analyzer
    partial_preview = manifest.get("delivery_mode") == "preview" and manifest.get("allow_partial_selling_points") is True
    def feasible(good: list[dict]) -> bool:
        durations = sentence.get("audio_durations_us") or [timing_contract.duration_for(
            audio, sentence["index"], segment={"text": sentence["text"]})[0]]
        for duration in durations:
            available = False
            for size in range(1, min(len(good), int(manifest.get("span_max_parts", 3))) + 1):
                for group in combinations(good, size):
                    group_points = {p for r in group for p in r.get("supported_point_ids", [])}
                    if required and not (bool(required & group_points) if partial_preview else required <= group_points):
                        continue
                    observed = [_observed_shot(shots[r["shot_id"]], r) for r in group]
                    segments = tuple(shot_analyzer.span_segment_for(s, manifest=manifest,
                        time_sensitive=r["time_sensitive"], video=s["video"], shot_id=s["shot_id"])
                        for s, r in zip(observed, group))
                    if timing_contract.solve_span(timing_contract.SpanRequest(segments=segments,
                        audio_duration_us=duration, tail_pad_us=round(float(manifest.get("tail_pad_s", 0)) * 1e6),
                        fps=float(manifest.get("fps", 30)))).ok:
                        available = True
                        break
                if available:
                    break
            if not available:
                return False
        return True
    reviewed_by_id = {str(row.get("shot_id")): row for row in (existing_reviews or [])
                      if isinstance(row, dict) and row.get("shot_id") in shots}
    ids = sentence["candidate_ids"]
    required = {p["id"] for p in sentence["selling_points"]}
    for offset in range(0, len(ids), ROUND_SIZE):
        chunk = [sid for sid in ids[offset:offset + ROUND_SIZE] if sid not in reviewed_by_id]
        if not chunk:
            continue
        _prepare_review_round([shots[sid] for sid in chunk], directory)
        def review(sid: str) -> dict:
            try:
                return _review(manifest, sentence, shots[sid], directory)
            except (ValueError, OSError, TypeError, KeyError) as exc:
                return {"shot_id": sid, "verdict": "uncertain", "error": str(exc)}
        with ThreadPoolExecutor(max_workers=min(20, len(chunk))) as pool:
            futures = {pool.submit(review, sid): sid for sid in chunk}
            completed = {}
            for future in as_completed(futures):
                completed[futures[future]] = future.result()
                _save(directory / f"review_checkpoint_{sentence['index']}.json", {
                    "sentence_index": sentence["index"], "round": offset // ROUND_SIZE + 1,
                    "completed": len(reviewed_by_id) + len(completed),
                    "pending": len(ids) - len(reviewed_by_id) - len(completed),
                    "total": len(ids),
                    "reviews": [reviewed_by_id[s] for s in ids if s in reviewed_by_id] +
                               [completed[s] for s in chunk if s in completed]})
            reviewed_by_id.update(completed)
        dense_ids = [sid for sid in chunk if reviewed_by_id[sid].get("dense_required")]
        if dense_ids:
            _prepare_review_round([shots[sid] for sid in dense_ids], directory, dense=True)
            def dense_review(sid):
                previous = reviewed_by_id[sid]
                try:
                    refined = _review(manifest, sentence, shots[sid], directory, dense=True)
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    refined = {"shot_id": sid, "verdict": "uncertain", "error": str(exc)}
                if previous["verdict"] != "matched":
                    return refined
                if (refined.get("verdict") != "matched"
                        or not set(previous.get("supported_point_ids", [])) <= set(refined.get("supported_point_ids", []))
                        or not set(previous.get("scene_ids", [])) <= set(refined.get("scene_ids", []))
                        or previous.get("time_sensitive") != refined.get("time_sensitive")):
                    return {**previous, "dense_required": False, "refinement_status": "retained_original"}
                return {**refined, "refinement_status": "dense_review"}
            with ThreadPoolExecutor(max_workers=min(20, len(dense_ids))) as pool:
                reviewed_by_id.update(zip(dense_ids, pool.map(dense_review, dense_ids)))
        # Re-observe guarded-out evidence inside the actual usable scene; never
        # clip an existing evidence interval and treat it as a new observation.
        guarded = {}
        for sid in chunk:
            previous = reviewed_by_id[sid]
            if previous.get("verdict") != "matched":
                continue
            shot = shots[sid]
            head = max(0.0, shot_analyzer._number(manifest.get("head_trim_s"), shot_analyzer.DEFAULT_HEAD_TRIM_S))
            tail = max(0.0, shot_analyzer._number(manifest.get("tail_trim_s"), shot_analyzer.DEFAULT_TAIL_TRIM_S))
            lo, hi = float(shot["source_start"]) + head, float(shot["source_end"]) - tail
            if hi <= lo:
                continue
            if any(ev["start"] < round(lo * 1e6) or ev["end"] > round(hi * 1e6)
                   for ev in previous.get("evidence_intervals_us", [])):
                guarded[sid] = {**shot, "source_start": lo, "source_end": hi}
        if guarded:
            _prepare_review_round(list(guarded.values()), directory, dense=True)
            def guarded_review(sid):
                previous = reviewed_by_id[sid]
                try:
                    refined = _review(manifest, sentence, guarded[sid], directory, dense=True)
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    refined = {"verdict": "uncertain", "error": str(exc)}
                if (refined.get("verdict") == "matched"
                        and refined.get("source_identity") == previous.get("source_identity")
                        and set(previous.get("supported_point_ids", [])) <= set(refined.get("supported_point_ids", []))
                        and set(previous.get("scene_ids", [])) <= set(refined.get("scene_ids", []))
                        and previous.get("time_sensitive") == refined.get("time_sensitive")):
                    return {**refined, "guarded_review_status": "confirmed"}
                return {**previous, "guarded_review_status": "unconfirmed"}
            with ThreadPoolExecutor(max_workers=min(20, len(guarded))) as pool:
                reviewed_by_id.update(zip(guarded, pool.map(guarded_review, guarded)))
        reviewed = [reviewed_by_id[sid] for sid in ids if sid in reviewed_by_id]
        good = [r for r in reviewed if r["verdict"] == "matched"]
        covered = {p for r in good for p in r.get("supported_point_ids", [])}
        if not exhaustive and required and (bool(required & covered) if partial_preview else required <= covered) and feasible(good):
            if (len(good) >= 2 or len(reviewed) >= min(ALTERNATIVE_REVIEW_LIMIT, len(ids))
                    or len(reviewed) == len(ids)):
                break
        if not exhaustive and not required and len(good) >= min(3, len(ids)) and feasible(good):
            break
    reviewed = [reviewed_by_id[sid] for sid in ids if sid in reviewed_by_id]
    good = [r for r in reviewed if r["verdict"] == "matched"]
    supported = {point for row in good for point in row.get("supported_point_ids", [])}
    is_feasible = feasible(good)
    uncertain = sum(r["verdict"] == "uncertain" or r.get("guarded_review_status") == "unconfirmed"
                    for r in reviewed)
    exhausted = len(reviewed) == len(ids)
    if is_feasible:
        reason = "READY"
    elif required and not (bool(required & supported) if partial_preview else required <= supported):
        reason = "EVIDENCE_UNKNOWN" if uncertain or not exhausted else "NO_SUPPORT_AFTER_REVIEW"
    elif not required and not good:
        reason = "EVIDENCE_UNKNOWN" if uncertain or not exhausted else "NO_DISPLAY_AFTER_REVIEW"
    else:
        reason = "KNOWN_WINDOWS_INFEASIBLE"
    return reviewed, {"checked": len(reviewed), "total": len(ids), "exhausted": exhausted,
                      "feasible": is_feasible, "reason": reason,
                      "failed_calls": sum("error" in r for r in reviewed),
                      "uncertain": uncertain}


def _scene_preference(manifest: dict, sentence: dict, review: dict) -> float:
    common = set(sentence.get("scene_ids", [])) & set(review.get("scene_ids", []))
    return max((float(scene.get("preferred_speed", 1.0)) for scene in
        contract.editorial_context(manifest)["rules"].get("semantic_contracts", []) if scene["id"] in common), default=1.0)


def _observed_shot(shot: dict, review: dict) -> dict:
    evidence = [{"start": ev["start"] / 1e6, "end": ev["end"] / 1e6,
                 "claim": review["observation"], "evidence_source": "ai_editor_pixel_review"}
                for ev in review["evidence_intervals_us"]]
    role = "direct_evidence" if review.get("time_sensitive") else "product_display"
    minimum, _ = timing_contract.speed_range_for_role(role, time_sensitive=review.get("time_sensitive", False))
    return {**shot, "evidence_intervals": evidence,
            "selection_mode": "ai_editor", "speed_range_override": [minimum, contract.AI_SPEED_MAX],
            "preferred_speed": review.get("preferred_speed", 1.0),
            "role": "direct_evidence" if review.get("time_sensitive") else "product_display",
            "visual_role": "direct_evidence" if review.get("time_sensitive") else "product_display",
            "action_complete": True, "head_waste": 0, "tail_waste": 0}


def _assemble(manifest: dict, analysis: dict, plan: dict, *, narration_timing: dict | None,
              verify_sources: bool = False) -> dict:
    from . import shot_analyzer
    if {k: plan.get("identity", {}).get("editorial_rules", {}).get(k) for k in ("category", "versions")} != contract.rules_identity(manifest):
        raise ValueError("AI_EDITOR_RULES_CHANGED")
    shots = {str(s["shot_id"]): s for s in analysis.get("shots", [])}
    sentences = plan["sentences"]
    rows = plan.get("rows")
    if not isinstance(rows, list) or len(rows) != len(sentences):
        indices = [row.get("index") for row in rows if isinstance(row, dict) and type(row.get("index")) is int] if isinstance(rows, list) else []
        duplicates = sorted({i for i in indices if indices.count(i) > 1})
        missing = sorted(set(range(1, len(sentences) + 1)) - set(indices))
        raise ValueError(f"AI_EDITOR_PLAN_COVERAGE:duplicate={duplicates}:missing={missing}; each index must have exactly one row")
    indexed = {}
    for row in rows:
        i = row.get("index") if isinstance(row, dict) else None
        if type(i) is not int or not 1 <= i <= len(sentences) or i in indexed:
            raise ValueError("AI_EDITOR_PLAN_INDEX_INVALID")
        indexed[i] = row
    output = []
    partial_preview = manifest.get("delivery_mode") == "preview" and manifest.get("allow_partial_selling_points") is True
    unresolved = []
    original = timing_contract.split_sentences(manifest)
    for sentence in sentences:
        index = sentence["index"]
        row = indexed[index]
        parts = row.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError(f"AI_EDITOR_NO_SELECTION:{index}")
        audio_us, certainty = timing_contract.duration_for(narration_timing, index, segment=original[index - 1])
        if any(type(p.get("audio_duration_us")) is not int or p["audio_duration_us"] <= 0 for p in parts) or sum(p["audio_duration_us"] for p in parts) != audio_us:
            raise ValueError(f"AI_EDITOR_AUDIO_ALLOCATION_INVALID:{index}")
        reviews = plan["reviews"].get(str(index), []) if sentence["kind"] == "selling_point" else plan["display_reviews"]
        good = {r["shot_id"]: r for r in reviews if r["verdict"] == "matched"}
        reuse_reason = str(row.get("scarce_reuse_reason") or "").strip()
        coverage = plan.get("coverage", {}).get(str(index), {})
        if reuse_reason and (sentence["kind"] != "selling_point" or len(good) != 1
                             or not coverage.get("exhausted")
                             or type(coverage.get("checked")) is not int
                             or coverage.get("checked") != coverage.get("total")
                             or coverage.get("uncertain", 0)
                             or coverage.get("failed_calls", 0)):
            raise ValueError(f"AI_EDITOR_REUSE_HAS_ALTERNATIVES:{index}")
        supported = set()
        assembled = []
        for n, part in enumerate(parts):
            sid = part.get("shot_id")
            if sid not in good or sid not in shots:
                raise ValueError(f"AI_EDITOR_UNREVIEWED_SELECTION:{index}")
            review, shot = {**good[sid], "scarce_reuse_reason": reuse_reason}, shots[sid]
            review = {**review, "preferred_speed": _scene_preference(manifest, sentence, review)}
            if verify_sources and review["source_identity"] != contract.source_identity(shot["video"]):
                raise ValueError("AI_EDITOR_SOURCE_CONTENT_CHANGED")
            observed = _observed_shot(shot, review)
            # A continuation requests a later in-point; it must retain every
            # evidence interval, otherwise the technical check rejects it.
            if "source_start_us" in part:
                if type(part["source_start_us"]) is not int:
                    raise ValueError("AI_EDITOR_INPOINT_INVALID")
                observed["source_start"] = max(float(shot["source_start"]), part["source_start_us"] / 1e6)
            cfg = {**manifest, "tail_pad_s": manifest.get("tail_pad_s", 0) if n == len(parts) - 1 else 0}
            if "source_start_us" in part:
                # The guard belongs to the original scene, not the requested in-point.
                guard = max(0.0, shot_analyzer._number(manifest.get("head_trim_s"), shot_analyzer.DEFAULT_HEAD_TRIM_S))
                offset = observed["source_start"] - float(shot["source_start"])
                cfg["head_trim_s"] = max(0.0, guard - offset)
            solution = shot_analyzer.candidate_window(observed, audio_duration_us=part["audio_duration_us"],
                manifest=cfg, time_sensitive=review["time_sensitive"])
            if not solution.ok:
                raise ValueError(f"AI_EDITOR_WINDOW_INFEASIBLE:{index}:{sid}:{solution.reason}")
            item = {**solution.to_dict(), "video": shot["video"], "shot_id": sid,
                    "temporary_shot_id": sid, "source_start": solution.source_start_us / 1e6,
                    "source_end": solution.source_end_us / 1e6, "duration": solution.material_us / 1e6,
                    "video_speed": solution.speed, "visual_role": observed["visual_role"],
                    "speed_range_override": observed["speed_range_override"],
                    "preferred_speed": observed["preferred_speed"],
                    "time_sensitive": review["time_sensitive"],
                    "scene_source_start": shot["source_start"], "scene_source_end": shot["source_end"],
                    "evidence_intervals": observed["evidence_intervals"], "text": sentence["text"]}
            part_sentence = {**sentence, "selling_points": [p for p in sentence["selling_points"] if p["id"] in review.get("supported_point_ids", [])]}
            item["ai_selection"] = contract.make_receipt(item, index=index, sentence=part_sentence,
                observation=review, source_identity=review["source_identity"], identity=plan["identity"])
            assembled.append(item)
            supported.update(review.get("supported_point_ids", []))
        missing_points = [p for p in sentence["selling_points"] if p["id"] not in supported]
        if missing_points:
            if not partial_preview or not supported:
                raise ValueError(f"AI_EDITOR_POINTS_UNCOVERED:{index}")
            unresolved.append({"index": index, "text": sentence["text"], "type": "PARTIAL_SELLING_POINTS",
                               "missing_selling_points": missing_points})
        first = assembled[0]
        segment = {**original[index - 1], **first, "selection_mode": "ai_editor",
            "claim_text": sentence["text"], "sentence_kind": sentence["kind"],
            "selling_points": sentence["selling_points"], "audio_duration_us": audio_us,
            "timing_certainty": certainty, "window_solution": {k: v for k, v in first.items() if k in {"ok", "speed", "source_start", "source_end", "segment_s", "material_s", "handle_room_s", "reason", "detail"}},
            "action_complete": True, "has_subject": True, "persist_to_library": False,
            "visual_description": " / ".join(good[p["shot_id"]]["observation"] for p in parts),
            "uncovered_selling_points": missing_points,
            "degraded_no_match": False, "formal_rejected": False,
            "multi_shot": len(assembled) > 1, "span_parts": assembled if len(assembled) > 1 else None}
        overall_review = {**good[first["shot_id"]], "supported_point_ids": sorted(supported),
                          "scarce_reuse_reason": reuse_reason,
                          "time_sensitive": any(good[p["shot_id"]]["time_sensitive"] for p in parts),
                          "observation": segment["visual_description"], "reason": row.get("reason") or good[first["shot_id"]]["reason"]}
        segment["time_sensitive"] = overall_review["time_sensitive"]
        receipt_sentence = {**sentence, "selling_points": [p for p in sentence["selling_points"] if p["id"] in supported]}
        segment["ai_selection"] = contract.make_receipt(segment, index=index, sentence=receipt_sentence,
            observation=overall_review, source_identity=good[first["shot_id"]]["source_identity"], identity=plan["identity"])
        check = contract.validate_segment_receipt(segment, index=index, manifest=manifest)
        if not check["ok"]:
            raise ValueError(";".join(check["reasons"]))
        output.append(segment)
    out_manifest = {**manifest, "selection_mode": "ai_editor", "segments": output,
                    "temporary_material_analysis": True, "temporary_shot_ids": [s["temporary_shot_id"] for s in output]}
    out_manifest["claim_gate_coverage"] = {"mode": "ai_editor", "certification": "UNCERTIFIED" if unresolved else "CERTIFIED",
        "validation_passed": not bool(unresolved), "uncertified_reasons": [f"PARTIAL_SELLING_POINTS:{x['index']}" for x in unresolved],
        "scope": "source_observation_only_independent_qc_required"}
    if unresolved:
        out_manifest["preview_unresolved"] = unresolved
    overlap = contract.sequence_errors(output)
    if overlap:
        raise ValueError(";".join(overlap))
    return {"ok": True, "selection_policy": "ai_editor_selling_points_first", "selection_mode": "ai_editor",
            "segments": output, "manifest": out_manifest, "pending_items": unresolved,
            "accepted_shots": [p.get("shot_id", s["temporary_shot_id"]) for s in output for p in (s.get("span_parts") or [s])],
            "recommendations": [{"index": s["index"], "text": s["text"], "sentence_kind": s["kind"],
                "candidates": plan["reviews"].get(str(s["index"]), []) if s["kind"] == "selling_point" else plan["display_reviews"]} for s in sentences],
            "degraded_matches": [], "degraded_count": 0, "material_gaps": [], "material_gap_count": 0,
            "contract_issues": [], "claim_gate_coverage": out_manifest["claim_gate_coverage"]}


def _build_edit_inventory(manifest: dict, analysis: dict, plan: dict,
                          narration_timing: dict | None) -> list[dict]:
    from . import shot_analyzer
    original = timing_contract.split_sentences(manifest)
    shots = {str(s["shot_id"]): s for s in analysis.get("shots", [])}
    inventory = []
    source_ids = {path: f"source{i}" for i, path in enumerate(dict.fromkeys(
        shot["video"] for shot in shots.values()), 1)}
    for sentence in plan["sentences"]:
        reviews = (plan["reviews"].get(str(sentence["index"]), [])
                   if sentence["kind"] == "selling_point" else plan["display_reviews"])
        duration = timing_contract.duration_for(
            narration_timing, sentence["index"], segment=original[sentence["index"] - 1])[0]
        candidates = [{k: review[k] for k in ("shot_id", "basis", "observation", "reason", "confidence",
            "supported_point_ids", "evidence_intervals_us", "time_sensitive") if k in review}
            for review in reviews if review["verdict"] == "matched"]
        for candidate in candidates:
            review = next(r for r in reviews if r["shot_id"] == candidate["shot_id"])
            shot = shots[candidate["shot_id"]]
            review = {**review, "preferred_speed": _scene_preference(manifest, sentence, review)}
            solution = shot_analyzer.candidate_window(_observed_shot(shot, review),
                audio_duration_us=duration, manifest=manifest,
                time_sensitive=review["time_sensitive"])
            candidate["source_identity"] = source_ids[shot["video"]]
            candidate["technical_option"] = {**solution.to_dict(),
                "within_preferred_speed": solution.ok and solution.speed <= 2.0,
                "original_scene_start_us": round(float(shot["source_start"]) * 1e6),
                "original_scene_end_us": round(float(shot["source_end"]) * 1e6)}
        inventory.append({"index": sentence["index"], "text": sentence["text"],
            "kind": sentence["kind"], "selling_points": sentence["selling_points"],
            "audio_duration_us": duration, "candidates": candidates})
    return inventory


def _edit_plan(manifest: dict, analysis: dict, plan: dict, directory: Path,
               narration_timing: dict | None = None) -> None:
    """Compile current editing instructions from frozen, pixel-reviewed candidates."""
    shots = {str(s["shot_id"]): s for s in analysis.get("shots", [])}
    instructions = """
输出JSON {"rows":[{"index":1,"parts":[{"shot_id":"...","audio_duration_us":2000000,"source_start_us":0}],"reason":"整稿选择理由"}]}。
每句恰好一行，不能选择本句候选以外的镜头。parts的audio_duration_us为正整数且总和必须等于该句audio_duration_us。
多卖点可组合多镜，支持的point_ids并集必须覆盖本句全部卖点。
单镜或多镜根据句意与实际配音节奏选择：当一段口播包含不同卖点或存在多个贴切画面，且每个镜头有足够时间清楚呈现时，优先考虑句内多镜，避免机械地每句一镜；不要为了镜头数量裁掉动作、重复播放或制造闪帧。
同源连续实验顺接后续区间，不每句重播。source_start_us可省略，提供时不得裁掉必需证据。
镜头变化和去重只是质量接近时的偏好，不得抢走唯一卖点候选。没有合格候选返回空parts，不编造支持。
technical_option是按本句完整配音求出的实际可行窗口；分配多镜后程序会重新求解，不可用单镜无解淘汰组合。
匹配质量相近时优先within_preferred_speed=true，不把2倍当硬上限或全片目标；超2倍仍可用于唯一正确卖点并交独立QC。
根据source_identity、源区间及可见动作描述减少近重复。优先中后段，但完整证据和唯一卖点先于取段偏好。
确实只有一个实际支持的候选、卖点不得不复用时，可在该row写scarce_reuse_reason解释；介绍/钩子和有替代候选时不允许此例外，相关实验仍须顺接而非相邻重播。
"""
    if manifest.get("delivery_mode") == "preview" and manifest.get("allow_partial_selling_points") is True:
        instructions = instructions.replace("支持的point_ids并集必须覆盖本句全部卖点。",
            "至少覆盖本句一个经复看的卖点；优先覆盖更多卖点，未覆盖的卖点会明确标为待审。")

    def make_prompt() -> str:
        current_inventory = _build_edit_inventory(manifest, analysis, plan, narration_timing)
        return (contract.policy_prompt(manifest) + "\n你是整稿剪辑师。以下候选均已经实际看图。先保留卖点稀缺候选，再给介绍和钩子分配展示池。\n"
                + json.dumps(current_inventory, ensure_ascii=False) + instructions)

    for attempt in range(3):
        prompt = make_prompt()
        response = _call(prompt, directory / f"edit_{attempt}.json")
        plan["rows"] = response.get("rows")
        if attempt == 0:
            expanded = False
            for row in plan.get("rows") or []:
                if not isinstance(row, dict) or not str(row.get("scarce_reuse_reason") or "").strip():
                    continue
                index = row.get("index")
                sentence = next((s for s in plan["sentences"] if s.get("index") == index), None)
                if not sentence or sentence.get("kind") != "selling_point":
                    continue
                key = str(index)
                coverage = plan.get("coverage", {}).get(key, {})
                if coverage.get("exhausted"):
                    continue
                reviews, coverage = _review_pool(
                    manifest, sentence, shots, directory, narration_timing or {},
                    exhaustive=True, existing_reviews=plan["reviews"].get(key, []))
                plan["reviews"][key] = reviews
                plan.setdefault("coverage", {})[key] = coverage
                expanded = True
            if expanded:
                continue  # second and final editor call sees the complete audited pool
        try:
            _assemble(manifest, analysis, plan, narration_timing=narration_timing, verify_sources=True)
            plan["ok"] = True
            return
        except (ValueError, KeyError, TypeError, OSError) as exc:
            if attempt == 2:
                raise
            instructions += "\n上一版技术检查失败，只修订此问题并重新输出完整rows：" + str(exc) + "。不得把同一句的备选或修正各写成一行，每个index只保留最终决定的一行；多个镜头放在该行parts数组。"


def prepare(manifest: dict, analysis: dict, report_dir: Path, *, narration_timing: dict | None = None) -> dict:
    directory = report_dir / "ai_editor"
    directory.mkdir(parents=True, exist_ok=True)
    plan: dict = {"schema": contract.PLAN_SCHEMA, "policy_version": contract.POLICY_VERSION,
                  "reviews": {}, "display_reviews": [], "coverage": {}, "issues": []}
    try:
        shots = [s for s in analysis.get("shots", []) if isinstance(s, dict) and s.get("status") == "ready_for_matching"]
        if not shots or len({s["shot_id"] for s in shots}) != len(shots):
            raise ValueError("AI_EDITOR_NO_UNIQUE_CANDIDATES")
        by_id = {str(s["shot_id"]): s for s in shots}
        plan["sentences"] = _inventory(manifest, shots, directory)
        editor_model = _editor_model()
        plan["identity"] = {"model": editor_model,
            "manifest_sha256": contract.digest(manifest), "timing_sha256": contract.digest(narration_timing),
            "analysis_sha256": contract.digest({k: v for k, v in analysis.items() if k != "ai_editor_plan"}),
            "policy_version": contract.POLICY_VERSION,
            "editorial_rules": contract.rules_identity(manifest)}
        original = timing_contract.split_sentences(manifest)
        for sentence in plan["sentences"]:
            sentence["audio_durations_us"] = [timing_contract.duration_for(narration_timing,
                sentence["index"], segment=original[sentence["index"] - 1])[0]]
            if sentence["kind"] == "selling_point":
                reviews, coverage = _review_pool(manifest, sentence, by_id, directory, narration_timing or {})
                plan["reviews"][str(sentence["index"])] = reviews
                plan["coverage"][str(sentence["index"])] = coverage
                supported = {p for r in reviews if r["verdict"] == "matched" for p in r.get("supported_point_ids", [])}
                missing = [p for p in sentence["selling_points"] if p["id"] not in supported]
                if missing and coverage["exhausted"] and not coverage["uncertain"]:
                    plan.setdefault("material_gaps", []).append({"index": sentence["index"],
                        "text": sentence["text"], "selling_points": missing, "reason": "ALL_CANDIDATES_REVIEWED_NO_SUPPORT"})
                _save(directory / "plan.json", plan)
        displays = [s for s in plan["sentences"] if s["kind"] != "selling_point"]
        if displays:
            shared = {**displays[0], "index": 0, "text": "同商品通用产品展示池", "kind": "product_intro", "selling_points": []}
            shared["audio_durations_us"] = [timing_contract.duration_for(narration_timing, s["index"],
                segment=original[s["index"] - 1])[0] for s in displays]
            plan["display_reviews"], plan["coverage"]["display_pool"] = _review_pool(manifest, shared, by_id, directory, narration_timing or {})
        inventory = [{"index": s["index"], "text": s["text"], "kind": s["kind"],
            "selling_points": s["selling_points"], "audio_duration_us": timing_contract.duration_for(narration_timing, s["index"], segment=timing_contract.split_sentences(manifest)[s["index"] - 1])[0],
            "candidates": plan["reviews"].get(str(s["index"]), []) if s["kind"] == "selling_point" else plan["display_reviews"]} for s in plan["sentences"]]
        # Do not pass paths or old gate/score fields to the editor.
        for row in inventory:
            row["candidates"] = [{k: r[k] for k in ("shot_id", "basis", "observation", "reason", "confidence", "supported_point_ids", "evidence_intervals_us", "time_sensitive") if k in r}
                                 for r in row["candidates"] if r["verdict"] == "matched"]
            coverage = plan["coverage"].get(str(row["index"]) if row["kind"] == "selling_point" else "display_pool", {})
            if not coverage.get("feasible"):
                raise ValueError(f"AI_EDITOR_OBSERVATION_OR_WINDOW_PENDING:{row['index']}:{coverage.get('reason', 'UNKNOWN')}")
        _edit_plan(manifest, analysis, plan, directory, narration_timing)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        plan["ok"] = False
        plan["issues"].append({"type": "AI_EDITOR_UNCERTIFIED", "message": str(exc)})
    _save(directory / "plan.json", plan)
    return {**analysis, "ai_editor_plan": plan}


def match_manifest(manifest: dict, analysis: dict, *, narration_timing: dict | None = None) -> dict:
    plan = analysis.get("ai_editor_plan") or {}
    try:
        if plan.get("schema") != contract.PLAN_SCHEMA or not plan.get("ok"):
            raise ValueError("AI_EDITOR_PLAN_NOT_CERTIFIED")
        identity = plan.get("identity") or {}
        if plan.get("policy_version", identity.get("policy_version")) != contract.POLICY_VERSION:
            raise ValueError("AI_EDITOR_POLICY_CHANGED")
        if identity.get("manifest_sha256") != contract.digest(manifest) or identity.get("timing_sha256") != contract.digest(narration_timing):
            raise ValueError("AI_EDITOR_INPUT_CHANGED")
        if identity.get("analysis_sha256") != contract.digest({k: v for k, v in analysis.items() if k != "ai_editor_plan"}):
            raise ValueError("AI_EDITOR_ANALYSIS_CHANGED")
        return _assemble(manifest, analysis, plan, narration_timing=narration_timing)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {"ok": False, "selection_mode": "ai_editor", "segments": [], "manifest": dict(manifest),
                "pending_items": plan.get("issues") or [{"type": "AI_EDITOR_UNCERTIFIED", "message": str(exc)}],
                "recommendations": [], "degraded_matches": [], "material_gaps": plan.get("material_gaps", []),
                "material_gap_count": len(plan.get("material_gaps", []))}


def _heuristic_sentence(index: int, text: str) -> dict:
    """Keep explicit clauses as separate possible points without inventing claims."""
    value = str(text or "").strip()
    parts = [part.strip() for part in re.split(r"[，,；;]|以及|还有|并且|同时|和", value) if part.strip()]
    if len(parts) < 2 or any(len(part) < 2 for part in parts) or len(parts) > 6:
        parts = [value]
    return {
        "index": index,
        "text": value,
        "kind": "selling_point",
        "selling_points": [{"id": f"p{i}", "text": part} for i, part in enumerate(parts, 1)],
        "candidate_ids": [], "scene_ids": [],
    }


def _rule_shortlist_ids(matched: dict, analysis: dict, index: int, *, limit: int = 8,
                        min_duration_s: float = 0.0) -> list[str]:
    """从规则匹配结果取该句短名单；不够时用更长的 ready 镜头补齐。

    When ``min_duration_s`` is set, long-enough ready shots are placed first so
    a full shortlist of short rule candidates cannot crowd out feasible windows.
    """
    preferred: list[str] = []
    for rec in matched.get("recommendations") or []:
        if not isinstance(rec, dict):
            continue
        try:
            if int(rec.get("index")) != index:
                continue
        except (TypeError, ValueError):
            continue
        for row in rec.get("candidates") or rec.get("top3") or []:
            if isinstance(row, dict) and str(row.get("shot_id") or "").strip():
                preferred.append(str(row["shot_id"]).strip())
    for seg in matched.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        try:
            if int(seg.get("index") or 0) != index and int(seg.get("sentence_index") or 0) != index:
                continue
        except (TypeError, ValueError):
            continue
        sid = str(seg.get("shot_id") or seg.get("temporary_shot_id") or "").strip()
        if sid:
            preferred.append(sid)
        for part in seg.get("span_parts") or []:
            if isinstance(part, dict) and str(part.get("shot_id") or "").strip():
                preferred.append(str(part["shot_id"]).strip())
    # Timing gaps are usually SOURCE_TOO_SHORT: prefer longer ready shots when
    # padding so AI refill is not stuck reviewing the same short windows.
    ready_rows = [
        s for s in analysis.get("shots") or []
        if isinstance(s, dict) and s.get("status") == "ready_for_matching" and s.get("shot_id")
    ]
    by_id = {
        str(s["shot_id"]).strip(): s for s in ready_rows if str(s.get("shot_id") or "").strip()
    }

    def _dur(row: dict) -> float:
        try:
            return float(row.get("duration") or row.get("duration_s") or 0)
        except (TypeError, ValueError):
            return 0.0

    floor = max(0.0, float(min_duration_s or 0))
    ready_rows.sort(key=_dur, reverse=True)
    long_enough = [s for s in ready_rows if _dur(s) >= floor]

    def _long_enough_id(sid: str) -> bool:
        row = by_id.get(sid)
        return bool(row) and _dur(row) >= floor

    ids: list[str] = []
    # 1) rule/segment picks that already clear the duration floor
    if floor > 0:
        for sid in preferred:
            if sid and _long_enough_id(sid) and sid not in ids:
                ids.append(sid)
            if len(ids) >= limit:
                return ids
    else:
        for sid in preferred:
            if sid and sid not in ids:
                ids.append(sid)
            if len(ids) >= limit:
                return ids
    # 2) other long ready shots (longest first)
    for row in long_enough:
        sid = str(row["shot_id"]).strip()
        if sid and sid not in ids:
            ids.append(sid)
        if len(ids) >= limit:
            return ids
    # 3) remaining preferred (may be short — last resort for review coverage)
    for sid in preferred:
        if sid and sid not in ids:
            ids.append(sid)
        if len(ids) >= limit:
            return ids
    for row in ready_rows:
        sid = str(row["shot_id"]).strip()
        if sid and sid not in ids:
            ids.append(sid)
        if len(ids) >= limit:
            break
    return ids


def _reindex_ai_segment(segment: dict, index: int) -> dict:
    """Keep the current observation receipt when a mini-manifest becomes S<n>."""
    result = copy.deepcopy(segment)
    result["index"] = index
    for item in [result, *(result.get("span_parts") or [])]:
        if isinstance(item.get("ai_selection"), dict):
            item["ai_selection"]["index"] = index
    return result


def refill_indices(manifest: dict, analysis: dict, matched: dict, indices: list[int],
                   report_dir: Path, *, narration_timing: dict | None = None) -> dict:
    """Recover only failed sentences, preserving the other selected windows.

    Retrieval may use rule candidates, while the existing pixel review and
    window contract make the decision. Repeated recovery reuses observations.
    """
    wanted = sorted({int(i) for i in indices if int(i) >= 1})
    if not wanted:
        return matched
    original = timing_contract.split_sentences(manifest)
    shots = {str(s["shot_id"]): s for s in analysis.get("shots", [])
             if isinstance(s, dict) and s.get("status") == "ready_for_matching"
             and s.get("shot_id") and s.get("text_pass") not in ("pending", "failed")}
    directory = report_dir / "ai_editor_refill"
    directory.mkdir(parents=True, exist_ok=True)
    script_indices = {"".join(timing_contract.sentence_text(s).split()): i
                      for i, s in enumerate(original, 1)}
    by_index = {}
    for enum_i, segment in enumerate(matched.get("segments", []), 1):
        row = copy.deepcopy(segment)
        index = script_indices.get("".join(str(row.get("text") or row.get("claim") or "").split()))
        index = index or int(row.get("index") or enum_i)
        row["index"] = index
        by_index[index] = row
    reserved = {str(part.get("shot_id") or part.get("temporary_shot_id"))
                for index, row in by_index.items() if index not in wanted
                for part in (row.get("span_parts") or [row])}
    replaced, errors, refill_unresolved = [], [], []
    for index in wanted:
        if index > len(original):
            errors.append(f"AI_REFILL_INDEX_OOB:{index}")
            continue
        sentence = _heuristic_sentence(index, timing_contract.sentence_text(original[index - 1]))
        preferred = _rule_shortlist_ids(matched, analysis, index, limit=max(ROUND_SIZE, 16))
        sentence["candidate_ids"] = [sid for sid in dict.fromkeys([*preferred, *shots])
                                     if sid in shots and sid not in reserved]
        audio_us, certainty = timing_contract.duration_for(narration_timing, index,
                                                          segment=original[index - 1])
        sentence["audio_durations_us"] = [audio_us]
        try:
            if not sentence["candidate_ids"]:
                raise ValueError(f"AI_REFILL_NO_UNIQUE_CANDIDATES:{index}")
            reviews, coverage = _review_pool(manifest, sentence, shots, directory, narration_timing or {})
            good = [r for r in reviews if r.get("verdict") == "matched"]
            if not good or not coverage.get("feasible"):
                raise ValueError(f"AI_REFILL_NO_MATCH:{index}")
            mini_manifest = {k: v for k, v in manifest.items() if k not in ("segments", "full_script", "script")}
            mini_manifest.update(full_script=sentence["text"], selection_mode="ai_editor")
            timing_row = {"key": "s1", "text": sentence["text"], "audio_duration_us": audio_us,
                          "timing_certainty": certainty}
            if narration_timing and index <= len(narration_timing.get("rows", [])):
                timing_row = {**narration_timing["rows"][index - 1], **timing_row}
            mini_timing = {**(narration_timing or {}), "rows": [timing_row]}
            last_error = f"AI_REFILL_WINDOW_INFEASIBLE:{index}"
            for best in sorted(good, key=lambda r: float(r.get("confidence", 0)), reverse=True):
                plan = {"schema": contract.PLAN_SCHEMA, "policy_version": contract.POLICY_VERSION,
                        "sentences": [{**sentence, "index": 1}], "reviews": {"1": reviews},
                        "display_reviews": [], "rows": [{"index": 1, "parts": [{
                            "shot_id": best["shot_id"], "audio_duration_us": int(audio_us)}],
                            "reason": f"rule_then_ai refill for S{index:02d}"}],
                        "identity": {"model": _editor_model(),
                                     "editorial_rules": contract.rules_identity(manifest)}, "ok": True}
                try:
                    assembled = _assemble(mini_manifest, analysis, plan, narration_timing=mini_timing,
                                          verify_sources=True)
                    row = _reindex_ai_segment(assembled["segments"][0], index)
                    checked = contract.validate_segment_receipt(row, index=index, verify_source=True,
                                                                 manifest=manifest)
                    if not checked["ok"]:
                        raise ValueError(";".join(checked["reasons"]))
                    by_index[index] = row
                    reserved.add(str(best["shot_id"]))
                    replaced.append(index)
                    if row.get("uncovered_selling_points"):
                        refill_unresolved.append({"index": index, "text": sentence["text"],
                            "type": "PARTIAL_SELLING_POINTS",
                            "missing_selling_points": row["uncovered_selling_points"]})
                    _save(directory / f"refill_{index:02d}.json", {
                        "index": index, "shot_id": best["shot_id"], "coverage": coverage})
                    break
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    last_error = str(exc)
            else:
                raise ValueError(last_error)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append(str(exc))
    ordered = [by_index[i] for i in sorted(by_index)]
    def keep(row):
        return not isinstance(row, dict) or row.get("index") not in replaced
    result = {**matched, "selection_mode": "rule_then_ai", "selection_policy": "rule_then_ai_refill",
              "segments": ordered, "refill_indices": wanted, "refill_replaced": replaced,
              "refill_errors": errors, "manifest": {**(matched.get("manifest") or manifest),
                  "selection_mode": "rule_then_ai", "segments": ordered}}
    for field in ("pending_items", "material_gaps", "degraded_matches", "visual_missing_matches", "preview_unresolved"):
        result[field] = [row for row in matched.get(field, []) if keep(row)]
    result["preview_unresolved"].extend(refill_unresolved)
    result["pending_items"].extend({"type": "AI_EDITOR_UNCERTIFIED", "message": error} for error in errors)
    result["ok"] = (bool(ordered) and len(ordered) == len(original) and not result["pending_items"]
                    and set(wanted) <= set(replaced)
                    and not contract.sequence_errors([s for s in ordered if contract.is_ai_segment(s)]))
    for counter, field in (("material_gap_count", "material_gaps"), ("degraded_count", "degraded_matches"),
                           ("visual_missing_count", "visual_missing_matches"), ("preview_unresolved_count", "preview_unresolved")):
        result[counter] = len(result[field])
    result["degraded_fallback_count"] = len(result["degraded_matches"])
    if result["ok"]:
        outstanding = [f"{field}:{row.get('index', '?')}"
                       for field in ("degraded_matches", "material_gaps", "visual_missing_matches", "preview_unresolved")
                       for row in result.get(field, []) if isinstance(row, dict)]
        original_coverage = matched.get("claim_gate_coverage") or {}
        outstanding.extend(str(reason) for reason in original_coverage.get("uncertified_reasons", []) if reason)
        coverage = {"mode": "rule_then_ai",
            "certification": "UNCERTIFIED" if outstanding else "MIXED_RULE_AND_AI_REFILL",
            "validation_passed": not bool(outstanding), "uncertified_reasons": outstanding,
            "scope": "independent_qc_required"}
        result["claim_gate_coverage"] = coverage
        result["manifest"]["claim_gate_coverage"] = coverage
        result["manifest"]["preview_unresolved"] = result["preview_unresolved"]
        result["manifest"]["visual_missing_matches"] = result["visual_missing_matches"]
        result["accepted_shots"] = [p.get("shot_id") or p.get("temporary_shot_id")
            for row in ordered for p in (row.get("span_parts") or [row])]
    return result
