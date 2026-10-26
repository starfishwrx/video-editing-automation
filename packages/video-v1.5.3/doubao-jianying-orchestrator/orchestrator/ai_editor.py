"""Pixel-grounded candidate review followed by selling-point-first editing.

Legacy semantic scores only retrieve candidates. No legacy claim gate is used
here, and failure never falls back to a keyword-selected packaging shot.
"""
from __future__ import annotations

import copy
import json
import math
import os
from itertools import combinations
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import ai_editor_contract as contract, timing_contract

ROUND_SIZE = 8


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _call(prompt: str, path: Path, sheet: Path | None = None) -> dict:
    from . import vision_analyzer as vision
    if vision._vision_transport() != "volc_glm":
        raise ValueError("AI_EDITOR_REQUIRES_DIRECT_GLM")
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = {"prompt_sha256": contract.digest(prompt),
                "image_sha256": contract.file_sha256(sheet) if sheet else None,
                "model": vision._volc_settings()["model"], "endpoint": vision._volc_settings()["base_url"]}
    request_path = path.with_suffix(".request.json")
    if path.is_file() and request_path.is_file():
        try:
            if json.loads(request_path.read_text(encoding="utf-8")) == identity:
                cached = vision._json_from_text(path.read_text(encoding="utf-8"))
                if isinstance(cached, dict):
                    json.dumps(cached, allow_nan=False)
                    return cached
        except (OSError, ValueError, TypeError):
            pass
    (path.with_suffix(".prompt.txt")).write_text(prompt, encoding="utf-8")
    for attempt in range(2):
        with vision._vision_slot() as handle:
            if not handle.acquired:
                raise ValueError("AI_EDITOR_VISION_SLOT_TIMEOUT")
            ok, text = vision._run_volc_chat(sheet, prompt, path,
                timeout=int(os.environ.get("JY_AI_EDITOR_TIMEOUT_S", os.environ.get("JY_VISION_TIMEOUT", "180"))))
        if ok:
            payload = vision._json_from_text(text)
            if not isinstance(payload, dict):
                raise ValueError("AI_EDITOR_RESPONSE_NOT_OBJECT")
            json.dumps(payload, allow_nan=False)  # Reject non-finite model output.
            path.write_text(text, encoding="utf-8")
            _save(request_path, identity)
            return payload
        if attempt or not vision._is_transient(text):
            raise ValueError("AI_EDITOR_CALL_FAILED:" + str(text)[-500:])
    raise ValueError("AI_EDITOR_CALL_FAILED")


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
        if not isinstance(ids, list) or any(not isinstance(s, str) or s not in known for s in ids):
            raise ValueError("AI_EDITOR_CANDIDATE_UNKNOWN")
        by_index[index] = {**row, "text": timing_contract.sentence_text(rows[index - 1]),
                           "candidate_ids": list(dict.fromkeys(ids + [str(s["shot_id"]) for s in shots]))}
    display = data.get("display_pool_ids")
    if not isinstance(display, list) or any(s not in known for s in display):
        raise ValueError("AI_EDITOR_DISPLAY_POOL_INVALID")
    result = [by_index[i] for i in range(1, len(rows) + 1)]
    for row in result:
        if row["kind"] != "selling_point":
            row["candidate_ids"] = list(dict.fromkeys(display + [str(s["shot_id"]) for s in shots]))
    return result


