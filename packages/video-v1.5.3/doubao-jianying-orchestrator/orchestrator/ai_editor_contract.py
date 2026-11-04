"""Versioned, window-bound observations for the AI editor.

This contract validates identity and coverage, never Chinese keyword overlap.
It is an observation receipt, not an independent-QC or delivery permission.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
from typing import Any

PLAN_SCHEMA = "chatcut-ai-selection-v1"
RECEIPT_SCHEMA = "chatcut-ai-selection-evidence-v1"
POLICY_VERSION = "selling-points-first-reference-quality-v3"
POINT_COVERAGE_POLICY = "at_least_one_specific_point"
KINDS = {"selling_point", "product_intro", "hook"}
BASES = {"editorial_fit", "visible_result", "readable_label", "product_display"}
if __package__:
    from .policy import playback, continuity
else:
    _playback_spec = importlib.util.spec_from_file_location(
        "_chatcut_playback", Path(__file__).parent / "policy" / "playback.py")
    playback = importlib.util.module_from_spec(_playback_spec)
    _playback_spec.loader.exec_module(playback)
    _continuity_spec = importlib.util.spec_from_file_location(
        "_chatcut_continuity", Path(__file__).parent / "policy" / "continuity.py")
    continuity = importlib.util.module_from_spec(_continuity_spec)
    _continuity_spec.loader.exec_module(continuity)
AI_SPEED_MAX = playback.PROCESS_MAX
POLICY = """先通读整稿，先安排具体卖点，再安排产品介绍和无具体卖点的钩子/衔接。
句子按实际内容分类，开场包含具体卖点仍是selling_point。一句多个卖点至少实际呈现一个具体点，未覆盖项如实记录，不能声称全部已证实。
轻薄、柔软、舒服允许能体现特点的真实展示，不强制实验或固定厚度对照。
明确性能结果、数值或比较必须有对应可见画面，包装文字只支持该标称。
产品介绍和无具体卖点的钩子可用同商品包装、手持、拆包、取出和本体展示，属于正常匹配。
具体卖点不能用普通包装展示替代。看不到、只有口播、对象错误和不确定不得假装支持。
相关实验按前后语义连续播放，不反复从头播放；匹配先于去重与机位变化。
画面贴切优先，完整保留必要动作和结果；变速必须遵守下述生产播放规则。
优先原素材时间轴的中后段、尽量避开原素材前三秒；这是偏好，不是每个切分镜头再减三秒，短素材和开头唯一正确证据仍可用。
奶被纸、棉柔巾、厨房纸、湿厕纸按口播匹配卖点及同商品展示；根据实际可见动作、机位和源区间减少近重复，不按文件名判断。
素材描述仅供召回，最终判断必须依据本次提供的真实画面。不得修改口播或虚构素材。"""


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def points_supported(required: set, supported: set) -> bool:
    return not required or bool(required & supported)


def file_sha256(path: str | Path) -> str:
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def source_identity(path: str | Path) -> dict:
    """Cheap stale-media detection for local editing, not a content signature."""
    source = Path(path).resolve(strict=True)
    info = source.stat()
    return {"path": str(source), "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def editorial_context(manifest: dict | None = None) -> dict:
    # production_eval loads this contract as a standalone file. Load the sibling
    # rulepack from the same runtime, never an unrelated installed package.
    if __package__:
        from . import rulepack
    else:
        spec = importlib.util.spec_from_file_location("_chatcut_editorial_rules", Path(__file__).with_name("rulepack.py"))
        rulepack = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rulepack)
    return rulepack.editorial_context(manifest)


def rules_identity(manifest: dict | None = None) -> dict:
    context = editorial_context(manifest)
    return {key: context[key] for key in ("category", "versions")}


def policy_prompt(manifest: dict) -> str:
    policy = POLICY + "\n" + playback.PROMPT
    if editorial_context(manifest)["category"] in {"zhipin", "weipin"}:
        policy += "\n本任务生效的品类制作与选镜合同（按口播含义适用，不能据此虚构画面）：\n" + json.dumps(
            editorial_context(manifest), ensure_ascii=False, sort_keys=True)
    feedback = str(manifest.get("revision_feedback") or "").strip()
    if feedback:
        policy += ("\n以下是旧稿的历史反馈，时间码属于旧稿；把它作为修订线索，"
                   "逐项对照当前文案和真实画面，不把反馈本身当画面证据：\n"
                   + json.dumps(feedback, ensure_ascii=False))
    return policy


def selection_mode(manifest: dict | None = None) -> str:
    value = str((manifest or {}).get("selection_mode") or
                os.environ.get("JY_SELECTION_MODE") or "ai_editor").strip().lower()
    if value not in {"ai_editor", "rule_then_ai", "legacy"}:
        raise ValueError("INVALID_SELECTION_MODE")
    return value


def is_ai_segment(item: dict) -> bool:
    return item.get("selection_mode") == "ai_editor" or "ai_selection" in item


def sequence_conflict_details(segments: list[dict]) -> list[tuple]:
    """Identify exact conflicting parts/windows so a repair cannot swap one conflict for another."""
    errors: list[tuple] = []
    used: dict[str, list[tuple[int, int, int, int, bool]]] = {}
    for index, segment in enumerate(segments, 1):
        for part_index, part in enumerate(segment.get("span_parts") or [segment], 1):
            receipt = part.get("ai_selection") or {}
            source = str(receipt.get("video") or part.get("video") or "")
            window = receipt.get("window_us") or {}
            try:
                start, end = int(window["start"]), int(window["end"])
            except (KeyError, TypeError, ValueError):
                return [("AI_EDITOR_SEQUENCE_RECEIPT_INVALID",)]
            sensitive = receipt.get("time_sensitive") is True
            for previous, previous_part, a, b, was_sensitive in used.get(source, []):
                scarce_reuse = (receipt.get("sentence_kind") == "selling_point"
                                and bool(receipt.get("scarce_reuse_reason"))
                                and not (sensitive and was_sensitive and index <= previous + 1))
                detail = (source, previous_part, part_index, a, b, start, end)
                if min(end, b) - max(start, a) > 40000 and not scarce_reuse:
                    errors.append((f"AI_EDITOR_REPEATED_SOURCE_WINDOW:{previous}:{index}", *detail))
                if sensitive and was_sensitive and index <= previous + 1 and start < a - 40000:
                    errors.append((f"AI_EDITOR_EXPERIMENT_TIME_REVERSED:{previous}:{index}", *detail))
            used.setdefault(source, []).append((index, part_index, start, end, sensitive))
    return errors


def sequence_errors_all(segments: list[dict]) -> list[str]:
    return continuity.sequence_errors(segments) + [detail[0] for detail in sequence_conflict_details(segments)]


def sequence_errors(segments: list[dict]) -> list[str]:
    """Preserve the public first-error contract."""
    return sequence_errors_all(segments)[:1]


def _window(item: dict) -> dict[str, int]:
    solution = item.get("window_solution") or {}
    if solution:
        return {"start": int(solution.get("source_start_us", round(float(solution.get("source_start", 0)) * 1_000_000))),
                "end": int(solution.get("source_end_us", round(float(solution.get("source_end", 0)) * 1_000_000)))}
    start = round(float(item.get("source_start", item.get("in", 0))) * 1_000_000)
    end = round(float(item.get("source_end", float(item.get("source_start", 0)) +
                               float(item.get("duration", 0)))) * 1_000_000)
    return {"start": start, "end": end}


def make_receipt(segment: dict, *, index: int, sentence: dict, observation: dict,
                 source_identity: dict, identity: dict) -> dict:
    """Record the actual observation and solved cut without a self-signature."""
    receipt = {
        "schema": RECEIPT_SCHEMA, "policy_version": POLICY_VERSION,
        "index": index, "text": str(segment.get("text") or ""),
        "sentence_kind": sentence["kind"], "selling_points": sentence.get("selling_points", []),
        "basis": observation["basis"], "observation": observation["observation"],
        "reason": observation["reason"], "confidence": observation["confidence"],
        "time_sensitive": observation["time_sensitive"],
        "supported_point_ids": observation.get("supported_point_ids", []),
        "scarce_reuse_reason": observation.get("scarce_reuse_reason", ""),
        "video": str(segment["video"]), "shot_id": segment["temporary_shot_id"],
        "source_identity": source_identity, "window_us": _window(segment),
        "playback_policy_version": playback.VERSION,
        "playback_observation": observation.get("playback_observation"),
        "continuity_group": segment.get("continuity_group"),
        "action_completeness_scope": segment.get("action_completeness_scope"),
        "video_speed": segment.get("video_speed"),
        "speed_range_override": segment.get("speed_range_override"),
        "preferred_speed": segment.get("preferred_speed", 1.0),
        "scene_ids": observation.get("scene_ids", []), "sentence_scene_ids": sentence.get("scene_ids", []),
        "evidence_intervals_us": observation["evidence_intervals_us"],
        "identity": identity, "judgment": "matched",
    }
    return receipt


def validate_segment_receipt(item: dict, *, text: str | None = None,
                             index: int | None = None, verify_source: bool = False,
                             manifest: dict | None = None) -> dict:
    reasons: list[str] = []
    receipt = item.get("ai_selection")
    if not isinstance(receipt, dict):
        return {"ok": False, "reasons": ["AI_SELECTION_RECEIPT_MISSING"]}
    try:
        reasons.extend(continuity.member_errors(item, index if index is not None else receipt.get("index")))
        if item.get("continuity_group") and manifest is not None:
            context = item["continuity_group"]["input"]
            if (context.get("product") != manifest.get("product") or context.get("category") != manifest.get("category")
                    or context.get("rules") != rules_identity(manifest)):
                reasons.append("CONTINUITY_INPUT_CHANGED")
        if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("policy_version") != POLICY_VERSION:
            reasons.append("AI_SELECTION_CONTRACT_MISMATCH")
        if receipt.get("judgment") != "matched" or receipt.get("sentence_kind") not in KINDS:
            reasons.append("AI_SELECTION_NOT_MATCHED")
        if receipt.get("basis") not in BASES:
            reasons.append("AI_SELECTION_BASIS_INVALID")
        if receipt.get("text") != str(text if text is not None else item.get("text") or ""):
            reasons.append("AI_SELECTION_TEXT_CHANGED")
        if index is not None and receipt.get("index") != index:
            reasons.append("AI_SELECTION_INDEX_CHANGED")
        if receipt.get("video") != str(item.get("video") or "") or receipt.get("shot_id") != item.get("temporary_shot_id"):
            reasons.append("AI_SELECTION_SOURCE_CHANGED")
        if receipt.get("window_us") != _window(item):
            reasons.append("AI_SELECTION_WINDOW_CHANGED")
        speed = item.get("video_speed")
        bounds = item.get("speed_range_override")
        if (type(speed) not in (int, float) or not math.isfinite(speed)
                or not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds)
                or not 0 < bounds[0] <= speed <= bounds[1]):
            reasons.append("AI_SELECTION_SPEED_INVALID")
        window = _window(item)
        target_seconds = (item.get("window_solution") or {}).get("segment_s", item.get("segment_s", 0))
        target = item.get("segment_us") or round(float(target_seconds) * 1e6)
        # Older receipts may omit target duration; their declared speed is still
        # revalidated, and the writer independently checks final audio timing.
        if not target and type(speed) in (int, float) and math.isfinite(speed) and speed > 0:
            target = round((window["end"] - window["start"]) / speed)
        # The parent of a multi-shot sentence summarizes semantic coverage;
        # playback belongs to each physical part, checked recursively below.
        problems = [] if item.get("multi_shot") else playback.conflicts(
            start_us=window["start"], end_us=window["end"], target_us=target, speed=speed,
            time_sensitive=receipt.get("time_sensitive"), observation=receipt.get("playback_observation"), index=index)
        reasons.extend(p["code"] for p in problems)
        if item.get("playback_observation") != receipt.get("playback_observation"):
            reasons.append("PLAYBACK_OBSERVATION_CHANGED")
        if receipt.get("video_speed") != speed or receipt.get("speed_range_override") != bounds:
            reasons.append("AI_SELECTION_SPEED_CHANGED")
        preferred = item.get("preferred_speed", 1.0)
        if (type(preferred) not in (int, float) or not math.isfinite(preferred) or preferred <= 0
                or receipt.get("preferred_speed") != preferred):
            reasons.append("AI_SELECTION_SPEED_PREFERENCE_CHANGED")
        for key in ("source_start", "source_end", "duration"):
            value = item.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (key == "duration" and value <= 0):
                reasons.append("AI_SELECTION_PLACEMENT_INVALID")
                break
        if abs(round(float(item.get("duration", 0)) * 1e6) - (_window(item)["end"] - _window(item)["start"])) > 2:
            reasons.append("AI_SELECTION_DURATION_CHANGED")
        if (abs(round(float(item.get("source_start", 0)) * 1e6) - _window(item)["start"]) > 2
                or (item.get("source_end") is not None and abs(round(float(item["source_end"]) * 1e6) - _window(item)["end"]) > 2)):
            reasons.append("AI_SELECTION_PLACEMENT_CHANGED")
        if type(receipt.get("time_sensitive")) is not bool:
            reasons.append("AI_SELECTION_TIME_POLICY_MISSING")
        if item.get("sentence_kind", receipt.get("sentence_kind")) != receipt.get("sentence_kind"):
            reasons.append("AI_SELECTION_PURPOSE_CHANGED")
        if item.get("multi_shot"):
            parts = item.get("span_parts")
            if not isinstance(parts, list) or len(parts) < 2:
                reasons.append("AI_SELECTION_PARTS_CHANGED")
            else:
                for part in parts:
                    check = validate_segment_receipt({**part, "text": item.get("text"),
                        "temporary_shot_id": part.get("shot_id"), "window_solution": part},
                        text=text, index=index, verify_source=verify_source, manifest=manifest)
                    reasons.extend(check["reasons"])
        confidence = receipt.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not .6 <= confidence <= 1:
            reasons.append("AI_SELECTION_CONFIDENCE_INVALID")
        if not all(isinstance(receipt.get(k), str) and receipt[k].strip() for k in ("observation", "reason")):
            reasons.append("AI_SELECTION_OBSERVATION_MISSING")
        identity = receipt.get("identity") or {}
        if not identity.get("model"):
            reasons.append("AI_SELECTION_IDENTITY_MISSING")
        recorded_rules = identity.get("editorial_rules")
        context_manifest = manifest if manifest is not None else {"category": (recorded_rules or {}).get("category")}
        if not isinstance(recorded_rules, dict) or {k: recorded_rules.get(k) for k in ("category", "versions")} != rules_identity(context_manifest):
            reasons.append("AI_SELECTION_RULES_CHANGED")
        scenes = editorial_context(context_manifest)["rules"].get("semantic_contracts", [])
        known_scenes = {scene["id"] for scene in scenes}
        if any(not isinstance(receipt.get(key), list) or any(sid not in known_scenes for sid in receipt[key])
               for key in ("scene_ids", "sentence_scene_ids")):
            reasons.append("AI_SELECTION_SCENE_INVALID")
        common = set(receipt.get("scene_ids", [])) & set(receipt.get("sentence_scene_ids", []))
        expected_speed = max((float(scene.get("preferred_speed", 1)) for scene in scenes if scene["id"] in common), default=1.0)
        if preferred != expected_speed:
            reasons.append("AI_SELECTION_SCENE_PREFERENCE_CHANGED")
        points = receipt.get("selling_points")
        if not isinstance(points, list):
            reasons.append("AI_SELECTION_POINTS_INVALID")
        elif receipt.get("sentence_kind") == "selling_point":
            required = {p["id"] for p in points if isinstance(p, dict) and p.get("id") and p.get("text")}
            supported = set(receipt.get("supported_point_ids") or [])
            if (len(required) != len(points) or not required or not supported <= required
                    or not points_supported(required, supported)):
                reasons.append("AI_SELECTION_POINTS_UNCOVERED")
            if item.get("selling_points", points) != points:
                reasons.append("AI_SELECTION_POINTS_CHANGED")
            missing = [p for p in points if p["id"] not in supported]
            if item.get("uncovered_selling_points", []) != missing:
                reasons.append("AI_SELECTION_COVERAGE_CHANGED")
        intervals = receipt.get("evidence_intervals_us")
        window = _window(item)
        if not isinstance(intervals, list) or not intervals:
            reasons.append("AI_SELECTION_EVIDENCE_MISSING")
        else:
            for ev in intervals:
                if (not isinstance(ev, dict) or any(type(ev.get(k)) is not int for k in ("start", "end"))
                        or not window["start"] <= ev["start"] < ev["end"] <= window["end"]):
                    reasons.append("AI_SELECTION_EVIDENCE_CUT_AWAY")
                    break
        if window["start"] < 0 or window["end"] <= window["start"]:
            reasons.append("AI_SELECTION_WINDOW_INVALID")
        if verify_source and source_identity(item["video"]) != receipt.get("source_identity"):
            reasons.append("AI_SELECTION_SOURCE_CONTENT_CHANGED")
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        reasons.append("AI_SELECTION_RECEIPT_INVALID")
    return {"ok": not reasons, "reasons": reasons}