def _review(manifest: dict, sentence: dict, shot: dict, directory: Path, *, dense: bool = False) -> dict:
    from . import vision_analyzer as vision, platform_env, shot_analyzer
    start, end = float(shot["source_start"]), float(shot["source_end"])
    if not math.isfinite(start + end) or not 0 <= start < end:
        raise ValueError("AI_EDITOR_SOURCE_WINDOW_INVALID")
    source = str(shot["video"])
    before = contract.source_identity(source)
    key = contract.digest([sentence["index"], shot["shot_id"], before, dense])[:24]
    frame_dir = directory / "frames" / key
    frame_dir.mkdir(parents=True, exist_ok=True)
    count = 16 if dense else 8
    last = end - min(.04, (end - start) / 10)
    times = [start + (last - start) * n / (count - 1) for n in range(count)]
    ffmpeg, _ = platform_env.find_ffmpeg()
    if not ffmpeg:
        raise ValueError("AI_EDITOR_FFMPEG_MISSING")
    frames = []
    for i, timestamp in enumerate(times):
        target = frame_dir / f"{i:02d}.jpg"
        shot_analyzer.extract_frame(source, timestamp, target, ffmpeg=ffmpeg)
        if not target.is_file() or target.stat().st_size == 0:
            raise ValueError("AI_EDITOR_FRAME_EXTRACTION_FAILED")
        frames.append({"path": str(target), "label": f"{shot['shot_id']} t={timestamp:.3f}s"})
    sheet = frame_dir / "sheet.jpg"
    if not vision._write_sheet(frames, sheet, columns=4, tile_width=420, tile_height=300, ffmpeg=ffmpeg):
        raise ValueError("AI_EDITOR_CONTACT_SHEET_FAILED")
    if before != contract.source_identity(source):
        raise ValueError("AI_EDITOR_SOURCE_CHANGED_DURING_REVIEW")
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
    if result["verdict"] == "uncertain" and not dense:
        return _review(manifest, sentence, shot, directory, dense=True)
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
        if not isinstance(intervals, list) or not intervals:
            raise ValueError("AI_EDITOR_EVIDENCE_INVALID")
        for ev in intervals:
            if (not isinstance(ev, dict) or any(type(ev.get(k)) is not int for k in ("start", "end"))
                    or not round(start * 1e6) <= ev["start"] < ev["end"] <= round(end * 1e6)):
                raise ValueError("AI_EDITOR_EVIDENCE_INVALID")
    return {**result, "shot_id": shot["shot_id"], "source_identity": before,
            "frame_times_us": [round(t * 1e6) for t in times], "sheet": str(sheet)}


def _review_pool(manifest: dict, sentence: dict, shots: dict, directory: Path, audio: dict, *,
                 exhaustive: bool = False,
                 existing_reviews: list[dict] | None = None) -> tuple[list[dict], dict]:
    from . import shot_analyzer
    def feasible(good: list[dict]) -> bool:
        durations = sentence.get("audio_durations_us") or [timing_contract.duration_for(
            audio, sentence["index"], segment={"text": sentence["text"]})[0]]
        for duration in durations:
            available = False
            for size in range(1, min(len(good), int(manifest.get("span_max_parts", 3))) + 1):
                for group in combinations(good, size):
                    if not required <= {p for r in group for p in r.get("supported_point_ids", [])}:
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
        reviewed = [reviewed_by_id[sid] for sid in ids if sid in reviewed_by_id]
        good = [r for r in reviewed if r["verdict"] == "matched"]
        covered = {p for r in good for p in r.get("supported_point_ids", [])}
        if not exhaustive and required and required <= covered and feasible(good):
            break
        if not exhaustive and not required and len(good) >= min(3, len(ids)) and feasible(good):
            break
    reviewed = [reviewed_by_id[sid] for sid in ids if sid in reviewed_by_id]
    return reviewed, {"checked": len(reviewed), "total": len(ids), "exhausted": len(reviewed) == len(ids),
                      "feasible": feasible([r for r in reviewed if r["verdict"] == "matched"]),
                      "failed_calls": sum("error" in r for r in reviewed),
                      "uncertain": sum(r["verdict"] == "uncertain" for r in reviewed)}


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
        raise ValueError("AI_EDITOR_PLAN_COVERAGE")
    indexed = {}
    for row in rows:
        i = row.get("index") if isinstance(row, dict) else None
        if type(i) is not int or not 1 <= i <= len(sentences) or i in indexed:
            raise ValueError("AI_EDITOR_PLAN_INDEX_INVALID")
        indexed[i] = row
    output = []
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
        if not {p["id"] for p in sentence["selling_points"]} <= supported:
            raise ValueError(f"AI_EDITOR_POINTS_UNCOVERED:{index}")
        first = assembled[0]
        segment = {**original[index - 1], **first, "selection_mode": "ai_editor",
            "claim_text": sentence["text"], "sentence_kind": sentence["kind"],
            "selling_points": sentence["selling_points"], "audio_duration_us": audio_us,
            "timing_certainty": certainty, "window_solution": {k: v for k, v in first.items() if k in {"ok", "speed", "source_start", "source_end", "segment_s", "material_s", "handle_room_s", "reason", "detail"}},
            "action_complete": True, "has_subject": True, "persist_to_library": False,
            "visual_description": " / ".join(good[p["shot_id"]]["observation"] for p in parts),
            "degraded_no_match": False, "formal_rejected": False,
            "multi_shot": len(assembled) > 1, "span_parts": assembled if len(assembled) > 1 else None}
        overall_review = {**good[first["shot_id"]], "supported_point_ids": sorted(supported),
                          "scarce_reuse_reason": reuse_reason,
                          "time_sensitive": any(good[p["shot_id"]]["time_sensitive"] for p in parts),
                          "observation": segment["visual_description"], "reason": row.get("reason") or good[first["shot_id"]]["reason"]}
        segment["time_sensitive"] = overall_review["time_sensitive"]
        segment["ai_selection"] = contract.make_receipt(segment, index=index, sentence=sentence,
            observation=overall_review, source_identity=good[first["shot_id"]]["source_identity"], identity=plan["identity"])
        check = contract.validate_segment_receipt(segment, index=index, manifest=manifest)
        if not check["ok"]:
            raise ValueError(";".join(check["reasons"]))
        output.append(segment)
    out_manifest = {**manifest, "selection_mode": "ai_editor", "segments": output,
                    "temporary_material_analysis": True, "temporary_shot_ids": [s["temporary_shot_id"] for s in output]}
    out_manifest["claim_gate_coverage"] = {"mode": "ai_editor", "certification": "CERTIFIED",
        "validation_passed": True, "uncertified_reasons": [], "scope": "source_observation_only_independent_qc_required"}
    overlap = contract.sequence_errors(output)
    if overlap:
        raise ValueError(";".join(overlap))
    return {"ok": True, "selection_policy": "ai_editor_selling_points_first", "selection_mode": "ai_editor",
            "segments": output, "manifest": out_manifest, "pending_items": [],
            "accepted_shots": [p.get("shot_id", s["temporary_shot_id"]) for s in output for p in (s.get("span_parts") or [s])],
            "recommendations": [{"index": s["index"], "text": s["text"], "sentence_kind": s["kind"],
                "candidates": plan["reviews"].get(str(s["index"]), []) if s["kind"] == "selling_point" else plan["display_reviews"]} for s in sentences],
            "degraded_matches": [], "degraded_count": 0, "material_gaps": [], "material_gap_count": 0,
            "contract_issues": [], "claim_gate_coverage": {"mode": "ai_editor",
                "certification": "CERTIFIED", "validation_passed": True, "uncertified_reasons": [],
                "scope": "source_observation_only_independent_qc_required"}}


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
多卖点可组合多镜，支持的point_ids并集必须覆盖本句全部卖点；单镜足够时优先单镜。
同源连续实验顺接后续区间，不每句重播。source_start_us可省略，提供时不得裁掉必需证据。
镜头变化和去重只是质量接近时的偏好，不得抢走唯一卖点候选。没有合格候选返回空parts，不编造支持。
technical_option是按本句完整配音求出的实际可行窗口；分配多镜后程序会重新求解，不可用单镜无解淘汰组合。
匹配质量相近时优先within_preferred_speed=true，不把2倍当硬上限或全片目标；超2倍仍可用于唯一正确卖点并交独立QC。
根据source_identity、源区间及可见动作描述减少近重复。优先中后段，但完整证据和唯一卖点先于取段偏好。
确实只有一个实际支持的候选、卖点不得不复用时，可在该row写scarce_reuse_reason解释；介绍/钩子和有替代候选时不允许此例外，相关实验仍须顺接而非相邻重播。
"""

    def make_prompt() -> str:
        current_inventory = _build_edit_inventory(manifest, analysis, plan, narration_timing)
        return (contract.policy_prompt(manifest) + "\n你是整稿剪辑师。以下候选均已经实际看图。先保留卖点稀缺候选，再给介绍和钩子分配展示池。\n"
                + json.dumps(current_inventory, ensure_ascii=False) + instructions)

    for attempt in range(2):
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
            if attempt:
                raise
            instructions += "\n上一版技术检查失败，只修订此问题并重新输出完整rows：" + str(exc)


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
        plan["identity"] = {"model": os.environ.get("JY_VISION_MODEL") or "glm-5.3-flash",
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
                raise ValueError(f"AI_EDITOR_OBSERVATION_OR_WINDOW_PENDING:{row['index']}")
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
