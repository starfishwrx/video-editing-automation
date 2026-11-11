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
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any

from .. import ai_editor_contract as contract
from . import timing_contract
from . import sentence_continuity
from . import experiment_groups
from .editorial_actions import window_action
from .ai_editor_recovery import WindowFailure, failed_index, failure_fields, proposed_window, repairable_index

ROUND_SIZE = 8
# Explore at most one additional round for an editorial alternative after a
# feasible selling-point match. Matching and evidence still take precedence.
ALTERNATIVE_REVIEW_LIMIT = 2 * ROUND_SIZE


def _analysis_digest(analysis: dict) -> str:
    """Bind all analysis facts, excluding embedded plans and lock telemetry."""
    payload = {k: v for k, v in analysis.items() if k != "ai_editor_plan"}
    vision = payload.get("vision_analysis")
    if isinstance(vision, dict):
        payload["vision_analysis"] = {k: v for k, v in vision.items() if k != "cache_wait_s"}
    return contract.digest(payload)


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def snapshot_previous_analysis(analysis: dict, report_dir: Path) -> None:
    """Save only an analysis whose full digest belongs to the existing plan."""
    from ..runtime.file_safety import atomic_write_text
    directory = report_dir / "ai_editor"
    try:
        previous = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
        payload = {k: v for k, v in analysis.items() if k != "ai_editor_plan"}
        digest = previous.get("identity", {}).get("analysis_sha256")
        if (digest != previous.get("resume_identity", {}).get("analysis_sha256")
                or digest not in {contract.digest(payload), _analysis_digest(payload)}):
            return
        path = directory / f"previous_analysis_{digest}.json"
        if path.exists():
            # Preserve the first receipt; a corrupt existing one must not be
            # silently replaced and will fail its digest check on resume.
            return
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError, TypeError):
        return


def _duration_migration_equivalent(previous: dict, current: dict) -> bool:
    """Compare all facts, excluding only duration ruler and lock telemetry."""
    def normalize(analysis):
        result = copy.deepcopy({k: v for k, v in analysis.items() if k != "ai_editor_plan"})
        for row in result.get("shots", []):
            for key in ("material_duration_us", "material_duration_source",
                        "material_duration_confidence"):
                row.pop(key, None)
        vision = result.get("vision_analysis")
        if isinstance(vision, dict):
            vision.pop("cache_wait_s", None)
        return result
    return normalize(previous) == normalize(current)


def _duration_resume_safe(previous: dict, resume_identity: dict, analysis: dict, directory: Path) -> bool:
    old_identity = previous.get("resume_identity") or {}
    if ({k: v for k, v in old_identity.items() if k != "analysis_sha256"}
            != {k: v for k, v in resume_identity.items() if k != "analysis_sha256"}):
        return False
    try:
        digest = old_identity.get("analysis_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return False
        snapshot = json.loads((directory / f"previous_analysis_{digest}.json").read_text(encoding="utf-8"))
        if (old_identity.get("analysis_sha256") != previous.get("identity", {}).get("analysis_sha256")
                or old_identity.get("analysis_sha256") not in {contract.digest(snapshot), _analysis_digest(snapshot)}
                or not _duration_migration_equivalent(snapshot, analysis)):
            return False
        # A legacy raw digest can differ solely because the newer digest omits
        # lock-wait telemetry. Its full snapshot proves no material bound moved.
        identity_only = _analysis_digest(snapshot) == _analysis_digest(analysis)
        # Joint groups and reviewed subwindows must be rebuilt if their reviewed
        # range crosses a new physical boundary; old facts stay in the snapshot.
        shots = {str(s["shot_id"]): s for s in analysis.get("shots", [])}
        for row in previous.get("rows", []):
            group_key = row.get("continuity_key")
            if group_key:
                if not identity_only:
                    return False
                context = previous["continuity_reviews"][group_key]["input"]
                from ..policy import continuity
                boundaries = ([(sid, value["end_us"]) for sid, value in context["sources"].items()]
                    if continuity.is_experiment(previous["continuity_reviews"][group_key]) else
                    [(context["shot_id"], context["window_us"]["end"])])
                for sid, end in boundaries:
                    limit = shots[sid].get("material_duration_us")
                    if type(limit) is not int or type(end) is not int or end > limit:
                        return False
            for part in row.get("parts", []):
                key = part.get("window_review_key")
                if key:
                    reviewed = previous.get("window_reviews", {}).get(key) or {}
                    limit = shots[part["shot_id"]].get("material_duration_us")
                    if (type(limit) is not int or type(reviewed.get("review_window_us", {}).get("end")) is not int
                            or reviewed["review_window_us"]["end"] > limit):
                        return False
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def _scene_ids(value, manifest: dict, error: str) -> tuple[list, list]:
    known = {row["id"] for row in contract.editorial_context(manifest)["rules"].get("semantic_contracts", [])}
    if not isinstance(value, list) or any(not isinstance(sid, str) for sid in value):
        raise ValueError(error)
    if not known:
        # This auxiliary taxonomy is inapplicable to e.g. weipin. Dropping it
        # grants no visual support; the actual pixel review still decides that.
        return [], value
    if any(sid not in known for sid in value):
        raise ValueError(error)
    return value, []


def _editor_model() -> str:
    from ..vision import vision_analyzer as vision
    if vision._vision_transport() == "qwen":
        return vision._qwen_settings()["model"] or "qwen-unconfigured"
    if os.environ.get("JY_AI_EDITOR_QWEN", "").strip() == "1":
        return os.environ.get("JY_VISION_TEXT_MODEL", "").strip() or "qwen-unconfigured"
    return os.environ.get("JY_VISION_MODEL") or "glm-5.3-flash"


_FRAME_US_CACHE: dict[str, int] = {}


def _source_frame_us(shot: dict) -> int:
    """Duration of one source frame from ffprobe (fastest stream rate).

    Unknown rates return zero: no endpoint adjustment is permitted.
    """
    path = str(shot.get("video") or "")
    if path in _FRAME_US_CACHE:
        return _FRAME_US_CACHE[path]
    value = 0
    import subprocess
    try:
        from ..runtime.platform_env import find_ffmpeg
        ffprobe = find_ffmpeg()[1]
        if ffprobe and path:
            out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                                  "stream=avg_frame_rate,r_frame_rate", "-of", "csv=p=0", path],
                                 capture_output=True, text=True, timeout=60, check=True).stdout
            rates = []
            for token in re.split(r"[,\s]+", out.strip()):
                if "/" in token:
                    num, den = token.split("/", 1)
                    if float(den) > 0:
                        rates.append(float(num) / float(den))
                elif token:
                    rates.append(float(token))
            rates = [r for r in rates if 1.0 <= r <= 240.0]
            if rates:
                value = int(round(1e6 / max(rates)))
    except (OSError, ValueError, ImportError, subprocess.SubprocessError):
        pass
    if value:
        _FRAME_US_CACHE[path] = value
    return value


def _call(prompt: str, path: Path, sheet: Path | None = None) -> dict:
    from ..vision import vision_analyzer as vision
    from ..vision import material_cache
    from ..runtime.file_safety import file_lock
    transport = vision._vision_transport()
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


def _prior_use_by_shot(manifest, shots):
    from . import usage_ledger
    scenes, sources = usage_ledger.other_task_usage(manifest)
    counts = {}
    for shot in shots:
        key = usage_ledger.shot_key(shot.get("video"), shot.get("source_start"))
        counts[str(shot["shot_id"])] = (sources.get(key.rpartition("@")[0], 0), scenes.get(key, 0))
    return counts


def _inventory(manifest: dict, shots: list[dict], directory: Path) -> list[dict]:
    from . import shot_notes
    rows = timing_contract.split_sentences(manifest)
    catalog = [{"shot_id": s["shot_id"], "description": s.get("visual_description") or s.get("description"),
                "action": s.get("action"), "object": s.get("object"), "result": s.get("result"),
                **({"action_segments": s.get("action_segments") or []} if manifest.get("ai_refill_feedback") else {}),
                "duration_us": round((float(s["source_end"]) - float(s["source_start"])) * 1e6)} for s in shots]
    def inventory_prompt(sentence_rows):
        return contract.policy_prompt(manifest) + "\n你负责整稿用途分类和宽召回，本轮描述只是索引，不是画面证据。\n" + json.dumps({
        "product": manifest.get("product"), "category": manifest.get("category"),
        "general_notes": manifest.get("shot_notes_general", []),
        "full_script_context": [{"index": i, "text": timing_contract.sentence_text(s)}
                                for i, s in enumerate(rows, 1)],
        "sentences": [{"index": i, "text": timing_contract.sentence_text(s),
                       "notes": shot_notes.note_for(manifest, i, s) or s.get("visual_requirements")}
                      for i, s in sentence_rows],
        "catalog": catalog}, ensure_ascii=False) + """
输出JSON对象 {"sentences":[{"index":1,"kind":"selling_point|product_intro|hook",
"selling_points":[{"id":"p1","text":"本句具体卖点"}],"candidate_ids":["最相关shot_id", "其余候选"],"scene_ids":[],
"experiment_role":"absorption|rewet|followup|bridge|breathability|fluorescence|sku_change|promotion|other|uncertain"}],
"display_pool_ids":["适合包装/拆包/产品展示的shot_id"]}。
纯招呼或承接语可额外给 hook_neighbor="next"（引出后句）或 "previous"（承上句）。有独立痛点、数字、功能动作、商品切换或画面备注时省略。
每句恰好一行，不改变原文。selling_point列出本句全部具体卖点，其他kind的selling_points为空。
结合前后句识别使用感受和功能结果。比如弹力腰围之后的“不勒”、护围之后的“翻身也安心”仍在讲具体卖点，优先召回对应结构、动作或结果。纯惊叹或招呼才用一般展示。
展示池在同等相关时要覆盖不同可见动作，例如陈列、拆包、取出、展开、局部展示。换桌面或机位重新举包只算一种。
纸品scene_ids必须给出本句语义适用的semantic_contracts场景id，不适用时为空；不能只因某候选有擦拭就把包装抽数句判为清洁。
每句candidate_ids只返回最相关的前8个，display_pool_ids也只返回前8个，避免响应被整库ID挤满。
程序会把未列出的镜头加入后续逐轮扩展。不能因描述不确定就宣布无素材，不用A/B强度或旧合同词表淘汰。
卫品experiment_role按原句语义分类：倒液/吸收卖点为absorption，纸巾按压/反渗/干爽验证为rewet；
只有明确承接同次吸水验证的总结/衔接才可followup/bridge。透气、荧光、促销、商品切换必须独立，
不能为了借时长并组。该角色只是分组召回，实际同次实验、动作与卖点必须另看真实画面。
如提供action_segments，允许每句返回candidate_windows=[{"shot_id":"候选ID","start":0.0,"end":1.0}]，
start/end使用原素材秒数，选择与该素材action_segments重叠且最可能体现旧稿缺口的时间段；
它仅作定位线索，实际画面必须另行复核。
"""
    def inventory_part(sentence_rows, path, *, split_allowed=True):
        try:
            data = _call(inventory_prompt(sentence_rows), path)
        except ValueError as exc:
            if (not any(code in str(exc) for code in ("[OUTPUT_TRUNCATED]", "[EMPTY_CONTENT]"))
                    or len(sentence_rows) < 2 or not split_allowed):
                raise
            middle = len(sentence_rows) // 2
            left = inventory_part(sentence_rows[:middle], path.with_name(path.stem + "_left.json"), split_allowed=False)
            right = inventory_part(sentence_rows[middle:], path.with_name(path.stem + "_right.json"), split_allowed=False)
            return {"sentences": left["sentences"] + right["sentences"],
                    "display_pool_ids": list(dict.fromkeys(left["display_pool_ids"] + right["display_pool_ids"]))}
        found = data.get("sentences")
        display = data.get("display_pool_ids")
        expected = {index for index, _ in sentence_rows}
        if (not isinstance(found, list) or len(found) != len(expected)
                or any(not isinstance(row, dict) or type(row.get("index")) is not int for row in found)
                or {row["index"] for row in found} != expected):
            raise ValueError("AI_EDITOR_SENTENCE_COVERAGE")
        if not isinstance(display, list) or any(not isinstance(item, str) for item in display):
            raise ValueError("AI_EDITOR_DISPLAY_POOL_INVALID")
        return data

    data = inventory_part(list(enumerate(rows, 1)), directory / "inventory.json")
    found = data.get("sentences")
    if not isinstance(found, list) or len(found) != len(rows):
        raise ValueError("AI_EDITOR_SENTENCE_COVERAGE")
    by_index = {}
    history = _prior_use_by_shot(manifest, shots)
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
        role = row.get("experiment_role", "other")
        if role not in experiment_groups.ROLES:
            raise ValueError("AI_EDITOR_EXPERIMENT_ROLE_INVALID")
        row.pop("standalone_durations_us", None)
        row.pop("hook_neighbor_context", None)
        if row.get("hook_neighbor") not in ("next", "previous"):
            row.pop("hook_neighbor", None)
        row["experiment_role"] = role
        known_scenes = {scene["id"] for scene in contract.editorial_context(manifest)["rules"].get("semantic_contracts", [])}
        scene_ids = row.get("scene_ids", [] if not known_scenes else None)
        row["scene_ids"], row["inventory_ignored_scene_ids"] = _scene_ids(
            scene_ids, manifest, "AI_EDITOR_SENTENCE_SCENE_INVALID")
        if not isinstance(points, list) or any(not isinstance(p, dict) or not p.get("id") or not p.get("text") for p in points):
            raise ValueError("AI_EDITOR_POINTS_INVALID")
        if len({p["id"] for p in points}) != len(points) or (row["kind"] == "selling_point") != bool(points):
            raise ValueError("AI_EDITOR_POINTS_INVALID")
        ids = row.get("candidate_ids")
        if not isinstance(ids, list) or any(not isinstance(s, str) for s in ids):
            raise ValueError("AI_EDITOR_CANDIDATE_UNKNOWN")
        ids, row["inventory_ignored_candidate_ids"], row["inventory_resolved_candidate_ids"] = resolve_ids(ids)
        ids.sort(key=history.__getitem__)
        row["candidate_ids"] = ids
        by_index[index] = {**row, "text": timing_contract.sentence_text(rows[index - 1]),
                           "candidate_ids": list(dict.fromkeys(ids + [str(s["shot_id"]) for s in shots]))}
    display = data.get("display_pool_ids")
    if not isinstance(display, list) or any(not isinstance(s, str) for s in display):
        raise ValueError("AI_EDITOR_DISPLAY_POOL_INVALID")
    display, ignored_display, resolved_display = resolve_ids(display)
    display.sort(key=history.__getitem__)
    result = [by_index[i] for i in range(1, len(rows) + 1)]
    # Each hook/intro has its own retrieval hints. Combine those with the
    # common product-display shortlist before falling back to the full pool.
    line_hints = [display, *(row["candidate_ids"] for row in found if row["kind"] != "selling_point")]
    display_hints = []
    for rank in range(ROUND_SIZE):
        display_hints.extend(line[rank] for line in line_hints if rank < len(line))
    display = sorted(dict.fromkeys(display_hints), key=history.__getitem__)
    # Keep the review budget fixed. When all first-round hints are repeats,
    # give one previously unused file a chance to qualify on actual pixels.
    display = list(dict.fromkeys([*display, *history]))
    if len(display) >= ROUND_SIZE and all(history[sid][0] for sid in display[:ROUND_SIZE]):
        fresh = next((sid for sid in history if history[sid][0] == 0), None)
        if fresh is not None:
            if fresh in display:
                display.remove(fresh)
            display.insert(ROUND_SIZE - 1, fresh)
    for row in result:
        row["inventory_ignored_display_ids"] = ignored_display
        row["inventory_resolved_display_ids"] = resolved_display
        if row["kind"] != "selling_point":
            row["candidate_ids"] = list(display)
    return result


def _review_media(shot: dict, directory: Path, *, dense: bool = False, fit: bool = False) -> dict:
    from ..vision import vision_analyzer as vision
    from ..runtime import platform_env
    from . import shot_analyzer
    from ..vision import material_cache
    from ..runtime.runtime_policy import media_slot
    start, end = float(shot["source_start"]), float(shot["source_end"])
    if not math.isfinite(start + end) or not 0 <= start < end:
        raise ValueError("AI_EDITOR_SOURCE_WINDOW_INVALID")
    source = str(shot["video"])
    before = contract.source_identity(source)
    count = 16 if dense else 8
    last = end - min(.04, (end - start) / 10)
    times = [start + (last - start) * n / (count - 1) for n in range(count)]
    protocol = f"candidate-even-window-{count}-v1"
    focus = shot.get("_review_focus_window")
    if isinstance(focus, (list, tuple)) and len(focus) == 2:
        lo, hi = map(float, focus)
        if not (math.isfinite(lo + hi) and start <= lo < hi <= end):
            raise ValueError("AI_EDITOR_REVIEW_FOCUS_INVALID")
        focus_last = hi - min(.04, (hi - lo) / 10)
        times = sorted(set([*times, lo, (lo + focus_last) / 2, focus_last]))
        protocol = f"candidate-focus-window-{count}-v1:{lo}:{hi}"
    def prepare_frames(frame_dir):
        ffmpeg, _ = platform_env.find_ffmpeg()
        if not ffmpeg:
            raise ValueError("AI_EDITOR_FFMPEG_MISSING")
        frames = []
        with media_slot(timeout=600) as handle:
            if not handle.acquired:
                raise ValueError("AI_EDITOR_MEDIA_SLOT_TIMEOUT")
            requests = [(timestamp, frame_dir / f"{i:02d}.jpg")
                        for i, timestamp in enumerate(times)]
            flags = shot_analyzer.extract_frames_batch(source, requests, ffmpeg=ffmpeg)
            if len(flags) != len(requests):
                raise ValueError("AI_EDITOR_FRAME_EXTRACTION_FAILED")
            for (timestamp, target), ok in zip(requests, flags):
                if not ok or not target.is_file() or target.stat().st_size == 0:
                    raise ValueError("AI_EDITOR_FRAME_EXTRACTION_FAILED")
                frames.append({"path": str(target), "timestamp": timestamp})
        return frames
    frames = material_cache.cached_frames(shot, directory, prepare_frames,
        protocol=protocol)
    tiles = [{**frame, "label": f"t={frame['timestamp']:.3f}s"} for frame in frames]
    sheet_parameters = ({"columns": 4, "tile_width": 270, "tile_height": 514, "fit": True}
                        if fit else {"columns": 4, "tile_width": 420, "tile_height": 300})
    def prepare_sheet(target):
        with media_slot(timeout=600) as handle:
            if not handle.acquired:
                raise ValueError("AI_EDITOR_MEDIA_SLOT_TIMEOUT")
            return vision._write_sheet(tiles, target, **sheet_parameters,
                                       ffmpeg=platform_env.find_ffmpeg()[0])
    sheet = material_cache.cached_sheet(tiles, directory, prefix=f"candidate{count}",
        parameters=sheet_parameters, prepare=prepare_sheet)
    if sheet is None:
        raise ValueError("AI_EDITOR_CONTACT_SHEET_FAILED")
    if before != contract.source_identity(source):
        raise ValueError("AI_EDITOR_SOURCE_CHANGED_DURING_REVIEW")
    return {"start": start, "end": end, "before": before, "times": times, "sheet": sheet}


def _iter_review_round(ids, shots, directory, review, *, dense=False, on_error=None):
    from ..runtime.runtime_policy import load_policy
    from ..runtime.ready_work import iter_ready
    policy = load_policy()
    def prepare(sid):
        # The API worker consumes exactly the sheet produced here.
        return _review_media(shots[sid], directory, dense=dense)
    def error(sid, exc):
        if not isinstance(exc, (ValueError, OSError, TypeError, KeyError, TimeoutError)):
            raise exc
        if on_error is not None:
            return on_error(sid, exc)
        return {"shot_id": sid, "verdict": "uncertain", "error": str(exc)}
    return iter_ready(ids, prepare, review, prep_workers=policy.prep_workers,
                      api_workers=min(policy.api_workers, len(ids)), on_error=error, pass_prepared=True)


def _shot_value_request(sentence: dict) -> dict | None:
    """Only exact spoken phrases may be assessed; paraphrases remain unscored."""
    text = str(sentence.get("text") or "")
    if sentence.get("index") == 0 and sentence.get("kind") == "product_intro":
        return {"text": text, "display_goal": "general_product",
                "checks": [{"id": "general_display", "type": "display", "claim_text": text}]}
    if sentence.get("kind") != "selling_point":
        return None
    points = sentence.get("selling_points") or []
    if not points or any(not isinstance(p, dict) or not p.get("id") or
            not isinstance(p.get("text"), str) or not p["text"].strip() or p["text"] not in text for p in points):
        return None
    return {"text": text, "display_goal": "visible_feature",
            "checks": [{"id": p["id"], "type": "display", "claim_text": p["text"]} for p in points]}


def _feedback_for_shot(sentence: dict, shot_id) -> dict:
    """Scope old QC feedback to the shot it was written about.

    A same-shot recut window and the old cut's observation describe the
    previous shot only. Another candidate must be judged from its own pixels,
    so the recut window is removed and the feedback is marked as foreign.
    """
    feedback = sentence.get("qc_feedback")
    if not isinstance(feedback, dict):
        return sentence
    recut = feedback.get("same_shot_recut")
    recut_shot = str(recut.get("shot_id")) if isinstance(recut, dict) and recut.get("shot_id") else None
    prior = {str(s) for s in feedback.get("prior_shot_ids") or []}
    if recut_shot is not None:
        prior.add(recut_shot)
    if str(shot_id) in prior or (not prior and recut_shot is None):
        return sentence
    scoped = {k: v for k, v in feedback.items() if k != "same_shot_recut"}
    scoped["applies_to"] = ("旧稿质检反馈描述的是旧镜头" + (",".join(sorted(prior)) if prior else "") +
        "，不是本镜头；本镜头的observation与evidence_intervals_us只能来自附图，不得沿用旧镜头的描述或时间区间。")
    return {**sentence, "qc_feedback": scoped}


def _review(manifest: dict, sentence: dict, shot: dict, directory: Path, *, dense: bool = False,
            prepared_media=None) -> dict:
    media = prepared_media if prepared_media is not None else _review_media(shot, directory, dense=dense)
    if prepared_media is not None and media["before"] != contract.source_identity(shot["video"]):
        raise ValueError("AI_EDITOR_SOURCE_CHANGED_DURING_REVIEW")
    start, end, before, times, sheet = (media[k] for k in ("start", "end", "before", "times", "sheet"))
    key = contract.digest([sentence["index"], shot["shot_id"], before, start, end, dense])[:24]
    sentence = _feedback_for_shot(sentence, shot["shot_id"])
    feedback_instruction = ("\n本句旧稿质检反馈仅作定位和待核查缺口线索，不是当前素材证据，也可能有误。"
        "判断必须遵守生效业务规则；反馈与规则冲突时不得把反馈中的额外要求变成新规则。"
        "对实际适用的缺口复看当前像素，不得凭反馈或素材描述补造动作与结果。\n") if sentence.get("qc_feedback") else ""
    value_request = _shot_value_request(sentence)
    shared_display = (sentence["index"] == 0 and sentence["kind"] == "product_intro"
                      and not sentence.get("selling_points") and not sentence.get("qc_feedback")
                      and not sentence.get("action_continuation"))
    if shared_display:
        # This observation answers a product/window question. Placement and
        # dense refinement still use each task's actual narration below.
        review_context = {"source_identity": contract.digest(before),
                          "product_identity": {k: manifest.get(k) for k in ("product_id", "product_key", "brand", "sku")},
                          "sentence": {"index": 0, "kind": "product_intro",
                          "text": sentence["text"], "selling_points": []}}
    else:
        review_context = {"sentence": sentence, "shot_id": shot["shot_id"],
                          "whole_script": [timing_contract.sentence_text(s)
                                           for s in timing_contract.split_sentences(manifest)]}
    prompt = contract.policy_prompt(manifest) + feedback_instruction + "\n只根据附图判断，所有时间戳均为原素材秒数。\n" + json.dumps({
        "product": manifest.get("product"), "category": manifest.get("category"),
        **review_context, "shot_value_request": value_request,
        "scene_us": {"start": round(start * 1e6), "end": round(end * 1e6)}}, ensure_ascii=False) + """
输出JSON对象：{"verdict":"matched|mismatch|uncertain", "same_product":true,
"basis":"editorial_fit|visible_result|readable_label|product_display", "observation":"实际可见事实",
"reason":"为什么适合或不适合", "confidence":0.8, "supported_point_ids":["p1"],
"evidence_intervals_us":[{"start":1000000,"end":2000000}], "time_sensitive":false, "scene_ids":[]}。
scene_ids只列出本句语义适用且本镜实际支持的纸品semantic_contracts场景id；不支持或本句不涉及的场景不能列入，不适用时为空。
避开空等、调整机位、拿错物品后重来、主体长期遮挡等废弃画面。若同一条有干净的有效动作，只给那一段的证据；必要的停留亮结果或连贯入画应保留。
共享展示池句子index=0时，独立记录可读的紧凑展示区间，由剪辑阶段对齐每句配音。判断是否为同商品包装/拆包/取出/手持或本体展示，不承担功能证明。
sentence.action_continuation 为 true 且没有 hook_neighbor_context 时，这句没有卖点，判断附图是不是上一镜动作还能再延续的一段。同商品且动作仍在继续就 matched，不要因为没有新卖点而拒绝。证据区间落在 scene_us 内，长度不要超过这句配音。
卖点可以只支持其中几项，必须如实返回支持的id，不能凭口播推断。区间必须在提供的scene_us内，
包含支持判断必需的完整动作和结果；感受性展示按实际贴切区间。看不清返回uncertain，不能凭模型强度标签推断。
具体香型、成分、数量和数值须有画面可读的对应标称或可确认的直接证据；香薰、花朵等氛围只能表达一般香味体验，不能证明某种具体香型。检测仪外观或数字本身也不能证明仪器用途与检测结论。
相邻口播可连续承接同一实验；结合全稿，只返回本句实际承担的动作阶段/结果区间，不要求每一句从头重播整个实验。
sentence.audio_durations_us是真实配音时长。请给最短但足够支持判断的一个紧凑片段，不要把整条素材或多次重复演示全部列为必需证据。
优先返回足以体现卖点的完整源区间；时长不够时优先查找另一段短且完整的演示。
可选返回playback_observation={"schema":"reviewed-playback-window-v1","kind":"process","start_us":1000000,"end_us":2000000,"observed":"本次看到的纯过程及边界事实"}。
仅当该整个窗口不含关键接触、完成、提起、结果、计时证明时才能声明process；有这些内容或阶段不确定时省略，程序保持原速。不得凭口播或旧描述填写过程许可。
轻薄/柔软/舒适展示不要求从第一次持握一直播到最后一次抚摸，也不要为凑配音时长裁掉关键动作与结果。
evidence_intervals_us内各区间会被当作全部必须保留，不能把多个可替代的重复演示都列入；选最贴切的一次。
性能实验仍要保留本句必要过程和可见结果，无法在时长内支持时如实返回不确定或部分卖点，不为缩短而删除必需证据。
shot_value_request非空时，可额外返回shot_value_observation，逐项评估镜头传达了哪些原文信息；只依据实际画面，不能把清晰、运动或一般包装当成具体功能证据。
格式为{"all_spoken_claims_checked":false,"uncovered_spoken_claims":[],"claims":[{"check_id":"原请求id","claim_text":"原请求的原文",
"role":"direct_demonstration|feature_presentation|general_product|generic_motion|unrelated|uncertain","coverage":"full|partial|none",
"adds_claim_information":true,"specific_link":true,"action_or_presentation":"画面动作","visible_change_or_feature":"实际可见特征",
"claim_link":"画面如何对应这项原文","missing_information":"未看到什么","evidence_intervals_us":[{"start":1000000,"end":2000000}]}]}。
共享展示池只评价同商品一般展示，不证明任何具体口播。shot_value_observation只是排序参考，不替代上面的逐句证据判断或最终独立质检；不确定就省略。
可另返回shot_usability_observation，仅描述产品主体的实际可见性，不判断具体卖点：
{"view_kind":"stable_display|complete_demonstration|handling_or_setup|uncertain","subject_visibility":"clear|partial|blocked|uncertain",
"demonstration_complete":false,"information_point":"实际看见的主体或特征","limitations":"观察限制",
"clear_intervals_us":[{"start":1000000,"end":2000000,"observed":"这段为何看得清"}]}。
区间只填实际看清主体的时间，只用附图和原素材时间；遮挡或不确定须如实说。主体清晰不意味着支持功能或本句卖点。
"""
    if sentence.get("hook_neighbor_context"):
        prompt += """
本次拟把无独立画面诉求的招呼或衔接语与邻句连播。hook_neighbor_context 给出方向、相邻口播及已裁镜头的观察。
根据原文、备注和本次窗口的实际图片，判断是否确为纯衔接语，以及该段能否自然引出下一镜或延续上一镜的同一动作／展示。
有独立痛点、价格数量、功能、指向对象或指定画面时，不能仅凭同商品通过。准备空镜、遮挡、跳切或无关动作也不通过。
返回 hook_link_supported 布尔值；仅两项判断均有依据时填 true 并说明所见。"""
    result = _call(prompt, directory / "reviews" / f"{key}.json", sheet)
    if result.get("verdict") not in {"matched", "mismatch", "uncertain"} or type(result.get("same_product")) is not bool:
        raise ValueError("AI_EDITOR_REVIEW_INVALID")
    if sentence.get("hook_neighbor_context") and result.get("hook_link_supported") is not True:
        result = {**result, "verdict": "uncertain"}
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
        result["scene_ids"], result["ignored_scene_ids"] = _scene_ids(
            scene_ids, manifest, "AI_EDITOR_SCENE_INVALID")
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
        playback_observation = result.get("playback_observation")
        if playback_observation is not None:
            process = contract.playback.process_window(playback_observation)
            if process is None or not round(start * 1e6) <= process[0] < process[1] <= round(end * 1e6):
                raise ValueError("AI_EDITOR_PLAYBACK_OBSERVATION_INVALID")
    raw_value = result.pop("shot_value_observation", None)
    result.pop("selling_point_value", None)  # Only our validated assessment may use this key.
    usability = result.pop("shot_usability_observation", None)
    result.pop("usability", None)
    if value_request is not None and raw_value is not None and result["verdict"] == "matched":
        from . import shot_value
        try:
            value = shot_value.assess(raw_value, value_request,
                {"start": round(start * 1e6), "end": round(end * 1e6)})
            # The optional assessment cannot invent support not present in the
            # main review. Generic display is deliberately not a spoken claim.
            if (sentence.get("index") == 0 or
                    set(value["supported_ids"]) <= set(result.get("supported_point_ids") or [])):
                result["selling_point_value"] = value
                observed_ids = {r["check_id"] for r in raw_value["claims"]}
                claimed_ids = ({"general_display"} if sentence.get("index") == 0
                               else set(result.get("supported_point_ids") or []))
                contradicts = (observed_ids & claimed_ids) - set(value["supported_ids"])
                if contradicts:
                    result["shot_value_conflict"] = sorted(contradicts)
                    result["verdict"] = "uncertain"
        except (ValueError, TypeError, KeyError):
            pass  # Older or uncertain observations remain usable, unranked.
    if usability is not None and result["verdict"] == "matched":
        from . import shot_usability
        try:
            bounds = {"start": round(start * 1e6), "end": round(end * 1e6)}
            shot_usability.assess(usability, {"display_goal": "general_product"}, bounds, check_relation=False)
            result["shot_usability_observation"] = usability
        except (ValueError, TypeError, KeyError, ZeroDivisionError):
            pass
    durations = sentence.get("audio_durations_us") or []
    valid_durations = [v for v in durations if type(v) is int and v > 0]
    intervals = result.get("evidence_intervals_us") or []
    broad = (result["verdict"] == "matched" and valid_durations and intervals
             and max(ev["end"] for ev in intervals) - min(ev["start"] for ev in intervals)
             > 2 * min(valid_durations))
    return {**result, "dense_required": not dense and (result["verdict"] == "uncertain" or bool(broad)),
            "review_window_us": {"start": round(start * 1e6), "end": round(end * 1e6)},
            "shot_id": shot["shot_id"], "source_identity": before,
            "frame_times_us": [round(t * 1e6) for t in times], "sheet": str(sheet)}


def _reviewed_allocations(manifest, shots, group, duration):
    from . import shot_analyzer
    observed = [_observed_shot(shots[r["shot_id"]], r) for r in group]
    if len(group) == 1:
        solution = shot_analyzer.candidate_window(observed[0], audio_duration_us=duration,
            manifest=manifest, time_sensitive=group[0]["time_sensitive"])
        return [duration] if solution.ok else None
    spans = tuple(shot_analyzer.span_segment_for(s, manifest=manifest,
        time_sensitive=r["time_sensitive"], video=s["video"], shot_id=s["shot_id"])
        for s, r in zip(observed, group))
    pad = timing_contract.us_of(manifest.get("tail_pad_s"), 0)
    solution = timing_contract.solve_span(timing_contract.SpanRequest(segments=spans,
        audio_duration_us=duration, tail_pad_us=pad, fps=float(manifest.get("fps", 30))))
    return ([part.segment_us - (pad if i == len(group) - 1 else 0) for i, part in enumerate(solution.parts)]
            if solution.ok else None)


def _cached_row_feasible(manifest, analysis, plan, sentence, narration_timing):
    """Validate selected narrow observations before asking for more paid reviews."""
    from . import shot_analyzer
    try:
        index = sentence["index"]
        row = next(r for r in plan.get("rows", []) if r["index"] == index)
        if any(p.get("shot_id") in qc_rejected_shots(manifest).get(index, set()) for p in row["parts"]):
            return False
        groups = sentence_continuity.validate_plan(manifest, analysis, plan, narration_timing)
        group = groups.get(row.get("continuity_key"))
        if contract.continuity.is_experiment(group):
            experiment_groups.assemble_member(manifest, plan, group, index,
                timing_contract.split_sentences(manifest)[index - 1])
            return True
        shots = {s["shot_id"]: s for s in analysis["shots"]}
        duration, _ = timing_contract.duration_for(narration_timing, index,
            segment=timing_contract.split_sentences(manifest)[index - 1])
        if sum(p["audio_duration_us"] for p in row["parts"]) != duration:
            return False
        supported = set()
        for n, part in enumerate(row["parts"]):
            revised = plan.get("window_reviews", {}).get(part.get("window_review_key"), {})
            shot = shots[part["shot_id"]]
            if (revised.get("verdict") != "matched" or revised.get("sentence_index") != index
                    or revised.get("shot_id") != part["shot_id"]
                    or revised.get("continuity_key") != row.get("continuity_key")
                    or revised.get("source_identity") != contract.source_identity(shot["video"])):
                return False
            window = revised["review_window_us"]
            if not round(shot["source_start"] * 1e6) <= window["start"] < window["end"] <= round(shot["source_end"] * 1e6):
                return False
            observed = _observed_shot(shot, revised)
            observed["source_start"] = max(observed["source_start"], part.get("source_start_us", window["start"]) / 1e6)
            cfg = {**manifest, "tail_pad_s": manifest.get("tail_pad_s", 0) if n == len(row["parts"]) - 1 else 0}
            if "source_start_us" in part:
                cfg["head_trim_s"] = max(0, timing_contract.us_of(manifest.get("head_trim_s"), 300_000) / 1e6
                                            - (observed["source_start"] - shot["source_start"]))
                cfg["tail_trim_s"] = max(0, float(manifest.get("tail_trim_s", 0) or 0)
                                            - (shot["source_end"] - observed["source_end"]))
            if group:
                cfg.update(head_trim_s=0, tail_trim_s=0)
            if not shot_analyzer.candidate_window(observed, audio_duration_us=part["audio_duration_us"],
                    manifest=cfg, time_sensitive=revised["time_sensitive"]).ok:
                return False
            supported.update(revised.get("supported_point_ids", []))
        return contract.points_supported({p["id"] for p in sentence["selling_points"]}, supported)
    except (ValueError, KeyError, TypeError, OSError, StopIteration):
        return False


def _cached_reviews_feasible(manifest, sentence, shots, reviews, audio):
    """Validate timing using existing pixels; never invoke a model."""
    from . import shot_analyzer
    required = {p["id"] for p in sentence["selling_points"]}
    excluded = qc_rejected_shots(manifest).get(sentence["index"], set())
    good = [r for r in reviews if r.get("verdict") == "matched" and r.get("shot_id") in shots
            and r.get("shot_id") not in excluded]
    durations = sentence.get("standalone_durations_us") or sentence.get("audio_durations_us") or [
        timing_contract.duration_for(audio, sentence["index"], segment={"text": sentence["text"]})[0]]
    for duration in durations:
        available = False
        for size in range(1, min(len(good), int(manifest.get("span_max_parts", 3))) + 1):
            for group in combinations(good, size):
                if not contract.points_supported(required, {p for r in group for p in r.get("supported_point_ids", [])}):
                    continue
                if _reviewed_allocations(manifest, shots, group, duration) is not None:
                    available = True
                    break
            if available:
                break
        if not available:
            return False
    return True


def qc_rejected_shots(manifest: dict) -> dict[int, set[str]]:
    """Keep post-draft pixel rejections scoped to their original sentence."""
    out = {}
    raw = manifest.get("qc_rejected_shots")
    if not isinstance(raw, dict):
        return out
    for key, values in raw.items():
        try:
            index = int(key)
        except (TypeError, ValueError):
            continue
        if index > 0 and isinstance(values, (list, tuple, set)):
            out[index] = {str(value) for value in values if value}
    return out


def _review_pool(manifest: dict, sentence: dict, shots: dict, directory: Path, audio: dict, *,
                 exhaustive: bool = False,
                 existing_reviews: list[dict] | None = None,
                 excluded_ids: set[str] | None = None,
                 early_window_repair: bool = False) -> tuple[list[dict], dict]:
    """Review candidates until selling-point evidence is matched or the pool ends.

    ``early_window_repair`` is retained for call-site compatibility. Matched
    evidence always stops further search so placement can try a short crop,
    mild speed-up, or sentence group instead of scanning the rest of the pool.
    """
    _ = early_window_repair  # API compatibility only; evidence-stop is unconditional.
    from . import shot_analyzer
    def feasible(good: list[dict]) -> bool:
        return _cached_reviews_feasible(manifest, sentence, shots, good, audio)

    def display_variety_ready(good):
        # Neutral lines still need a varied fallback when they cannot join a
        # neighbor. Their lengths do not become mandatory feasibility gates.
        durations = sentence.get("audio_durations_us") or []
        if (sentence["index"] != 0 or len(durations) < 2 or len(reviewed_by_id) >= ALTERNATIVE_REVIEW_LIMIT
                or len(reviewed_by_id) == len(ids)):
            return True
        if not any(shots[sid].get("action") or shots[sid].get("action_segments") for sid in ids):
            return True
        actions = set()
        for review in good:
            shot = shots[review["shot_id"]]
            for duration in set(durations):
                cut = shot_analyzer.candidate_window(_observed_shot(shot, review), audio_duration_us=duration,
                        manifest=manifest, time_sensitive=review["time_sensitive"])
                if cut.ok:
                    actions.add(window_action(shot, cut.source_start_us / 1e6, cut.source_end_us / 1e6))
            if len(actions - {""}) >= 2:
                return True
        return False
    reviewed_by_id = {str(row.get("shot_id")): row for row in (existing_reviews or [])
                      if isinstance(row, dict) and row.get("shot_id") in shots
                      and row.get("shot_id") not in (excluded_ids or set())
                      and not failure_fields(str(row.get("error", ""))).get("retryable")}
    ids = [sid for sid in sentence["candidate_ids"] if sid not in (excluded_ids or set())]
    required = {p["id"] for p in sentence["selling_points"]}
    for offset in range(0, len(ids), ROUND_SIZE):
        existing_good = [r for r in reviewed_by_id.values() if r.get("verdict") == "matched"]
        existing_points = {p for r in existing_good for p in r.get("supported_point_ids", [])}
        evidence_found = bool(required) and contract.points_supported(required, existing_points)
        enough = ((required and (len(existing_good) >= 2
                   or len(reviewed_by_id) >= min(ALTERNATIVE_REVIEW_LIMIT, len(ids))))
                  or (not required and len(existing_good) >= min(3, len(ids))))
        if not exhaustive and enough and feasible(existing_good) and display_variety_ready(existing_good):
            break
        # Matched selling-point evidence ends the search. Timing fit is a
        # separate placement path (short crop, mild speed, sentence group);
        # do not keep paying to scan the rest of the pool for a shorter match.
        if not exhaustive and evidence_found and not feasible(existing_good):
            break
        chunk = [sid for sid in ids[offset:offset + ROUND_SIZE] if sid not in reviewed_by_id]
        if not chunk:
            continue
        def review(sid: str, media=None) -> dict:
            try:
                return _review(manifest, sentence, shots[sid], directory, prepared_media=media)
            except (ValueError, OSError, TypeError, KeyError) as exc:
                return {"shot_id": sid, "verdict": "uncertain", "error": str(exc)}
        completed = {}
        for sid, result in _iter_review_round(chunk, shots, directory, review):
            completed[sid] = result
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
            def dense_review(sid, media=None):
                previous = reviewed_by_id[sid]
                try:
                    refined = _review(manifest, sentence, shots[sid], directory, dense=True, prepared_media=media)
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
            reviewed_by_id.update(_iter_review_round(
                dense_ids, shots, directory, dense_review, dense=True,
                on_error=lambda sid, exc: ({**reviewed_by_id[sid], "dense_required": False,
                    "refinement_status": "retained_original"}
                    if reviewed_by_id[sid]["verdict"] == "matched" else
                    {"shot_id": sid, "verdict": "uncertain", "error": str(exc)})))
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
            def guarded_review(sid, media=None):
                previous = reviewed_by_id[sid]
                try:
                    refined = _review(manifest, sentence, guarded[sid], directory, dense=True, prepared_media=media)
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    refined = {"verdict": "uncertain", "error": str(exc)}
                if (refined.get("verdict") == "matched"
                        and refined.get("source_identity") == previous.get("source_identity")
                        and set(previous.get("supported_point_ids", [])) <= set(refined.get("supported_point_ids", []))
                        and set(previous.get("scene_ids", [])) <= set(refined.get("scene_ids", []))
                        and previous.get("time_sensitive") == refined.get("time_sensitive")):
                    return {**refined, "guarded_review_status": "confirmed"}
                return {**previous, "guarded_review_status": "unconfirmed"}
            reviewed_by_id.update(_iter_review_round(
                list(guarded), guarded, directory, guarded_review, dense=True,
                on_error=lambda sid, exc: {**reviewed_by_id[sid], "guarded_review_status": "unconfirmed"}))
        reviewed = [reviewed_by_id[sid] for sid in ids if sid in reviewed_by_id]
        good = [r for r in reviewed if r["verdict"] == "matched"]
        covered = {p for r in good for p in r.get("supported_point_ids", [])}
        evidence_found = bool(required) and contract.points_supported(required, covered)
        if not exhaustive and evidence_found and feasible(good):
            if (len(good) >= 2 or len(reviewed) >= min(ALTERNATIVE_REVIEW_LIMIT, len(ids))
                    or len(reviewed) == len(ids)):
                break
        if not exhaustive and evidence_found and not feasible(good):
            break
        if not exhaustive and not required and len(good) >= min(3, len(ids)) and feasible(good) and display_variety_ready(good):
            break
    reviewed = [reviewed_by_id[sid] for sid in ids if sid in reviewed_by_id]
    good = [r for r in reviewed if r["verdict"] == "matched"]
    supported = {point for row in good for point in row.get("supported_point_ids", [])}
    is_feasible = feasible(good)
    evidence_found = (bool(required) and contract.points_supported(required, supported)) or (
        not required and bool(good))
    uncertain = sum(r["verdict"] == "uncertain" or r.get("guarded_review_status") == "unconfirmed"
                    for r in reviewed)
    exhausted = len(reviewed) == len(ids)
    if is_feasible:
        reason = "READY"
    elif required and not contract.points_supported(required, supported):
        reason = "EVIDENCE_UNKNOWN" if uncertain or not exhausted else "NO_SUPPORT_AFTER_REVIEW"
    elif not required and not good:
        reason = "EVIDENCE_UNKNOWN" if uncertain or not exhausted else "NO_DISPLAY_AFTER_REVIEW"
    else:
        reason = "KNOWN_WINDOWS_INFEASIBLE"
    return reviewed, {"checked": len(reviewed), "total": len(ids), "exhausted": exhausted,
                      "feasible": is_feasible, "evidence_found": evidence_found, "reason": reason,
                      "failed_calls": sum("error" in r for r in reviewed),
                      "uncertain": uncertain}


def _scene_preference(manifest: dict, sentence: dict, review: dict) -> float:
    common = set(sentence.get("scene_ids", [])) & set(review.get("scene_ids", []))
    return max((float(scene.get("preferred_speed", 1.0)) for scene in
        contract.editorial_context(manifest)["rules"].get("semantic_contracts", []) if scene["id"] in common), default=1.0)


def _placement_fit_observation(review: dict) -> dict | None:
    """Let a suitable short crop play up to 2x inside the pixels already reviewed.

    The placement pass certifies the crop. It does not have to call the action a
    pure process. Speed stays as close to 1x as the evidence span allows.
    """
    if review.get("placement_review") is not True or review.get("time_sensitive"):
        return None
    if contract.playback.process_window(review.get("playback_observation")):
        return None
    window = review.get("review_window_us") or {}
    start, end = window.get("start"), window.get("end")
    if type(start) is not int or type(end) is not int or not start < end:
        return None
    return {
        "schema": contract.playback.OBSERVATION_SCHEMA,
        "kind": "placement_fit",
        "observed": "放置短窗已复核完整，仅在该裁切内加速以罩住证据",
        "start_us": start,
        "end_us": end,
    }


def _observed_shot(shot: dict, review: dict) -> dict:
    # These estimates belong to the original scene, even after a requested
    # in-point moves. A reviewed placement already certifies the exact crop.
    clean_edges = None
    exact_cut = review.get("placement_review") or review.get("continuity_key") or review.get("action_continuation")
    if not exact_cut and (shot.get("head_waste") or shot.get("tail_waste")):
        start, end = (round(float(shot[k]) * 1e6) for k in ("source_start", "source_end"))
        head, tail = timing_contract.waste_guards_us(start, end, shot.get("head_waste"), shot.get("tail_waste"), 0, 0)
        clean_edges = (start + head, end - tail)
    window = review.get("review_window_us")
    if window is not None:
        if (not isinstance(window, dict) or any(type(window.get(k)) is not int for k in ("start", "end"))
                or not round(float(shot["source_start"]) * 1e6) <= window["start"] < window["end"]
                   <= round(float(shot["source_end"]) * 1e6)):
            raise ValueError("AI_EDITOR_REVIEW_WINDOW_INVALID")
        # Local pixel review certifies only this source window. Both the
        # candidate solver and final assembly must use those same boundaries.
        shot = {**shot, "source_start": window["start"] / 1e6, "source_end": window["end"] / 1e6}
    evidence = [{"start": ev["start"] / 1e6, "end": ev["end"] / 1e6,
                 "claim": review["observation"], "evidence_source": "ai_editor_pixel_review"}
                for ev in review["evidence_intervals_us"]]
    role = "direct_evidence" if review.get("time_sensitive") else "product_display"
    observation = review.get("playback_observation")
    minimum, maximum = contract.playback.speed_range(
        time_sensitive=review.get("time_sensitive", False), observation=observation)
    process = contract.playback.process_window(observation)
    if process and not review.get("time_sensitive"):
        shot = {**shot, "source_start": max(float(shot["source_start"]), process[0] / 1e6),
                "source_end": min(float(shot["source_end"]), process[1] / 1e6)}
    return {**shot, "evidence_intervals": evidence,
            "playback_observation": observation,
            "selection_mode": "ai_editor", "speed_range_override": [minimum, maximum],
            "preferred_speed": review.get("preferred_speed", 1.0),
            "role": "direct_evidence" if review.get("time_sensitive") else "product_display",
            "visual_role": "direct_evidence" if review.get("time_sensitive") else "product_display",
            "action_complete": True, "head_waste": 0, "tail_waste": 0, "preferred_clean_edges_us": clean_edges}


def _assemble_segments(manifest, analysis, plan, narration_timing, verify_sources, *, only_index=None):
    """Solve individual cuts with their evidence. Whole-script constraints follow in _assemble."""
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
    continuity_groups = sentence_continuity.validate_plan(manifest, analysis, plan, narration_timing)
    output = []
    partial_preview = manifest.get("delivery_mode") == "preview" and manifest.get("allow_partial_selling_points") is True
    unresolved = []
    rejected = qc_rejected_shots(manifest)
    original = timing_contract.split_sentences(manifest)
    for sentence in sentences:
        index = sentence["index"]
        if only_index is not None and index != only_index:
            continue
        row = indexed[index]
        frozen = plan.get("_frozen_segments", {}).get(index)
        if frozen is not None:
            if contract.is_ai_segment(frozen):
                check = contract.validate_segment_receipt(frozen, index=index, manifest=manifest, verify_source=True)
                if not check["ok"]:
                    raise ValueError("AI_REFILL_FROZEN_INVALID:" + ";".join(check["reasons"]))
            elif not plan.get("_allow_frozen_rule_segments"):
                raise ValueError("AI_REFILL_FROZEN_INVALID:NON_AI_SEGMENT")
            output.append(copy.deepcopy(frozen))
            continue
        group = continuity_groups.get(row.get("continuity_key"))
        if contract.continuity.is_experiment(group):
            output.append(experiment_groups.assemble_member(manifest, plan, group, index, original[index - 1]))
            continue
        parts = row.get("parts")
        if not isinstance(parts, list) or not parts:
            raise ValueError(f"AI_EDITOR_NO_SELECTION:{index}")
        audio_us, certainty = timing_contract.duration_for(narration_timing, index, segment=original[index - 1])
        if any(type(p.get("audio_duration_us")) is not int or p["audio_duration_us"] <= 0 for p in parts) or sum(p["audio_duration_us"] for p in parts) != audio_us:
            raise ValueError(f"AI_EDITOR_AUDIO_ALLOCATION_INVALID:{index}")
        reviews = plan["reviews"].get(str(index), []) if sentence["kind"] == "selling_point" else plan["display_reviews"]
        good = {r["shot_id"]: r for r in reviews if r["verdict"] == "matched"}
        group = continuity_groups.get(row.get("continuity_key"))
        if group:
            # Joint pixels may establish a previously unreviewed member phase.
            # Keep that permission scoped to this validated complete group.
            member_review = next(r for r in group["review"]["members"] if r["index"] == index)
            good[group["input"]["shot_id"]] = {**member_review,
                "source_identity": group["input"]["source_identity"]}
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
        used_reviews = []
        for n, part in enumerate(parts):
            sid = part.get("shot_id")
            window_key = part.get("window_review_key")
            continuation = plan.get("window_reviews", {}).get(window_key) or {} if window_key else {}
            if (sid not in good and continuation.get("action_continuation") is True
                    and sentence.get("kind") in {"hook", "product_intro"} and not sentence.get("selling_points")
                    and continuation.get("shot_id") == sid and continuation.get("verdict") == "matched"
                    and sid in shots and sid not in rejected.get(index, set())):
                good = {**good, sid: continuation}
            if sid not in good or sid not in shots or sid in rejected.get(index, set()):
                raise ValueError(f"AI_EDITOR_UNREVIEWED_SELECTION:{index}")
            original_shot = shots[sid]
            review, shot = {**good[sid], "scarce_reuse_reason": reuse_reason}, original_shot
            window_key = part.get("window_review_key")
            if window_key:
                revised = plan.get("window_reviews", {}).get(window_key) or {}
                if (revised.get("sentence_index") != index or revised.get("shot_id") != sid
                        or revised.get("verdict") != "matched"
                        or revised.get("continuity_key") != row.get("continuity_key")
                        or revised.get("source_identity") != review["source_identity"]):
                    raise ValueError(f"AI_EDITOR_UNREVIEWED_SELECTION:{index}")
                window = revised["review_window_us"]
                if not round(float(original_shot["source_start"]) * 1e6) <= window["start"] < window["end"] <= round(float(original_shot["source_end"]) * 1e6):
                    raise ValueError(f"AI_EDITOR_UNREVIEWED_SELECTION:{index}")
                review = {**revised, "scarce_reuse_reason": reuse_reason}
                shot = {**shot, "source_start": window["start"] / 1e6, "source_end": window["end"] / 1e6}
            review = {**review, "preferred_speed": _scene_preference(manifest, sentence, review)}
            fit = _placement_fit_observation(review)
            if fit:
                review = {**review, "playback_observation": fit}
            if verify_sources and review["source_identity"] != contract.source_identity(shot["video"]):
                raise ValueError("AI_EDITOR_SOURCE_CONTENT_CHANGED")
            observed = _observed_shot(original_shot, review)
            observed["speed_range_override"] = list(shot_analyzer._fit_speed_range(
                observed, manifest=manifest, time_sensitive=review["time_sensitive"]))
            # A continuation requests a later in-point; it must retain every
            # evidence interval, otherwise the technical check rejects it.
            if "source_start_us" in part:
                if type(part["source_start_us"]) is not int:
                    raise ValueError("AI_EDITOR_INPOINT_INVALID")
                observed["source_start"] = max(float(observed["source_start"]), part["source_start_us"] / 1e6)
            cfg = {**manifest, "tail_pad_s": manifest.get("tail_pad_s", 0) if n == len(parts) - 1 else 0}
            if "source_start_us" in part or window_key:
                # The guard belongs to the original scene, not the requested in-point.
                guard = max(0.0, shot_analyzer._number(manifest.get("head_trim_s"), shot_analyzer.DEFAULT_HEAD_TRIM_S))
                offset = observed["source_start"] - float(original_shot["source_start"])
                cfg["head_trim_s"] = max(0.0, guard - offset)
                cfg["tail_trim_s"] = max(0.0, shot_analyzer._number(manifest.get("tail_trim_s"), shot_analyzer.DEFAULT_TAIL_TRIM_S)
                                          - (float(original_shot["source_end"]) - float(shot["source_end"])))
            group = continuity_groups.get(row.get("continuity_key"))
            if group:
                # The group solver already protects the original scene edges;
                # internal sentence boundaries are a continuous cut, not waste.
                cfg.update(head_trim_s=0, tail_trim_s=0)
            solution = shot_analyzer.candidate_window(observed, audio_duration_us=part["audio_duration_us"],
                manifest=cfg, time_sensitive=review["time_sensitive"])
            if not solution.ok:
                raise WindowFailure(index, sid, solution.reason, part_index=n)
            item = {**solution.to_dict(), "video": shot["video"], "shot_id": sid,
                    "temporary_shot_id": sid, "source_start": solution.source_start_us / 1e6,
                    "source_end": solution.source_end_us / 1e6, "duration": solution.material_us / 1e6,
                    "video_speed": solution.speed, "visual_role": observed["visual_role"],
                    "speed_range_override": observed["speed_range_override"],
                    "preferred_speed": observed["preferred_speed"],
                    "playback_observation": observed.get("playback_observation"),
                    "time_sensitive": review["time_sensitive"],
                    "scene_source_start": shot["source_start"], "scene_source_end": shot["source_end"],
                    "guard_source_start": original_shot["source_start"],
                    "guard_source_end": original_shot["source_end"],
                    "evidence_intervals": observed["evidence_intervals"], "text": sentence["text"]}
            if group:
                item.update(continuity_group=group, guard_override=[0, 0],
                            action_completeness_scope="continuous_sentence_group",
                            audio_duration_us=audio_us, selling_points=sentence["selling_points"])
            part_sentence = {**sentence, "selling_points": [p for p in sentence["selling_points"] if p["id"] in review.get("supported_point_ids", [])]}
            item["ai_selection"] = contract.make_receipt(item, index=index, sentence=part_sentence,
                observation=review, source_identity=review["source_identity"], identity=plan["identity"],
                task_speed_cap=contract.playback.task_absorb_speed_cap(manifest))
            assembled.append(item)
            used_reviews.append(review)
            supported.update(review.get("supported_point_ids", []))
        missing_points = [p for p in sentence["selling_points"] if p["id"] not in supported]
        if sentence["kind"] == "selling_point" and not contract.points_supported(
                {p["id"] for p in sentence["selling_points"]}, supported):
            raise ValueError(f"AI_EDITOR_POINTS_UNCOVERED:{index}")
        if missing_points:
            if partial_preview:
                unresolved.append({"index": index, "text": sentence["text"], "type": "PARTIAL_SELLING_POINTS",
                                   "missing_selling_points": missing_points})
        first = assembled[0]
        segment = {**original[index - 1], **first, "selection_mode": "ai_editor",
            "claim_text": sentence["text"], "sentence_kind": sentence["kind"],
            "selling_points": sentence["selling_points"], "audio_duration_us": audio_us,
            "timing_certainty": certainty, "window_solution": {k: v for k, v in first.items() if k in {"ok", "speed", "source_start", "source_end", "segment_s", "material_s", "handle_room_s", "reason", "detail"}},
            "action_complete": True, "has_subject": True, "persist_to_library": False,
            "visual_description": " / ".join(r["observation"] for r in used_reviews),
            "uncovered_selling_points": missing_points,
            "degraded_no_match": False, "formal_rejected": False,
            "multi_shot": len(assembled) > 1, "span_parts": assembled if len(assembled) > 1 else None}
        overall_review = {**used_reviews[0], "supported_point_ids": sorted(supported),
                          "scarce_reuse_reason": reuse_reason,
                          "time_sensitive": any(r["time_sensitive"] for r in used_reviews),
                          "observation": segment["visual_description"], "reason": row.get("reason") or used_reviews[0]["reason"]}
        segment["time_sensitive"] = overall_review["time_sensitive"]
        receipt_sentence = sentence  # Keep requirements even in an uncertified partial preview.
        segment["ai_selection"] = contract.make_receipt(segment, index=index, sentence=receipt_sentence,
            observation=overall_review, source_identity=good[first["shot_id"]]["source_identity"], identity=plan["identity"],
            task_speed_cap=contract.playback.task_absorb_speed_cap(manifest))
        check = contract.validate_segment_receipt(segment, index=index, manifest=manifest)
        if not check["ok"]:
            # A single-shot playback conflict belongs to this sentence. Keep
            # its identity so a separately reviewed continuity group is not
            # discarded when a later sentence needs its own timing repair.
            if check["reasons"] == ["PLAYBACK_SPEED_CONFLICT"] and len(parts) == 1:
                raise WindowFailure(index, first["shot_id"], "PLAYBACK_SPEED_CONFLICT")
            raise ValueError(";".join(check["reasons"]))
        output.append(segment)
    return output, unresolved


def _assemble(manifest: dict, analysis: dict, plan: dict, *, narration_timing: dict | None,
              verify_sources: bool = False, validate_sequence: bool = True) -> dict:
    output, unresolved = _assemble_segments(manifest, analysis, plan, narration_timing, verify_sources)
    sentences = plan["sentences"]
    for index, neighbor in _linked_hooks(plan):
        if not _touching(output, index, neighbor):
            raise ValueError(f"AI_EDITOR_NO_SELECTION:{index}")
    out_manifest = {**manifest, "selection_mode": "ai_editor", "segments": output,
                    "temporary_material_analysis": True,
                    "temporary_shot_ids": [s.get("temporary_shot_id") or s.get("shot_id") for s in output]}
    out_manifest["claim_gate_coverage"] = {"mode": "ai_editor", "certification": "UNCERTIFIED" if unresolved else "CERTIFIED",
        "point_coverage_policy": contract.POINT_COVERAGE_POLICY,
        "uncovered_selling_points": [{"index": i, "points": s.get("uncovered_selling_points")}
                                     for i, s in enumerate(output, 1) if s.get("uncovered_selling_points")],
        "validation_passed": not bool(unresolved), "uncertified_reasons": [f"PARTIAL_SELLING_POINTS:{x['index']}" for x in unresolved],
        "scope": "source_observation_only_independent_qc_required"}
    if unresolved:
        out_manifest["preview_unresolved"] = unresolved
    overlap = contract.sequence_errors(output, manifest=manifest,
        ai_only=bool(plan.get("_allow_frozen_rule_segments"))) if validate_sequence else []
    if overlap:
        raise ValueError(";".join(overlap))
    return {"ok": True, "selection_policy": "ai_editor_selling_points_first", "selection_mode": "ai_editor",
            "segments": output, "manifest": out_manifest, "pending_items": unresolved,
            "accepted_shots": [p.get("shot_id") or s.get("temporary_shot_id") for s in output for p in (s.get("span_parts") or [s])],
            "recommendations": [{"index": s["index"], "text": s["text"], "sentence_kind": s["kind"],
                "candidates": plan["reviews"].get(str(s["index"]), []) if s["kind"] == "selling_point" else plan["display_reviews"]} for s in sentences],
            "degraded_matches": [], "degraded_count": 0, "material_gaps": [], "material_gap_count": 0,
            "contract_issues": [], "claim_gate_coverage": out_manifest["claim_gate_coverage"]}


def _build_edit_inventory(manifest: dict, analysis: dict, plan: dict,
                          narration_timing: dict | None) -> list[dict]:
    from . import shot_analyzer
    original = timing_contract.split_sentences(manifest)
    shots = {str(s["shot_id"]): s for s in analysis.get("shots", [])}
    rejected = qc_rejected_shots(manifest)
    inventory = []
    history = _prior_use_by_shot(manifest, shots.values())
    source_ids = {path: f"source{i}" for i, path in enumerate(dict.fromkeys(
        shot["video"] for shot in shots.values()), 1)}
    for sentence in plan["sentences"]:
        reviews = (plan["reviews"].get(str(sentence["index"]), [])
                   if sentence["kind"] == "selling_point" else plan["display_reviews"])
        duration = timing_contract.duration_for(
            narration_timing, sentence["index"], segment=original[sentence["index"] - 1])[0]
        candidates = [{k: review[k] for k in ("shot_id", "basis", "observation", "reason", "confidence",
            "supported_point_ids", "evidence_intervals_us", "time_sensitive") if k in review}
            for review in reviews if review["verdict"] == "matched"
            and review["shot_id"] not in rejected.get(sentence["index"], set())]
        for candidate in candidates:
            review = next(r for r in reviews if r["shot_id"] == candidate["shot_id"])
            shot = shots[candidate["shot_id"]]
            review = {**review, "preferred_speed": _scene_preference(manifest, sentence, review)}
            solution = shot_analyzer.candidate_window(_observed_shot(shot, review),
                audio_duration_us=duration, manifest=manifest,
                time_sensitive=review["time_sensitive"])
            candidate["source_identity"] = source_ids[shot["video"]]
            if solution.ok:
                action = window_action(shot, solution.source_start_us / 1e6, solution.source_end_us / 1e6)
                if action:
                    candidate["window_action_hint"] = action
            source_count, scene_count = history[candidate["shot_id"]]
            candidate.update(prior_source_tasks=source_count, prior_scene_tasks=scene_count)
            candidate["technical_option"] = {**solution.to_dict(),
                "playback_policy_version": contract.playback.VERSION,
                "within_preferred_speed": solution.ok and solution.speed <= contract.playback.PROCESS_PREFERRED_MAX,
                "original_scene_start_us": round(float(shot["source_start"]) * 1e6),
                "original_scene_end_us": round(float(shot["source_end"]) * 1e6)}
            value = review.get("selling_point_value")
            # A broad reviewed scene cannot lend its value to a different crop.
            intervals = value.get("evidence_intervals_us") if isinstance(value, dict) else None
            allowed_ids = {p["id"] for p in sentence.get("selling_points", [])}
            if sentence.get("kind") != "selling_point":
                allowed_ids = {"general_display"}
            supported = value.get("supported_ids") if isinstance(value, dict) else None
            if (solution.ok and isinstance(value, dict) and type(value.get("strength")) is int
                    and 0 <= value["strength"] <= 3 and isinstance(supported, list)
                    and all(isinstance(i, str) for i in supported)
                    and set(supported) <= allowed_ids and isinstance(intervals, list) and bool(intervals)
                    and all(isinstance(e, dict) and type(e.get("start")) is int and type(e.get("end")) is int
                            and solution.source_start_us <= e["start"] < e["end"] <= solution.source_end_us
                            for e in intervals)):
                candidate["selling_point_value"] = value
            blind = review.get("shot_usability_observation")
            reviewed = review.get("review_window_us")
            if solution.ok and isinstance(blind, dict) and isinstance(reviewed, dict):
                from . import shot_usability
                try:
                    actual = {"start": solution.source_start_us, "end": solution.source_end_us}
                    if not (type(reviewed.get("start")) is int and type(reviewed.get("end")) is int
                            and reviewed["start"] <= actual["start"] < actual["end"] <= reviewed["end"]):
                        raise ValueError("SHOT_USABILITY_REVIEW_WINDOW_CHANGED")
                    candidate["usability"] = shot_usability.assess(blind,
                        {"display_goal": "general_product"}, actual, reviewed_window=reviewed, check_relation=False)
                except (ValueError, TypeError, KeyError, ZeroDivisionError):
                    pass
        from . import shot_value, shot_usability
        from .opening_hook import packaging_recall_priority
        def quality_rank(candidate):
            quality = candidate.get("usability") or {}
            status = quality.get("status")
            clarity = 0 if status == "USABLE_FOR_COMPARISON" else 2 if status == "REJECTED" else 1
            return (shot_value.rank({"plan": candidate}), clarity, shot_usability.rank(candidate)[1:],
                    not candidate["technical_option"].get("ok"),
                    packaging_recall_priority(candidate.get("observation") or "")
                    if sentence["kind"] in {"hook", "product_intro"} else 0,
                    candidate["prior_source_tasks"], candidate["prior_scene_tasks"])
        # Concurrent tasks can see the same empty ledger. Break equal-quality
        # ties reproducibly by task and sentence without changing the evidence.
        task = manifest.get("record_id") or manifest.get("ledger_task") or manifest.get("task_id")
        def editorial_rank(candidate):
            seed = (contract.digest([task, sentence["index"], shots[candidate["shot_id"]]["video"]])
                    if task else "")
            return (quality_rank(candidate), seed)
        candidates.sort(key=editorial_rank)
        for rank, candidate in enumerate(candidates, 1):
            candidate["editorial_order"] = rank
        inventory.append({"index": sentence["index"], "text": sentence["text"],
            "kind": sentence["kind"], "selling_points": sentence["selling_points"],
            "audio_duration_us": duration, "candidates": candidates,
            **({"hook_neighbor": sentence["hook_neighbor"]} if sentence.get("hook_neighbor") in ("next", "previous") else {})})
    return inventory


def _matched_parent_review(plan: dict, sentence: dict, shot_id: str) -> dict | None:
    """Return the sentence's already-matched observation for this shot, if any."""
    rows = (plan.get("reviews", {}).get(str(sentence["index"]), [])
            if sentence.get("kind") == "selling_point" else plan.get("display_reviews", []))
    required = {p["id"] for p in sentence.get("selling_points", [])}
    for row in rows:
        if (isinstance(row, dict) and row.get("shot_id") == shot_id and row.get("verdict") == "matched"
                and row.get("same_product") is True
                and contract.points_supported(required, set(row.get("supported_point_ids", [])))):
            return row
    return None


def _review_placement_window(manifest: dict, sentence: dict, shot: dict, directory: Path,
                             *, parent: dict) -> dict:
    """Judge only whether a crop is a complete, placeable clip.

    Selling-point support is already certified by ``parent``. This pass must not
    re-litigate instrument purpose, label claims, or other semantic gates.
    """
    media = _review_media(shot, directory, dense=True)
    start, end, before, times, sheet = (media[k] for k in ("start", "end", "before", "times", "sheet"))
    key = contract.digest(["placement", sentence["index"], shot["shot_id"], before, start, end,
                           parent.get("evidence_intervals_us")])[:24]
    durations = [v for v in (sentence.get("audio_durations_us") or []) if type(v) is int and v > 0]
    prompt = (
        contract.policy_prompt(manifest)
        + "\n这是放置短窗复核，不是重新选镜。附图只对应拟定裁切窗口；卖点已被更长观察确认。\n"
        + json.dumps({
            "product": manifest.get("product"),
            "category": manifest.get("category"),
            "sentence_index": sentence.get("index"),
            "sentence_text": sentence.get("text"),
            "shot_id": shot["shot_id"],
            "audio_durations_us": durations,
            "scene_us": {"start": round(start * 1e6), "end": round(end * 1e6)},
            "certified_selling_point": {
                "supported_point_ids": parent.get("supported_point_ids"),
                "basis": parent.get("basis"),
                "observation": parent.get("observation"),
                "evidence_intervals_us": parent.get("evidence_intervals_us"),
            },
        }, ensure_ascii=False)
        + """
只判断这个裁切窗口能不能作为一句完整可放入的镜头，不要重新判定卖点是否成立。
禁止以“仪器用途不确定”“标称是否证明卖点”“是否荧光检测”等理由否定；那些已由 certified_selling_point 确认。
输出JSON：{"verdict":"suitable|unsuitable|uncertain","same_product":true,
"observation":"裁切内实际可见的完整片段事实","reason":"为什么这段裁切完整合适或不合适",
"confidence":0.9,"evidence_intervals_us":[{"start":1000000,"end":1800000}],
"playback_observation":null}。
suitable=裁切内是一段完整、未拦腰切断关键动作/结果的可用镜头；unsuitable=明显切在动作中途或关键结果被裁掉；
裁切里的空等、机位调整、拿错重来或长时间遮挡挤掉了有效展示时也判unsuitable。稳定展示结果、必要的入画或揭起不属于废料。
看不清完整性时uncertain。evidence_intervals_us必须落在scene_us内，选最短但完整的一段，优先能放进audio_durations_us。
可选playback_observation仅当整段确认为纯过程窗口；否则省略。未标纯过程时，程序仍可在本裁切内用不超过2倍的速度罩住证据。
"""
    )
    result = _call(prompt, directory / "reviews" / f"placement_{key}.json", sheet)
    verdict = result.get("verdict")
    if verdict not in {"suitable", "unsuitable", "uncertain"} or type(result.get("same_product")) is not bool:
        raise ValueError("AI_EDITOR_PLACEMENT_REVIEW_INVALID")
    confidence = result.get("confidence")
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ValueError("AI_EDITOR_PLACEMENT_REVIEW_CONFIDENCE_INVALID")
    if not all(isinstance(result.get(k), str) and result[k].strip() for k in ("observation", "reason")):
        raise ValueError("AI_EDITOR_PLACEMENT_REVIEW_OBSERVATION_MISSING")
    if not result["same_product"] or confidence < .6:
        verdict = "uncertain" if result["same_product"] else "unsuitable"
    intervals = result.get("evidence_intervals_us")
    if (isinstance(intervals, list) and len(intervals) == 2
            and all(type(value) is int for value in intervals)):
        intervals = [{"start": intervals[0], "end": intervals[1]}]
        result["evidence_intervals_us"] = intervals
    if verdict == "suitable":
        if not isinstance(intervals, list) or not intervals:
            raise ValueError("AI_EDITOR_PLACEMENT_EVIDENCE_INVALID")
        lo_us, hi_us = round(start * 1e6), round(end * 1e6)
        frame_us, clamps = None, []
        for ev in intervals:
            if not isinstance(ev, dict) or any(type(ev.get(k)) is not int for k in ("start", "end")):
                raise ValueError("AI_EDITOR_PLACEMENT_EVIDENCE_INVALID")
            s, e = ev["start"], ev["end"]
            if s < lo_us or e > hi_us:
                # A boundary at most one source frame outside the reviewed crop
                # names the crop's own edge frame; clamp it. Larger overshoot
                # means the action continues past the crop: still rejected.
                frame_us = frame_us or _source_frame_us(shot)
                if lo_us - frame_us <= s < lo_us:
                    s = lo_us
                if hi_us < e <= hi_us + frame_us:
                    e = hi_us
            if not lo_us <= s < e <= hi_us:
                raise ValueError("AI_EDITOR_PLACEMENT_EVIDENCE_INVALID")
            if (s, e) != (ev["start"], ev["end"]):
                clamps.append({"from": {"start": ev["start"], "end": ev["end"]},
                               "to": {"start": s, "end": e}, "frame_us": frame_us})
                ev["start"], ev["end"] = s, e
        if clamps:
            _save(directory / "reviews" / f"placement_{key}.clamp.json",
                  {"review_window_us": {"start": lo_us, "end": hi_us}, "clamps": clamps})
        playback_observation = result.get("playback_observation")
        if playback_observation is not None:
            process = contract.playback.process_window(playback_observation)
            if process is None or not round(start * 1e6) <= process[0] < process[1] <= round(end * 1e6):
                raise ValueError("AI_EDITOR_PLAYBACK_OBSERVATION_INVALID")
    mapped = "matched" if verdict == "suitable" else ("mismatch" if verdict == "unsuitable" else "uncertain")
    # Inherit certified selling-point identity; placement must not rewrite it.
    return {
        **result,
        "verdict": mapped,
        "placement_review": True,
        "placement_verdict": verdict,
        "same_product": True if mapped == "matched" else result["same_product"],
        "basis": parent.get("basis"),
        "supported_point_ids": list(parent.get("supported_point_ids") or []),
        "time_sensitive": bool(parent.get("time_sensitive")),
        "scene_ids": list(parent.get("scene_ids") or []),
        "preferred_speed": parent.get("preferred_speed", 1.0),
        "parent_evidence_intervals_us": copy.deepcopy(parent.get("evidence_intervals_us") or []),
        "dense_required": False,
        "review_window_us": {"start": round(start * 1e6), "end": round(end * 1e6)},
        "shot_id": shot["shot_id"],
        "source_identity": before,
        "frame_times_us": [round(t * 1e6) for t in times],
        "sheet": str(sheet),
    }


def _review_proposed_window(manifest, plan, directory, sentence, shot, window, *, attempt_metadata=None):
    """Spend the persistent sentence budget only on new, actual window pixels."""
    index = sentence["index"]
    limit = shot.get("material_duration_us")
    if type(limit) is int and limit > 0 and window[1] > limit:
        return None
    source = contract.source_identity(shot["video"])
    # Display and hook cuts still need a dense look at the new pixels. Placement
    # review only inherits a selling point that was already matched.
    parent = (_matched_parent_review(plan, sentence, shot["shot_id"])
              if sentence.get("kind") == "selling_point" else None)
    key = contract.digest([
        "placement" if parent else "semantic", sentence, shot["shot_id"], source, window,
        (parent or {}).get("evidence_intervals_us")])[:24]
    attempts = plan.setdefault("window_repair_attempts", {})
    if key in attempts:
        # A process may stop after saving a valid review but before attaching
        # it to the selected part. Resume only the exact persisted evidence.
        attempt = attempts[key]
        saved = plan.get("window_reviews", {}).get(key) or {}
        if not isinstance(attempt, dict):
            return None
        # Transient vision outages must not permanently burn this crop key.
        if (attempt.get("status") == "failed"
                and failure_fields(str(attempt.get("reason") or "")).get("retryable")):
            del attempts[key]
        else:
            if not isinstance(saved, dict):
                return None
            intervals = saved.get("evidence_intervals_us")
            confidence = saved.get("confidence")
            supported = saved.get("supported_point_ids")
            allowed = {p["id"] for p in sentence["selling_points"]}
            playback = saved.get("playback_observation")
            process = contract.playback.process_window(playback) if playback is not None else None
            parent_ok = (not parent) or (
                saved.get("placement_review") is True
                and saved.get("supported_point_ids") == list(parent.get("supported_point_ids") or [])
                and saved.get("basis") == parent.get("basis")
                and saved.get("time_sensitive") == bool(parent.get("time_sensitive")))
            if (attempt.get("status") == "reviewed" and attempt.get("index") == index
                    and attempt.get("shot_id") == shot["shot_id"]
                    and attempt.get("window_us") == {"start": window[0], "end": window[1]}
                    and saved.get("sentence_index") == index and saved.get("shot_id") == shot["shot_id"]
                    and saved.get("verdict") == "matched" and saved.get("same_product") is True
                    and saved.get("source_identity") == source and not saved.get("continuity_key")
                    and saved.get("review_window_us") == {"start": window[0], "end": window[1]}
                    and isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                    and math.isfinite(confidence) and .6 <= confidence <= 1
                    and saved.get("basis") in contract.BASES and type(saved.get("time_sensitive")) is bool
                    and all(isinstance(saved.get(k), str) and saved[k].strip() for k in ("observation", "reason"))
                    and isinstance(intervals, list) and intervals
                    and all(isinstance(ev, dict) and type(ev.get("start")) is int and type(ev.get("end")) is int
                            and window[0] <= ev["start"] < ev["end"] <= window[1] for ev in intervals)
                    and (playback is None or (process is not None and window[0] <= process[0] < process[1] <= window[1]))
                    and isinstance(supported, list) and all(isinstance(p, str) and p in allowed for p in supported)
                    and contract.points_supported(allowed, set(supported))
                    and (not sentence.get("hook_neighbor_context") or saved.get("hook_link_supported") is True)
                    and parent_ok):
                return key
            return None
    continuation = sentence.get("action_continuation") is True
    prior = [v for v in attempts.values() if isinstance(v, dict) and v.get("index") == index]
    # Failed crops of this sentence's own shot do not spend the one look at the previous action.
    if continuation:
        mode = "hook_neighbor" if sentence.get("hook_neighbor_context") else "continuation"
        if any(v.get("mode") == mode for v in prior):
            return None
    elif len(prior) >= 3:
        return None
    attempts[key] = {"index": index, "shot_id": shot["shot_id"],
                     "window_us": {"start": window[0], "end": window[1]}, "status": "reviewing",
                     "mode": mode if continuation else ("placement" if parent else "semantic")}
    if attempt_metadata:
        attempts[key].update(attempt_metadata)
    _save(directory / "plan.json", plan)
    cropped = {**shot, "source_start": window[0] / 1e6, "source_end": window[1] / 1e6}
    try:
        if parent:
            reviewed = _review_placement_window(manifest, sentence, cropped, directory, parent=parent)
        else:
            reviewed = _review(manifest, sentence, cropped, directory, dense=True)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        attempts[key].update(status="failed", reason=str(exc))
        _save(directory / "plan.json", plan)
        if failure_fields(str(exc)):
            raise
        return None
    if (reviewed.get("verdict") != "matched" or reviewed.get("same_product") is not True
            or reviewed.get("source_identity") != source
            or reviewed.get("review_window_us") != {"start": window[0], "end": window[1]}
            or not contract.points_supported({p["id"] for p in sentence["selling_points"]},
                                             set(reviewed.get("supported_point_ids", [])))):
        attempts[key].update(status="rejected", reason=reviewed.get("reason", "WINDOW_NOT_SUPPORTED"))
        _save(directory / "plan.json", plan)
        return None
    plan.setdefault("window_reviews", {})[key] = {**reviewed, "sentence_index": index}
    attempts[key]["status"] = "reviewed"
    _save(directory / "plan.json", plan)
    return key


def _repair_selected_window(manifest: dict, analysis: dict, plan: dict, directory: Path,
                            failure: WindowFailure) -> bool:
    sentence = next(s for s in plan["sentences"] if s["index"] == failure.index)
    row = next(r for r in plan["rows"] if r["index"] == failure.index)
    parts = row["parts"]
    if failure.part_index is not None:
        part = (parts[failure.part_index] if 0 <= failure.part_index < len(parts)
                and parts[failure.part_index].get("shot_id") == failure.shot_id else None)
    else:
        matching = [p for p in parts if p.get("shot_id") == failure.shot_id]
        part = matching[0] if len(matching) == 1 else None
    if part is None:
        return False
    shot = next(s for s in analysis["shots"] if s["shot_id"] == failure.shot_id)
    proposal = dict(part or {})
    reviews = (plan["reviews"].get(str(failure.index), [])
               if sentence["kind"] == "selling_point" else plan["display_reviews"])
    review = next((r for r in reviews if r.get("shot_id") == failure.shot_id
                   and r.get("verdict") == "matched"), {})
    intervals = review.get("evidence_intervals_us") or []
    adjusted_start = False
    if intervals:
        start = proposal.get("source_start_us", min(ev["start"] for ev in intervals))
        physical_end = shot.get("material_duration_us")
        scene_end = round(float(shot["source_end"]) * 1e6)
        duration = proposal.get("audio_duration_us")
        if (type(start) is int and type(physical_end) is int and 0 < physical_end < scene_end
                and type(duration) is int and duration > 0
                and max(ev["end"] for ev in intervals) > physical_end
                and start + duration > physical_end):
            # Move the proposed window only for a fresh pixel review. The old
            # evidence is never shortened or relabelled as a new observation.
            scene_start = round(float(shot["source_start"]) * 1e6)
            start = max(scene_start, physical_end - duration)
            adjusted_start = True
        proposal["source_start_us"] = start
    window = proposed_window(shot, proposal, max_speed=contract.playback.PROCESS_PREFERRED_MAX)
    if window is None:
        return False
    metadata = {"original_source_start_us": part.get("source_start_us")} if adjusted_start else None
    key = _review_proposed_window(manifest, plan, directory, sentence, shot, window, attempt_metadata=metadata)
    if key is None:
        return False
    part["window_review_key"] = key
    if adjusted_start:
        part["source_start_us"] = window[0]
    _save(directory / "plan.json", plan)
    return True


def _fabric_comfort_sentence(sentence: dict) -> bool:
    """面料手感句可以换一条不同的抚摸镜头；吸收、防漏、干爽不走这条裁切。"""
    if sentence.get("kind") != "selling_point":
        return False
    text = str(sentence.get("text") or "")
    for point in sentence.get("selling_points") or []:
        if isinstance(point, dict):
            text += str(point.get("text") or "")
    if re.search(r"吸收|瞬吸|防漏|反渗|干爽|透气", text):
        return False
    return re.search(r"柔软|舒适|面层", text) is not None


def _single_part_end(manifest, analysis, plan, narration_timing, index):
    """Solve one already selected part. Returns the shot and where that cut ends."""
    from . import shot_analyzer
    sentence = next(s for s in plan["sentences"] if s["index"] == index)
    row = next(r for r in plan["rows"] if r["index"] == index)
    parts = row.get("parts") or []
    if row.get("continuity_key") or len(parts) != 1 or not isinstance(parts[0], dict):
        return None
    part = parts[0]
    sid = part.get("shot_id")
    shot = next((s for s in analysis["shots"] if s["shot_id"] == sid), None)
    if shot is None or type(part.get("audio_duration_us")) is not int:
        return None
    original_shot = shot
    reviews = (plan.get("reviews", {}).get(str(index), []) if sentence.get("kind") == "selling_point"
               else plan.get("display_reviews", []))
    review = next((r for r in reviews if isinstance(r, dict) and r.get("shot_id") == sid
                   and r.get("verdict") == "matched"), None)
    window_key = part.get("window_review_key")
    if window_key:
        revised = plan.get("window_reviews", {}).get(window_key) or {}
        if revised.get("verdict") != "matched" or revised.get("shot_id") != sid:
            return None
        review = revised
        window = revised.get("review_window_us") or {}
        if type(window.get("start")) is not int or type(window.get("end")) is not int:
            return None
        shot = {**shot, "source_start": window["start"] / 1e6, "source_end": window["end"] / 1e6}
    if review is None:
        return None
    observed = _observed_shot(original_shot, review)
    if "source_start_us" in part:
        if type(part["source_start_us"]) is not int:
            return None
        observed["source_start"] = max(float(observed["source_start"]), part["source_start_us"] / 1e6)
    cfg = dict(manifest)
    if "source_start_us" in part or window_key:
        guard = max(0.0, shot_analyzer._number(manifest.get("head_trim_s"), shot_analyzer.DEFAULT_HEAD_TRIM_S))
        offset = observed["source_start"] - float(original_shot["source_start"])
        cfg["head_trim_s"] = max(0.0, guard - offset)
        cfg["tail_trim_s"] = max(0.0, shot_analyzer._number(manifest.get("tail_trim_s"), shot_analyzer.DEFAULT_TAIL_TRIM_S)
                                  - (float(original_shot["source_end"]) - float(shot["source_end"])))
    solution = shot_analyzer.candidate_window(
        observed, audio_duration_us=part["audio_duration_us"], manifest=cfg,
        time_sensitive=bool(review.get("time_sensitive")))
    if not solution.ok:
        return None
    return original_shot, solution.source_end_us


def _grouped_cut_end(analysis, plan, index):
    """Source end of the last sentence in a continuity group.

    A grouped sentence has no single solved part. The tail after that group's
    final cut can still carry a following line that has no selling point.
    Footage already given to a later member of the same group is not free.
    """
    row = next((r for r in plan.get("rows") or [] if isinstance(r, dict) and r.get("index") == index), None)
    key = (row or {}).get("continuity_key")
    group = (plan.get("continuity_reviews") or {}).get(key) if key else None
    if not isinstance(group, dict):
        return None
    context = group.get("input") or {}
    members = context.get("members") or []
    if not members or not isinstance(members[-1], dict) or members[-1].get("index") != index:
        return None
    end = members[-1].get("end_us")
    sid = context.get("shot_id")
    parts = row.get("parts") or []
    shot = next((s for s in analysis.get("shots") or [] if s.get("shot_id") == sid), None)
    if (shot is None or type(end) is not int or end < 0 or len(parts) != 1
            or parts[0].get("shot_id") != sid):
        return None
    return shot, end


def _extend_previous_action(manifest, analysis, plan, directory, failure, narration_timing) -> bool:
    """A short line with no selling point may keep playing the previous action.

    The extra picture has to already be in that shot, at the previous cut, and
    long enough at original speed. A pixel review still has to accept the tail.
    The previous sentence may be the last line of a continuity group; the cut
    is that group's final source end, not a new slice inside the group.
    """
    if not isinstance(failure, WindowFailure):
        return False
    sentence = next((s for s in plan.get("sentences", []) if s.get("index") == failure.index), None)
    if (sentence is None or sentence.get("kind") not in {"hook", "product_intro"}
            or sentence.get("selling_points") or failure.index < 2):
        return False
    solved = _single_part_end(manifest, analysis, plan, narration_timing, failure.index - 1)
    if solved is None:
        solved = _grouped_cut_end(analysis, plan, failure.index - 1)
    if solved is None:
        return False
    shot, prev_end = solved
    from . import shot_analyzer
    original = timing_contract.split_sentences(manifest)
    audio, certainty = timing_contract.duration_for(
        narration_timing, failure.index, segment=original[failure.index - 1])
    if certainty == timing_contract.TIMING_CERTAINTY_ESTIMATED or audio <= 0:
        return False
    scene_end = round(float(shot["source_end"]) * 1e6)
    limit = shot.get("material_duration_us")
    if type(limit) is int and limit > 0:
        scene_end = min(scene_end, limit)
    hi = scene_end - timing_contract.us_of(
        manifest.get("tail_trim_s"), round(shot_analyzer.DEFAULT_TAIL_TRIM_S * 1e6))
    required = audio + timing_contract.us_of(manifest.get("tail_pad_s"), 0)
    if hi - prev_end < required:
        return False
    window = (prev_end, prev_end + required)
    key = _review_proposed_window(
        manifest, plan, directory, {**sentence, "action_continuation": True}, shot, window)
    if key is None:
        return False
    plan["window_reviews"][key]["action_continuation"] = True
    candidate = {"index": failure.index, "parts": [{
        "shot_id": shot["shot_id"], "audio_duration_us": audio,
        "source_start_us": prev_end, "window_review_key": key}],
        "reason": "无卖点短句顺延上一镜还没播完的动作"}
    trial = copy.deepcopy(plan)
    trial["rows"] = [candidate if r["index"] == failure.index else r for r in trial["rows"]]
    try:
        _assemble(manifest, analysis, trial, narration_timing=narration_timing, verify_sources=True)
    except WindowFailure as exc:
        if exc.index <= failure.index:
            return False
    except (ValueError, KeyError, TypeError, OSError):
        return False
    row = next(r for r in plan["rows"] if r["index"] == failure.index)
    row.clear()
    row.update(candidate)
    _save(directory / "plan.json", plan)
    return True


def _repair_display_duplicate_window(manifest, analysis, plan, directory, narration_timing, index, baseline, before):
    """New original-speed display pixels may resolve an otherwise repeated cut."""
    from . import shot_analyzer
    sentence = next(s for s in plan["sentences"] if s["index"] == index)
    if sentence["kind"] not in {"hook", "product_intro"} or sentence.get("selling_points"):
        return False
    row = next(r for r in plan["rows"] if r["index"] == index)
    def accept(candidate):
        trial = copy.deepcopy(plan)
        trial["rows"] = [candidate if r["index"] == index else r for r in trial["rows"]]
        try:
            checked = _assemble(manifest, analysis, trial, narration_timing=narration_timing,
                                verify_sources=True, validate_sequence=False)
            if any(p["video_speed"] != 1 for p in checked["segments"][index - 1].get("span_parts") or [checked["segments"][index - 1]]):
                return False
            if not set(contract.sequence_conflict_details(checked["segments"])) < before:
                return False
        except (ValueError, KeyError, OSError, TypeError):
            return False
        row.clear()
        row.update(copy.deepcopy(candidate))
        _save(directory / "plan.json", plan)
        return True
    for candidate in _short_complete_rows(manifest, analysis, plan, index, narration_timing):
        if accept(candidate):
            return True
    attempts = plan.setdefault("window_repair_attempts", {})
    if sum(r.get("index") == index for r in attempts.values()) >= 3:
        return False
    shots = {s["shot_id"]: s for s in analysis["shots"]}
    excluded = qc_rejected_shots(manifest).get(index, set())
    used = {}
    for i, segment in enumerate(baseline["segments"], 1):
        if i == index:
            continue
        for part in segment.get("span_parts") or [segment]:
            source_key = os.path.normcase(contract.source_identity(part["video"])["path"])
            used.setdefault(source_key, []).append((round(part["source_start"] * 1e6), round(part["source_end"] * 1e6)))
    reviews = [r for r in plan.get("display_reviews", []) if r.get("verdict") == "matched"
        and r.get("same_product") is True and r.get("shot_id") in shots and r["shot_id"] not in excluded]
    reviews.sort(key=lambda r: (os.path.normcase((r.get("source_identity") or {}).get("path", "")) in used,
        r.get("basis") not in {"product_display", "readable_label"},
        max(e["end"] for e in r["evidence_intervals_us"]) - min(e["start"] for e in r["evidence_intervals_us"]), r["shot_id"]))
    duration = row["parts"][0]["audio_duration_us"] + timing_contract.us_of(manifest.get("tail_pad_s"), 0)
    seen_sources = set()
    for review in reviews:
        shot = shots[review["shot_id"]]
        source = contract.source_identity(shot["video"])
        source_key = os.path.normcase(source["path"])
        if source_key in seen_sources or review.get("source_identity") != source:
            continue
        lo = round(shot["source_start"] * 1e6) + timing_contract.us_of(
            manifest.get("head_trim_s"), round(shot_analyzer.DEFAULT_HEAD_TRIM_S * 1e6))
        scene_end = round(shot["source_end"] * 1e6)
        limit = shot.get("material_duration_us")
        if type(limit) is int and limit > 0:
            scene_end = min(scene_end, limit)
        hi = scene_end - timing_contract.us_of(
            manifest.get("tail_trim_s"), round(shot_analyzer.DEFAULT_TAIL_TRIM_S * 1e6))
        if hi - lo < duration:
            continue
        evlo = min(e["start"] for e in review["evidence_intervals_us"])
        evhi = max(e["end"] for e in review["evidence_intervals_us"])
        starts = [evhi - duration, (evlo + evhi - duration) // 2, evlo]
        starts += [end for _start, end in used.get(source_key, [])]
        for start in dict.fromkeys(max(lo, min(start, hi - duration)) for start in starts):
            window = (start, start + duration)
            if any(min(window[1], end) - max(window[0], begin) > 40_000 for begin, end in used.get(source_key, [])):
                continue
            checked_before = len(attempts)
            key = _review_proposed_window(manifest, plan, directory, sentence, shot, window)
            if len(attempts) > checked_before:
                seen_sources.add(source_key)
            if key is None:
                if len(attempts) > checked_before:
                    break  # Diversify sources after a real negative observation.
                continue
            candidate = {"index": index, "parts": [{"shot_id": shot["shot_id"],
                "audio_duration_us": row["parts"][0]["audio_duration_us"],
                "source_start_us": start, "window_review_key": key}],
                "reason": "新原速展示短窗经真实像素复核后消除重复区间。"}
            if accept(candidate):
                return True
            break
        if sum(r.get("index") == index for r in attempts.values()) >= 3:
            return False
    return False


def _repair_comfort_duplicate_window(manifest, analysis, plan, directory, narration_timing, index, baseline, before):
    """Two fabric-feel lines may each keep a different already-seen touch."""
    sentence = next(s for s in plan["sentences"] if s["index"] == index)
    if not _fabric_comfort_sentence(sentence):
        return False
    row = next(r for r in plan["rows"] if r["index"] == index)
    current = row["parts"][0].get("shot_id")
    def accept(candidate):
        trial = copy.deepcopy(plan)
        trial["rows"] = [candidate if r["index"] == index else r for r in trial["rows"]]
        try:
            checked = _assemble(manifest, analysis, trial, narration_timing=narration_timing,
                                verify_sources=True, validate_sequence=False)
            parts = checked["segments"][index - 1].get("span_parts") or [checked["segments"][index - 1]]
            if any(p["video_speed"] != 1 for p in parts):
                return False
            if not set(contract.sequence_conflict_details(checked["segments"])) < before:
                return False
        except (ValueError, KeyError, OSError, TypeError):
            return False
        row.clear()
        row.update(copy.deepcopy(candidate))
        _save(directory / "plan.json", plan)
        return True
    shots = {s["shot_id"]: s for s in analysis["shots"]}
    excluded = qc_rejected_shots(manifest).get(index, set())
    used = {}
    for i, segment in enumerate(baseline["segments"], 1):
        if i == index:
            continue
        for part in segment.get("span_parts") or [segment]:
            source_key = os.path.normcase(contract.source_identity(part["video"])["path"])
            used.setdefault(source_key, []).append((round(part["source_start"] * 1e6), round(part["source_end"] * 1e6)))
    from . import shot_analyzer
    duration = row["parts"][0]["audio_duration_us"] + timing_contract.us_of(manifest.get("tail_pad_s"), 0)
    reviews = [r for r in plan.get("reviews", {}).get(str(index), [])
               if isinstance(r, dict) and r.get("verdict") == "matched" and r.get("same_product") is True
               and r.get("time_sensitive") is not True and r.get("shot_id") not in {current, *excluded}
               and r.get("shot_id") in shots]
    for review in reviews:
        shot = shots[review["shot_id"]]
        source = contract.source_identity(shot["video"])
        if review.get("source_identity") != source:
            continue
        intervals = review.get("evidence_intervals_us") or []
        if not intervals:
            continue
        evlo = min(e["start"] for e in intervals)
        evhi = max(e["end"] for e in intervals)
        if evhi - evlo <= duration:
            continue
        lo = round(shot["source_start"] * 1e6) + timing_contract.us_of(
            manifest.get("head_trim_s"), round(shot_analyzer.DEFAULT_HEAD_TRIM_S * 1e6))
        scene_end = round(shot["source_end"] * 1e6)
        limit = shot.get("material_duration_us")
        if type(limit) is int and limit > 0:
            scene_end = min(scene_end, limit)
        hi = scene_end - timing_contract.us_of(
            manifest.get("tail_trim_s"), round(shot_analyzer.DEFAULT_TAIL_TRIM_S * 1e6))
        start = max(lo, evlo)
        if start + duration > min(hi, evhi):
            continue
        window = (start, start + duration)
        source_key = os.path.normcase(source["path"])
        if any(min(window[1], end) - max(window[0], begin) > 40_000 for begin, end in used.get(source_key, [])):
            continue
        key = _review_proposed_window(manifest, plan, directory, sentence, shot, window)
        if key is None:
            if sum(r.get("index") == index for r in plan.get("window_repair_attempts", {}).values()) >= 3:
                return False
            continue
        candidate = {"index": index, "parts": [{"shot_id": shot["shot_id"],
            "audio_duration_us": row["parts"][0]["audio_duration_us"],
            "source_start_us": start, "window_review_key": key}],
            "reason": "面料舒适改用另一条已经看过的抚摸镜头。"}
        if accept(candidate):
            return True
    return False


def _repair_distinct_window(manifest, analysis, plan, directory, narration_timing, index, baseline, before):
    if _repair_display_duplicate_window(manifest, analysis, plan, directory, narration_timing, index, baseline, before):
        return True
    return _repair_comfort_duplicate_window(manifest, analysis, plan, directory, narration_timing, index, baseline, before)


def _repair_repeated_window(manifest: dict, analysis: dict, plan: dict, directory: Path,
                            narration_timing: dict | None, index: int, expanded: set[int]) -> bool:
    """Try already pixel-reviewed single-shot alternatives; add at most one review round."""
    row = next((r for r in plan.get("rows", []) if isinstance(r, dict) and r.get("index") == index), None)
    sentence = next((s for s in plan.get("sentences", []) if s.get("index") == index), None)
    if not row or row.get("continuity_key") or not sentence or len(row.get("parts") or []) != 1:
        return False
    part = row["parts"][0]
    if not isinstance(part, dict) or type(part.get("audio_duration_us")) is not int:
        return False

    try:
        baseline = _assemble(manifest, analysis, plan, narration_timing=narration_timing,
                             verify_sources=True, validate_sequence=False)
        before = set(contract.sequence_conflict_details(baseline["segments"],
            source_reuse_limit=contract.source_reuse_limit(manifest)))
    except (ValueError, KeyError, TypeError, OSError):
        return False
    if not before:
        return False

    def try_reviewed():
        inventory = next((item for item in _build_edit_inventory(manifest, analysis, plan, narration_timing)
                          if item["index"] == index), None)
        for candidate in (inventory or {}).get("candidates", []):
            if not candidate.get("technical_option", {}).get("ok"):
                continue
            proposal = copy.deepcopy(plan)
            target = next(r for r in proposal["rows"] if r.get("index") == index)
            target.pop("scarce_reuse_reason", None)
            target["parts"] = [{"shot_id": candidate["shot_id"],
                                "audio_duration_us": part["audio_duration_us"]}]
            target["reason"] = "已复核候选替换重复源区间，整稿窗口与证据重新校验。"
            try:
                checked = _assemble(manifest, analysis, proposal, narration_timing=narration_timing,
                                    verify_sources=True, validate_sequence=False)
                after = set(contract.sequence_conflict_details(checked["segments"],
                    source_reuse_limit=contract.source_reuse_limit(manifest)))
                reuse = lambda details: {d for d in details if d[0].startswith("AI_EDITOR_SOURCE_REUSE_LIMIT:")}
                improved_reuse = (len(reuse(after)) < len(reuse(before))
                    and (after - reuse(after)) <= (before - reuse(before)))
                if not (after < before or improved_reuse):
                    continue
            except (ValueError, KeyError, TypeError, OSError):
                continue
            row.clear()
            row.update(target)
            return True
        return False

    if try_reviewed():
        return True
    # A different window in the same file cannot repair a per-source limit.
    reuse_failure = any(d[0].startswith("AI_EDITOR_SOURCE_REUSE_LIMIT:") and
        index in contract.source_reuse_indices(baseline["segments"], d[1]) for d in before)
    if any(r.get("index") == index and r.get("reviewed_candidates") for r in plan.get("duplicate_repair_reviews", [])):
        expanded.add(index)
    if index in expanded:
        return False if reuse_failure else _repair_distinct_window(manifest, analysis, plan, directory, narration_timing, index, baseline, before)
    expanded.add(index)
    display = sentence["kind"] != "selling_point"
    reviews = plan.get("display_reviews", []) if display else plan.get("reviews", {}).get(str(index), [])
    seen = {r.get("shot_id") for r in reviews if isinstance(r, dict)}
    all_ids = list(dict.fromkeys(sentence.get("candidate_ids") or []))
    new_ids = [sid for sid in all_ids if sid not in seen][:ROUND_SIZE]
    if not new_ids:
        return False if reuse_failure else _repair_distinct_window(manifest, analysis, plan, directory, narration_timing, index, baseline, before)
    # Keep all previous observations, and cap newly reviewed candidates to one round.
    retained = [r for r in reviews if isinstance(r, dict)
                and not failure_fields(str(r.get("error", ""))).get("retryable")]
    target_sentence = {**sentence, "candidate_ids": new_ids,
                       "audio_durations_us": [part["audio_duration_us"]]}
    if display:
        target_sentence.update(index=0, text="同商品通用产品展示池", kind="product_intro", selling_points=[])
    shots = {str(shot["shot_id"]): shot for shot in analysis.get("shots", [])}
    fresh, _coverage = _review_pool(manifest, target_sentence, shots, directory,
                                     narration_timing or {}, existing_reviews=[],
                                     excluded_ids=qc_rejected_shots(manifest).get(index, set()))
    historical_failures = [r for r in reviews if isinstance(r, dict)
                           and failure_fields(str(r.get("error", ""))).get("retryable")]
    result = retained + fresh + historical_failures
    if display:
        plan["display_reviews"] = result
    else:
        plan.setdefault("reviews", {})[str(index)] = result
    plan.setdefault("duplicate_repair_reviews", []).append({"index": index, "new_candidates": new_ids,
        "reviewed_candidates": [r["shot_id"] for r in fresh]})
    _save(directory / "plan.json", plan)
    return try_reviewed() or (not reuse_failure and _repair_distinct_window(
        manifest, analysis, plan, directory, narration_timing, index, baseline, before))


def _short_complete_rows(manifest, analysis, plan, index, narration_timing):
    """Only already reviewed evidence may replace an infeasible selected row."""
    from . import shot_analyzer
    sentence = next(s for s in plan["sentences"] if s["index"] == index)
    reviews = (plan["reviews"].get(str(index), []) if sentence["kind"] == "selling_point"
               else plan["display_reviews"])
    shots = {s["shot_id"]: s for s in analysis["shots"]}
    original = timing_contract.split_sentences(manifest)
    duration = timing_contract.duration_for(narration_timing, index, segment=original[index - 1])[0]
    required = {p["id"] for p in sentence["selling_points"]}
    candidates = []
    variants = [(r, "") for r in reviews] + [(r, key) for key, r in plan.get("window_reviews", {}).items()
        if r.get("sentence_index") == index and not r.get("continuity_key")]
    for review, window_key in variants:
        if (review.get("verdict") != "matched" or review.get("shot_id") not in shots
                or review.get("shot_id") in qc_rejected_shots(manifest).get(index, set())
                or not contract.points_supported(required, set(review.get("supported_point_ids", [])))):
            continue
        shot = shots[review["shot_id"]]
        try:
            current_source = contract.source_identity(shot["video"])
        except OSError:
            continue
        if review.get("source_identity") != current_source:
            continue
        cfg = manifest
        if window_key:
            window = review.get("review_window_us") or {}
            if not (type(window.get("start")) is int and type(window.get("end")) is int
                    and round(shot["source_start"] * 1e6) <= window["start"] < window["end"] <= round(shot["source_end"] * 1e6)):
                continue
            cfg = {**manifest,
                "head_trim_s": max(0, timing_contract.us_of(manifest.get("head_trim_s"), 300_000) / 1e6
                                       - (window["start"] / 1e6 - shot["source_start"])),
                "tail_trim_s": max(0, float(manifest.get("tail_trim_s", 0) or 0)
                                       - (shot["source_end"] - window["end"] / 1e6))}
        solution = shot_analyzer.candidate_window(_observed_shot(shot, review),
            audio_duration_us=duration, manifest=cfg, time_sensitive=review["time_sensitive"])
        if solution.ok:
            candidates.append((solution.speed > contract.playback.PROCESS_PREFERRED_MAX,
                -float(review.get("confidence", 0)), solution.material_us, review["shot_id"], window_key))
    for _, _, _, sid, window_key in sorted(candidates):
        part = {"shot_id": sid, "audio_duration_us": duration}
        if window_key:
            part.update(window_review_key=window_key, source_start_us=plan["window_reviews"][window_key]["review_window_us"]["start"])
        yield {"index": index, "parts": [part],
               "reason": "short complete reviewed window under " + contract.playback.VERSION}


def _revalidate_rows(manifest, analysis, plan, *, narration_timing=None):
    """Re-solve cached rows; replace only failing sentences, without API calls."""
    attempted = set()
    while True:
        try:
            result = _assemble(manifest, analysis, plan, narration_timing=narration_timing,
                               verify_sources=True)
            plan["playback_policy_version"] = contract.playback.VERSION
            return result
        except WindowFailure as failure:
            old = next(row for row in plan["rows"] if row["index"] == failure.index)
            key = (failure.index, contract.digest(old["parts"]))
            if key in attempted:
                raise
            attempted.add(key)
            replaced = False
            for candidate in _short_complete_rows(manifest, analysis, plan, failure.index, narration_timing):
                if (failure.index, contract.digest(candidate["parts"])) in attempted:
                    continue
                trial = {**plan, "rows": [candidate if row["index"] == failure.index else row
                                           for row in plan["rows"]]}
                try:
                    _assemble(manifest, analysis, trial, narration_timing=narration_timing,
                              verify_sources=True)
                except (ValueError, KeyError, TypeError, OSError) as other:
                    if repairable_index(other) in (None, failure.index):
                        continue
                plan["rows"] = trial["rows"]
                plan.setdefault("playback_repairs", []).append({"index": failure.index,
                    "reason": failure.reason, "previous": copy.deepcopy(old), "replacement": copy.deepcopy(candidate)})
                replaced = True
                break
            if not replaced:
                raise


def _retrieve_empty_selection(manifest, analysis, plan, failure, narration_timing):
    """An empty model choice must not hide a known, reviewed timing conflict.

    Retrieve a complete feasible candidate first. A long reviewed candidate is only
    a repair proposal, never a certified selection; normal assembly still rejects it
    until a short-window or joint pixel review and the timing checks succeed.
    """
    from . import shot_analyzer
    if isinstance(failure, WindowFailure):
        return False
    index = repairable_index(failure)
    if index is None:
        return False
    row = next((r for r in plan.get("rows", []) if isinstance(r, dict) and r.get("index") == index), None)
    excluded = qc_rejected_shots(manifest).get(index, set())
    rejected_selection = row is not None and any(p.get("shot_id") in excluded for p in row.get("parts", []))
    if row is None or (row.get("parts") != [] and not rejected_selection) or row.get("continuity_key"):
        return False
    candidate = next(_short_complete_rows(manifest, analysis, plan, index, narration_timing), None)
    if candidate is None:
        sentence = next(s for s in plan["sentences"] if s["index"] == index)
        if sentence["kind"] != "selling_point":
            return False
        shots = {s["shot_id"]: s for s in analysis["shots"]}
        required = {p["id"] for p in sentence["selling_points"]}
        duration, _ = timing_contract.duration_for(narration_timing, index,
            segment=timing_contract.split_sentences(manifest)[index - 1])
        for review in plan["reviews"].get(str(index), []):
            shot = shots.get(review.get("shot_id"))
            if (not shot or review.get("verdict") != "matched" or review.get("same_product") is not True
                    or review.get("shot_id") in qc_rejected_shots(manifest).get(index, set())
                    or not contract.points_supported(required, set(review.get("supported_point_ids", [])))
                    or review.get("source_identity") != contract.source_identity(shot["video"])):
                continue
            solution = shot_analyzer.candidate_window(_observed_shot(shot, review),
                audio_duration_us=duration, manifest=manifest, time_sensitive=review["time_sensitive"])
            if solution.ok or solution.reason not in {
                    timing_contract.REASON_EVIDENCE_SPAN_TOO_LONG,
                    timing_contract.REASON_EVIDENCE_SPEED_CONFLICT,
                    timing_contract.REASON_EVIDENCE_OUT_OF_RANGE}:
                continue
            part = {"shot_id": shot["shot_id"], "audio_duration_us": duration}
            # Evidence that starts inside the head/tail guard still needs a real
            # short-window re-review; seed an in-guard start so placement can run.
            if solution.reason == timing_contract.REASON_EVIDENCE_OUT_OF_RANGE:
                head = timing_contract.us_of(manifest.get("head_trim_s"), 300_000)
                scene_start = round(float(shot["source_start"]) * 1e6)
                part["source_start_us"] = scene_start + head
            candidate = {"index": index, "parts": [part],
                         "reason": "reviewed evidence requires window or continuity repair"}
            break
    if candidate is None:
        return False
    plan["rows"] = [candidate if r["index"] == index else r for r in plan["rows"]]
    plan.setdefault("playback_repairs", []).append({"index": index, "reason": "EMPTY_SELECTION_RETRIEVAL",
        "previous": copy.deepcopy(row), "replacement": copy.deepcopy(candidate)})
    return True


def _duplicate_repair_focus(plan, failure):
    reuse = re.fullmatch(r'AI_EDITOR_SOURCE_REUSE_LIMIT:(\d+)', str(failure))
    if reuse:
        return int(reuse[1])
    match = re.fullmatch(r'AI_EDITOR_REPEATED_SOURCE_WINDOW:(\d+):(\d+)', str(failure))
    if not match:
        return None
    first, second = map(int, match.groups())
    rows = {r.get('index'): r for r in plan.get('rows', []) if isinstance(r, dict)}
    if rows.get(second, {}).get('continuity_key') and not rows.get(first, {}).get('continuity_key'):
        return first
    return second


def _place_empty_matched_evidence(manifest, analysis, plan, directory, narration_timing) -> bool:
    """Fill empty matched-evidence rows and run their short-window placement first.

    Assemble walks sentences in order, so an earlier SPAN placement can burn the
    vision budget before a later empty OUT_OF_RANGE row is ever retrieved.
    """
    changed = False
    for sentence in plan.get("sentences") or []:
        index = sentence.get("index")
        if type(index) is not int:
            continue
        row = next((r for r in (plan.get("rows") or [])
                    if isinstance(r, dict) and r.get("index") == index), None)
        if row is None or row.get("parts"):
            continue
        if not _retrieve_empty_selection(
                manifest, analysis, plan, ValueError(f"AI_EDITOR_NO_SELECTION:{index}"),
                narration_timing):
            continue
        changed = True
        row = next(r for r in plan["rows"] if r["index"] == index)
        part = (row.get("parts") or [None])[0]
        if not isinstance(part, dict) or not part.get("shot_id"):
            continue
        # A cached short window is already reviewed and feasible. Placement
        # review is only for an out-of-range seed, and it must run before an
        # earlier long crop spends the vision budget. A merely long crop stays
        # in the normal repair order so one failed short review can still
        # become a sentence group.
        if str(row.get("reason") or "").startswith("short complete"):
            continue
        if "source_start_us" not in part:
            continue
        if _repair_selected_window(
                manifest, analysis, plan, directory,
                WindowFailure(index, part["shot_id"], timing_contract.REASON_EVIDENCE_OUT_OF_RANGE)):
            continue
    return changed


_WIPE_MARK = re.compile(r"纸巾|按压|擦拭")
_WIPE_ONLY = re.compile(r"反渗|回流|不粘|不黏")
_ABSORB_MARK = re.compile(r"瞬吸|秒吸|吸收|下渗|渗透|倒水|倒液")


def _absorb_sentence_phase(text: str) -> str:
    """Wipe claims use the press; seep and absorb stay on the pour-to-absorb span."""
    raw = str(text or "")
    if _WIPE_MARK.search(raw):
        return "wipe"
    if _WIPE_ONLY.search(raw) and not _ABSORB_MARK.search(raw):
        return "wipe"
    return "absorb"


def _phase_span_us(phases: dict, names: tuple[str, ...], lo: int, hi: int):
    starts, ends = [], []
    for name in names:
        span = phases.get(name) if isinstance(phases, dict) else None
        if not isinstance(span, (list, tuple)) or len(span) < 2:
            continue
        try:
            starts.append(int(round(float(span[0]) * 1_000_000)))
            ends.append(int(round(float(span[1]) * 1_000_000)))
        except (TypeError, ValueError):
            continue
    if not starts:
        return None
    start, end = max(lo, min(starts)), min(hi, max(ends))
    if end - start < 100_000:
        return None
    return start, end


def _line_audio_us(manifest, sentence, narration_timing) -> int:
    index = sentence.get("index")
    original = timing_contract.split_sentences(manifest)
    if type(index) is not int or not 1 <= index <= len(original):
        return 0
    audio, _certainty = timing_contract.duration_for(narration_timing, index, segment=original[index - 1])
    return int(audio or 0)


def _place_absorb_experiment(manifest, analysis, plan, directory, narration_timing,
                             _excluded_candidates=None, _only_group=None) -> bool:
    """Put adjacent absorb claims on one experiment shot, cut by phase.

    下渗/渗透 stay with the pour and absorb. A tissue-press line uses the wipe
    phase of that same shot. A phase the shot does not contain keeps the
    sentence's own shot and is recorded, instead of borrowing another experiment.
    """
    from .. import rulepack
    from ..policy import scene_rules
    from ..vision import phase_timing

    rules = rulepack.for_manifest(manifest)
    scene = next((s for s in rules.get("scenes") or []
                  if isinstance(s, dict) and s.get("id") == "absorb_test"), None)
    sentences = [s for s in plan.get("sentences") or [] if isinstance(s, dict) and type(s.get("index")) is int]
    if scene is None or len(sentences) < 2:
        return False
    sentences.sort(key=lambda s: s["index"])
    rows = {r.get("index"): r for r in plan.get("rows") or [] if isinstance(r, dict)}
    shots = {str(s.get("shot_id")): s for s in analysis.get("shots") or [] if isinstance(s, dict)}
    segments = [{"text": s.get("text"), "video": "grouped",
                 "audio_duration_us": _line_audio_us(manifest, s, narration_timing)}
                for s in sentences]
    semantic_roles = any(s.get("experiment_role") not in (None, "other", "uncertain") for s in sentences)
    if semantic_roles:
        positions = {s["index"]: p for p, s in enumerate(sentences)}
        groups = [{"positions": [positions[s["index"]] for s in group]}
                  for group in experiment_groups.groups(sentences, require_rewet=False)]
    else:
        # Historical plans keep their existing recall hints. They still need
        # phase pixels and the same joint continuity certificate to install.
        groups = [g for g in scene_rules._split_groups(
            segments, rules, {}, category=str(manifest.get("category") or ""))
            if g.get("scene", {}).get("id") == "absorb_test" and len(g.get("positions") or []) >= 2]
    if not groups:
        return False
    notes = plan.setdefault("absorb_phase_notes", [])
    placed_any = False
    excluded_candidates = _excluded_candidates if _excluded_candidates is not None else {}
    for group in groups:
        members = [sentences[p] for p in group["positions"] if p < len(sentences)]
        phase_by_index, verification = {}, False
        for member in members:
            role = member.get("experiment_role")
            verification = verification or role == "rewet"
            phase_by_index[member["index"]] = ("wipe" if verification else "absorb") if semantic_roles else _absorb_sentence_phase(member.get("text"))
        group_id = tuple(s["index"] for s in members)
        if _only_group is not None and group_id != _only_group:
            continue
        if any(rows.get(s["index"], {}).get("continuity_key") for s in members):
            continue
        rejected = qc_rejected_shots(manifest)
        # A directed repair must retain accepted neighboring sentences; this
        # optional group rewrite cannot establish cross-file experiment identity.
        if rejected or manifest.get("ai_refill_indices"):
            continue
        candidates = []
        seen_shots = set()
        for sentence in members:
            parts = (rows.get(sentence["index"]) or {}).get("parts") or []
            if len(parts) != 1 or not parts[0].get("shot_id"):
                continue
            sid = str(parts[0]["shot_id"])
            if (sid in seen_shots or sid in excluded_candidates.get(group_id, set())
                    or sid in rejected.get(sentence["index"], set())):
                continue
            seen_shots.add(sid)
            shot = shots.get(sid)
            if shot:
                candidates.append(shot)
        located, shot, phases = None, None, None
        needed = ["pour", "absorb"] + (["wipe"] if any(phase_by_index[s["index"]] == "wipe" for s in members) else [])
        missing_by_shot = {}
        for candidate in candidates:
            try:
                found = phase_timing.locate(candidate, scene, Path(directory))
            except (OSError, ValueError, TypeError, KeyError):
                found = {"ok": False, "reason": "PHASE_LOCATE_FAILED"}
            found_phases = found.get("phases") if isinstance(found, dict) else None
            if not found.get("ok") or not isinstance(found_phases, dict):
                missing_by_shot[candidate["shot_id"]] = list(needed)
                continue
            _lo = int(round(float(candidate.get("source_start") or 0) * 1_000_000))
            _hi = int(round(float(candidate.get("source_end") or 0) * 1_000_000))
            if type(candidate.get("material_duration_us")) is int and candidate["material_duration_us"] > 0:
                _hi = min(_hi, candidate["material_duration_us"])
            missing_by_shot[candidate["shot_id"]] = [p for p in needed
                                                     if _phase_span_us(found_phases, (p,), _lo, _hi) is None]
            lo = int(round(float(candidate.get("source_start") or 0) * 1_000_000))
            hi = int(round(float(candidate.get("source_end") or 0) * 1_000_000))
            material = candidate.get("material_duration_us")
            if type(material) is int and material > 0:
                hi = min(hi, material)
            if any(_phase_span_us(found_phases, (phase,), lo, hi) is None
                   for phase in ("pour", "absorb")):
                continue
            if (any(phase_by_index[s["index"]] == "wipe" for s in members)
                    and _phase_span_us(found_phases, ("wipe",), lo, hi) is None):
                continue
            located, shot, phases = found, candidate, found_phases
            break
        if shot is None:
            missed = candidates[0]["shot_id"] if candidates else ""
            notes.append({"indices": [s["index"] for s in members], "shot_id": missed,
                          "missing_phases": missing_by_shot.get(missed, list(needed)), "reason": "PHASE_UNLOCATED"})
            continue
        lo = int(round(float(shot.get("source_start") or 0) * 1_000_000))
        hi = int(round(float(shot.get("source_end") or 0) * 1_000_000))
        material = shot.get("material_duration_us")
        if type(material) is int and material > 0:
            hi = min(hi, material)
        body = _phase_span_us(phases, ("pour", "absorb"), lo, hi)
        wipe = _phase_span_us(phases, ("wipe",), lo, hi)
        snapshot = {s["index"]: copy.deepcopy(rows[s["index"]]) for s in members if s["index"] in rows}
        placed = []
        cursors = {"absorb": body[0] if body else None, "wipe": wipe[0] if wipe else None}
        limits = {"absorb": body[1] if body else None, "wipe": wipe[1] if wipe else None}
        for sentence in members:
            phase = phase_by_index[sentence["index"]]
            index = sentence["index"]
            # Directed QC repairs must not silently replace already accepted
            # neighboring sentences in the same experiment group.
            if rejected and index not in rejected:
                continue
            audio = _line_audio_us(manifest, sentence, narration_timing)
            if shot.get("shot_id") in rejected.get(index, set()):
                notes.append({"index": index, "shot_id": shot.get("shot_id"),
                              "reason": "这句已经排除这条镜头，仍用原镜头"})
                continue
            if phase == "wipe" and wipe is None:
                notes.append({"index": index, "shot_id": shot.get("shot_id"), "missing_phase": "wipe",
                              "reason": "缺少同次实验按压证据，保留原镜头并等待独立复核"})
                continue
            if phase != "wipe" and body is None:
                notes.append({"index": index, "shot_id": shot.get("shot_id"), "missing_phase": "absorb",
                              "reason": "这条实验素材没有倒液吸收，这句仍用原镜头"})
                continue
            start = cursors[phase]
            limit = limits[phase]
            if type(start) is not int or type(limit) is not int or audio <= 0 or start + audio > limit:
                notes.append({"index": index, "shot_id": shot.get("shot_id"), "missing_phase": phase,
                              "reason": "这段实验后面不够原速再播这句，仍用原镜头"})
                continue
            key = _review_proposed_window(
                manifest, plan, directory, sentence, shot, (start, start + audio),
                attempt_metadata={"mode": "absorb_phase", "phase": phase})
            if not key:
                notes.append({"index": index, "shot_id": shot.get("shot_id"), "missing_phase": phase,
                              "reason": "这段相位画面没有复核通过，仍用原镜头"})
                continue
            revised = (plan.get("window_reviews") or {}).get(key) or {}
            reviews = plan.setdefault("reviews", {}).setdefault(str(index), [])
            if not any(r.get("shot_id") == shot.get("shot_id") and r.get("verdict") == "matched" for r in reviews):
                reviews.append(revised)
            row = rows.get(index)
            if not isinstance(row, dict):
                continue
            row["parts"] = [{"shot_id": shot.get("shot_id"), "source_start_us": start,
                             "audio_duration_us": audio, "window_review_key": key, "absorb_phase": phase}]
            row.pop("continuity_key", None)
            cursors[phase] = start + audio
            placed.append(index)
        if len(placed) != len(members):
            for index in placed:
                rows[index].clear()
                rows[index].update(snapshot[index])
            plan["rows"] = [copy.deepcopy(snapshot[r["index"]]) if r["index"] in placed else r for r in plan["rows"]]
            rows = {r.get("index"): r for r in plan["rows"] if isinstance(r, dict)}
            notes.append({"indices": list(group_id), "shot_id": shot.get("shot_id"),
                          "reason": "实验相位无法完整配对，尝试其他已选素材"})
            excluded_candidates.setdefault(group_id, set()).add(str(shot.get("shot_id")))
            placed_any = (_place_absorb_experiment(manifest, analysis, plan, directory, narration_timing,
                          excluded_candidates, group_id) or placed_any)
            continue
        # A source file may contain several unrelated demonstrations.  The
        # existing continuity contract is the only certificate that links all
        # selected windows to one observed demonstration; individual reviews
        # and phase timestamps do not establish that relationship.
        from ..policy import continuity
        allocations = []
        for sentence in members:
            part = rows[sentence["index"]]["parts"][0]
            start = part["source_start_us"]
            audio = part["audio_duration_us"]
            target = audio + timing_contract.us_of(manifest.get("tail_pad_s"), 0)
            member = {k: copy.deepcopy(sentence[k]) for k in
                      ("index", "text", "kind", "selling_points", "scene_ids") if k in sentence}
            member.update(audio_duration_us=audio, target_us=target, start_us=start, end_us=start + target)
            allocations.append(member)
        valid_shape = (2 <= len(allocations) <= continuity.MAX_MEMBERS
                       and all(m.get("kind") == "selling_point" and m.get("selling_points") for m in allocations)
                       and all(a["end_us"] == b["start_us"] for a, b in zip(allocations, allocations[1:])))
        try:
            if not valid_shape:
                raise ValueError("CONTINUITY_PAIRING_UNCERTAIN")
            context = {"product": manifest.get("product"), "category": manifest.get("category"),
                       "rules": contract.rules_identity(manifest), "shot_id": shot["shot_id"],
                       "source_identity": contract.source_identity(shot["video"]),
                       "scene_us": {"start": lo, "end": hi},
                       "window_us": {"start": allocations[0]["start_us"], "end": allocations[-1]["end_us"]},
                       "members": allocations}
            group_review = sentence_continuity.review_group(manifest, context, shot, Path(directory))
            group_key = contract.digest(context)[:24]
            sentence_continuity.install(plan, group_key, group_review)
        except (ValueError, KeyError, TypeError, OSError):
            for index in placed:
                rows[index].clear()
                rows[index].update(snapshot[index])
            plan["rows"] = [copy.deepcopy(snapshot[r["index"]]) if r["index"] in placed else r for r in plan["rows"]]
            rows = {r.get("index"): r for r in plan["rows"] if isinstance(r, dict)}
            notes.append({"indices": list(group_id), "shot_id": shot.get("shot_id"),
                          "reason": "同次演示未经联合确认，保留原组"})
            excluded_candidates.setdefault(group_id, set()).add(str(shot.get("shot_id")))
            placed_any = (_place_absorb_experiment(manifest, analysis, plan, directory, narration_timing,
                          excluded_candidates, group_id) or placed_any)
            continue
        try:
            _assemble(manifest, analysis, plan, narration_timing=narration_timing)
        except WindowFailure as exc:
            if exc.index in placed:
                for index in placed:
                    rows[index].clear()
                    rows[index].update(snapshot[index])
                plan["rows"] = [copy.deepcopy(snapshot[r["index"]]) if r["index"] in placed else r for r in plan["rows"]]
                rows = {r.get("index"): r for r in plan["rows"] if isinstance(r, dict)}
                notes.append({"indices": placed, "shot_id": shot.get("shot_id"),
                              "reason": f"相位切开后排不开:{exc.reason}"})
                excluded_candidates.setdefault(group_id, set()).add(str(shot.get("shot_id")))
                placed_any = (_place_absorb_experiment(manifest, analysis, plan, directory, narration_timing,
                              excluded_candidates, group_id) or placed_any)
                continue
        except (ValueError, KeyError, TypeError):
            for index in placed:
                rows[index].clear()
                rows[index].update(snapshot[index])
            plan["rows"] = [copy.deepcopy(snapshot[r["index"]]) if r["index"] in placed else r for r in plan["rows"]]
            rows = {r.get("index"): r for r in plan["rows"] if isinstance(r, dict)}
            excluded_candidates.setdefault(group_id, set()).add(str(shot.get("shot_id")))
            placed_any = (_place_absorb_experiment(manifest, analysis, plan, directory, narration_timing,
                          excluded_candidates, group_id) or placed_any)
            continue
        placed_any = True
        plan.setdefault("absorb_groups", []).append({
            "indices": [s["index"] for s in members], "shot_id": shot.get("shot_id"),
            "placed": placed, "missing_phases": [] if wipe else ["wipe"]})
    return placed_any


def _speed_fit_reviewed_evidence(manifest, analysis, plan, directory, failure, narration_timing=None) -> bool:
    """Play matched evidence up to 2x when a shorter crop is not available.

    The short-window look runs first. If that look cannot be made, and the
    evidence already reviewed still fits inside twice the spoken line, keep
    every reviewed frame and speed the cut. Time-sensitive proof and anything
    over 2x stay blocked. This does not call the model or crop the result.
    """
    if failure.reason != timing_contract.REASON_EVIDENCE_SPAN_TOO_LONG:
        return False
    sentence = next((s for s in plan.get("sentences") or []
                     if isinstance(s, dict) and s.get("index") == failure.index), None)
    row = next((r for r in plan.get("rows") or []
                if isinstance(r, dict) and r.get("index") == failure.index), None)
    if sentence is None or row is None or row.get("continuity_key"):
        return False
    if sentence.get("kind") != "selling_point" or sentence.get("time_sensitive"):
        return False
    parts = row.get("parts") or []
    if failure.part_index is not None and 0 <= failure.part_index < len(parts):
        part = parts[failure.part_index]
    else:
        matching = [p for p in parts if isinstance(p, dict) and p.get("shot_id") == failure.shot_id]
        part = matching[0] if len(matching) == 1 else None
    if not isinstance(part, dict):
        return False
    shot = next((s for s in analysis.get("shots") or [] if s.get("shot_id") == failure.shot_id), None)
    reviews = plan.get("reviews", {}).get(str(failure.index), [])
    review = next((r for r in reviews if isinstance(r, dict) and r.get("shot_id") == failure.shot_id
                   and r.get("verdict") == "matched" and r.get("time_sensitive") is not True), None)
    if shot is None or review is None:
        return False
    existing = plan.get("window_reviews", {}).get(part.get("window_review_key")) or {}
    if existing.get("placement_review") is True:
        return False
    intervals = [ev for ev in review.get("evidence_intervals_us") or []
                 if isinstance(ev, dict) and type(ev.get("start")) is int and type(ev.get("end")) is int
                 and ev["start"] < ev["end"]]
    audio = part.get("audio_duration_us")
    if not intervals or type(audio) is not int or audio <= 0:
        return False
    start, end = min(ev["start"] for ev in intervals), max(ev["end"] for ev in intervals)
    budget = int(round(audio * contract.playback.PROCESS_PREFERRED_MAX))
    if end - start > budget:
        return False
    lo, hi = round(float(shot["source_start"]) * 1e6), round(float(shot["source_end"]) * 1e6)
    material = shot.get("material_duration_us")
    if type(material) is int and material > 0:
        hi = min(hi, material)
    # The 2x crop stays on the reviewed evidence. A few frames of slack inside
    # the same shot let the frame grid cover that evidence without clipping it.
    crop_start, crop_end = start, min(hi, start + budget)
    if crop_end < end:
        crop_end = end
        crop_start = max(lo, end - budget)
    if not lo <= crop_start <= start < end <= crop_end <= hi:
        return False
    key = contract.digest(["speed-fit", failure.index, shot["shot_id"], crop_start, crop_end, audio])[:24]
    if part.get("window_review_key") == key:
        return False
    saved = {k: copy.deepcopy(v) for k, v in review.items()
             if k not in {"playback_observation", "review_window_us", "placement_review", "continuity_key"}}
    saved.update(placement_review=True, placement_verdict="suitable", sentence_index=failure.index,
                 shot_id=shot["shot_id"], verdict="matched", time_sensitive=False,
                 playback_observation=None, review_window_us={"start": crop_start, "end": crop_end},
                 evidence_intervals_us=copy.deepcopy(intervals),
                 source_identity=review.get("source_identity"))
    trial = copy.deepcopy(plan)
    trial.setdefault("window_reviews", {})[key] = saved
    trial_row = next(r for r in trial["rows"] if r["index"] == failure.index)
    trial_part = trial_row["parts"][parts.index(part)]
    trial_part["window_review_key"] = key
    trial_part["source_start_us"] = crop_start
    try:
        _assemble(manifest, analysis, trial, narration_timing=narration_timing, verify_sources=True)
    except WindowFailure as other:
        if other.index <= failure.index:
            return False
    except (ValueError, KeyError, TypeError, OSError):
        return False
    plan.setdefault("window_reviews", {})[key] = saved
    part["window_review_key"] = key
    part["source_start_us"] = crop_start
    plan.setdefault("playback_repairs", []).append({
        "index": failure.index, "shot_id": shot["shot_id"], "reason": failure.reason,
        "speed_fit": "reviewed evidence within 2x"})
    _save(directory / "plan.json", plan)
    return True


def _repair_reviewed_rows(manifest, analysis, plan, directory, narration_timing, duplicate_expanded):
    """Drain useful local repairs without spending text-editor retry rounds.

    Every loop must change the selected rows. The existing persistent per-sentence
    pixel-review budgets still apply; a separate finite step ceiling is defensive
    against faulty repair helpers, while repeated states stop immediately.
    """
    # Empty OUT_OF_RANGE / SPAN seeds must place before earlier sentences spend
    # the vision budget on their own short-window repairs.
    _place_absorb_experiment(manifest, analysis, plan, directory, narration_timing)
    experiment_groups.repair(manifest, analysis, plan, directory, narration_timing)
    _place_empty_matched_evidence(manifest, analysis, plan, directory, narration_timing)
    seen = set()
    for _ in range(1 + 8 * len(plan.get("sentences", []))):
        _seed_unplaced_hooks(manifest, analysis, plan, directory, narration_timing)
        try:
            return _revalidate_rows(manifest, analysis, plan, narration_timing=narration_timing)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            failure = exc
        state = contract.digest(plan.get("rows"))
        if state in seen:
            raise failure
        seen.add(state)
        rejected_row = re.fullmatch(r"AI_EDITOR_UNREVIEWED_SELECTION:(\d+)", str(failure))
        if rejected_row:
            index = int(rejected_row[1])
            excluded = qc_rejected_shots(manifest).get(index, set())
            row = next((r for r in plan["rows"] if r["index"] == index), {})
            if any(p.get("shot_id") in excluded for p in row.get("parts", [])):
                failure = ValueError(f"AI_EDITOR_NO_SELECTION:{index}")
        if _retrieve_empty_selection(manifest, analysis, plan, failure, narration_timing):
            continue
        repeated_index = _duplicate_repair_focus(plan, failure)
        if repeated_index is not None:
            repair_indices = [repeated_index]
            if str(failure).startswith("AI_EDITOR_SOURCE_REUSE_LIMIT:"):
                try:
                    baseline = _assemble(manifest, analysis, plan, narration_timing=narration_timing,
                                         verify_sources=True, validate_sequence=False)
                    details = contract.source_reuse_conflict_details(
                        baseline["segments"], contract.source_reuse_limit(manifest))
                    for detail in details:
                        if detail[0] == str(failure):
                            repair_indices.extend(contract.source_reuse_indices(baseline["segments"], detail[1]))
                except (ValueError, KeyError, TypeError, OSError):
                    pass
            if any(_repair_repeated_window(manifest, analysis, plan, directory, narration_timing,
                                           index, duplicate_expanded) for index in dict.fromkeys(repair_indices)):
                continue
        if isinstance(failure, WindowFailure):
            if _seed_unplaced_hooks(manifest, analysis, plan, directory, narration_timing, failed_index=failure.index):
                continue
            # A recrop that still cannot hold the evidence used to count as
            # progress and skip the previous-shot tail. Look at that tail first.
            sentence = next((s for s in plan.get("sentences") or []
                             if isinstance(s, dict) and s.get("index") == failure.index), {})
            short_without_point = (
                sentence.get("kind") in {"hook", "product_intro"}
                and not sentence.get("selling_points")
                and failure.reason == timing_contract.REASON_EVIDENCE_SPAN_TOO_LONG)
            if short_without_point and _extend_previous_action(
                    manifest, analysis, plan, directory, failure, narration_timing):
                continue
            if _repair_selected_window(manifest, analysis, plan, directory, failure):
                continue
            if sentence_continuity.repair(manifest, analysis, plan, directory, failure, narration_timing):
                continue
            if not short_without_point and _speed_fit_reviewed_evidence(
                    manifest, analysis, plan, directory, failure, narration_timing):
                continue
            if not short_without_point and _extend_previous_action(
                    manifest, analysis, plan, directory, failure, narration_timing):
                continue
        raise failure
    raise ValueError("AI_EDITOR_REPAIR_PROGRESS_LIMIT")


def _clear_incomplete_groups(plan):
    """Unselect orphaned group members; never promote old group observations."""
    rows = plan.get('rows') or []
    keys = {r.get('continuity_key') for r in rows if isinstance(r, dict) and r.get('continuity_key')}
    for key in keys:
        group = plan.get('continuity_reviews', {}).get(key) or {}
        members = (group.get('input') or {}).get('members') or []
        expected = {m.get('index') for m in members if isinstance(m, dict)}
        actual = {r.get('index') for r in rows if r.get('continuity_key') == key}
        if not expected or actual != expected:
            rows = [{'index': r['index'], 'parts': [], 'reason': 'incomplete continuous group; reselect'}
                    if r.get('continuity_key') == key else r for r in rows]
    plan['rows'] = rows


def _restore_interrupted_groups(manifest, analysis, plan, previous, narration_timing):
    """Recover a validated group split by an unsuccessful partial edit."""
    if not any(row.get('message') == 'CONTINUITY_INPUT_CHANGED'
               for row in previous.get('issues', []) if isinstance(row, dict)):
        return
    rejected = qc_rejected_shots(manifest)
    rows = plan.get('rows') or []
    keys = {r.get('continuity_key') for r in rows if isinstance(r, dict) and r.get('continuity_key')}
    for key in keys:
        group = plan.get('continuity_reviews', {}).get(key) or {}
        context = group.get('input') or {}
        members = {m.get('index') for m in context.get('members', []) if isinstance(m, dict)}
        selected = {r.get('index') for r in rows if r.get('continuity_key') == key}
        if not members or selected == members or any(context.get('shot_id') in rejected.get(i, set()) for i in members):
            continue
        trial = copy.deepcopy(plan)
        try:
            sentence_continuity.install(trial, key, group)
            sentence_continuity.validate_plan(manifest, analysis, trial, narration_timing)
        except (ValueError, KeyError, TypeError, OSError, StopIteration):
            continue
        trial['ok'] = False
        plan.update(trial)
        rows = plan['rows']


def _replace_focused_row(plan, index, revised):
    """A partial edit dissolves the old group and reselects other members."""
    old = next((r for r in plan['rows'] if r.get('index') == index), None)
    key = old.get('continuity_key') if old else None
    if key:
        group = plan.get('continuity_reviews', {}).get(key)
        if not group:
            raise ValueError('CONTINUITY_INPUT_CHANGED')
        members = {m['index'] for m in group['input']['members']}
        if index not in members or any(not any(r.get('index') == i and r.get('continuity_key') == key
                                                for r in plan['rows']) for i in members):
            raise ValueError('CONTINUITY_INPUT_CHANGED')
        plan['rows'] = [revised if r.get('index') == index else
            {'index': r['index'], 'parts': [], 'reason': 'continuous group changed; reselect from reviewed candidates'}
            if r.get('index') in members else r for r in plan['rows']]
    else:
        plan['rows'] = [revised if r.get('index') == index else r for r in plan['rows']]


def _linked_hooks(plan):
    """Yield the two sentence indices bound by each accepted hook review."""
    for row in plan["rows"]:
        for part in row.get("parts") or []:
            review = plan.get("window_reviews", {}).get(part.get("window_review_key"), {})
            side = review.get("hook_neighbor_side")
            if side in ("previous", "next"):
                if review.get("hook_link_supported") is not True:
                    raise ValueError(f"AI_EDITOR_UNREVIEWED_SELECTION:{row['index']}")
                index = row["index"]
                yield index, index + (1 if side == "next" else -1)


def _mix_repeated_displays(manifest, analysis, plan, timing, inventory):
    """Replace plain repeated displays only when the complete alternate edit validates."""
    from . import shot_notes
    if not inventory or manifest.get("shot_notes_general"):
        return
    menus = {m["index"]: m for m in inventory if m["kind"] in {"hook", "product_intro"}}
    sources = {s["shot_id"]: s["video"] for s in analysis.get("shots", [])}
    occupied = {sources.get(p.get("shot_id")) for row in plan["rows"] for p in row.get("parts", [])}
    if not any(c["prior_source_tasks"] for menu in menus.values() for c in menu["candidates"]):
        return
    original = timing_contract.split_sentences(manifest)
    revised = list(plan["rows"])
    linked = {i for pair in _linked_hooks(plan) for i in pair}
    changed = False
    for position, row in enumerate(plan["rows"]):
        index = row["index"]
        menu = menus.get(index)
        parts = row.get("parts") or []
        if (not menu or index in linked or len(parts) != 1 or row.get("continuity_key") or parts[0].get("window_review_key")
                or index in plan.get("_frozen_segments", {}) or original[index-1].get("visual_requirements")
                or shot_notes.note_for(manifest, index, original[index-1])):
            continue
        chosen = next((c for c in menu["candidates"] if c["shot_id"] == parts[0]["shot_id"]), None)
        if not chosen or not chosen["prior_source_tasks"]:
            continue
        alternative = next((c for c in menu["candidates"]
            if c["editorial_order"] < chosen["editorial_order"] and c["prior_source_tasks"] < chosen["prior_source_tasks"]
            and c.get("basis") == chosen.get("basis") and c.get("confidence", 0) >= chosen.get("confidence", 0)
            and c["technical_option"].get("within_preferred_speed") and not c.get("time_sensitive")
            and sources.get(c["shot_id"]) not in occupied), None)
        if alternative:
            occupied.add(sources[alternative["shot_id"]])
            revised[position] = {**row, "parts": [{"shot_id": alternative["shot_id"],
                "audio_duration_us": parts[0]["audio_duration_us"]}],
                "reason": "同商品展示已有复核，换用其他任务较少使用的素材。"}
            changed = True
    if changed:
        try:
            _assemble(manifest, analysis, {**plan, "rows": revised}, narration_timing=timing, verify_sources=True)
        except (KeyError, ValueError, TypeError, OSError):
            return
        plan["rows"] = revised


def _vary_repeated_actions(manifest, analysis, plan, timing, inventory):
    """Prefer a new action over a restart, only among equivalent, reviewed cuts."""
    from . import shot_notes, shot_value, shot_usability
    if plan.get("_frozen_segments") or manifest.get("shot_notes_general") or not any(
            len({c["window_action_hint"] for c in m["candidates"] if c.get("window_action_hint")}) > 1
            for m in inventory):
        return
    shots = {s["shot_id"]: s for s in analysis["shots"]}
    menus = {m["index"]: m["candidates"] for m in inventory}
    segments = _assemble(manifest, analysis, plan, narration_timing=timing, verify_sources=True)["segments"]
    original = timing_contract.split_sentences(manifest)
    protected = {i for pair in _linked_hooks(plan) for i in pair}
    counts = Counter()
    actions = {}
    occupied = set()
    for index, segment in enumerate(segments, 1):
        actions[index] = []
        for part in segment.get("span_parts") or [segment]:
            shot = shots[part["shot_id"]]
            occupied.add(shot["video"])
            action = window_action(shot, part["source_start"], part["source_end"])
            counts[action] += 1
            actions[index].append(action)
        if index > 1 and _touching(segments, index, index - 1):
            protected.update((index - 1, index))

    seen = set()
    for row in sorted(plan["rows"], key=lambda r: r["index"]):
        index, parts = row["index"], row["parts"]
        row_actions = actions[index]
        if len(row_actions) != 1 or not row_actions[0] or row_actions[0] not in seen:
            seen.update(row_actions)
            continue
        action = row_actions[0]
        if (index in protected or len(parts) != 1 or row.get("continuity_key") or parts[0].get("window_review_key")
                or original[index - 1].get("visual_requirements")
                or shot_notes.note_for(manifest, index, original[index - 1])):
            continue
        menu = menus[index]
        chosen = next((c for c in menu if c["shot_id"] == parts[0]["shot_id"]), None)
        if not chosen or chosen.get("time_sensitive"):
            continue
        for candidate in menu:
            other = candidate.get("window_action_hint")
            if (not other or counts[other] or shots[candidate["shot_id"]]["video"] in occupied
                    or candidate.get("time_sensitive") or candidate["basis"] != chosen["basis"]
                    or candidate["confidence"] < chosen["confidence"]
                    or not set(chosen["supported_point_ids"]) <= set(candidate["supported_point_ids"])
                    or shot_value.rank({"plan": candidate}) > shot_value.rank({"plan": chosen})
                    or shot_usability.rank(candidate)[1:] > shot_usability.rank(chosen)[1:]
                    or (candidate.get("usability") or {}).get("status") == "REJECTED"
                    or not candidate["technical_option"].get("within_preferred_speed")
                    or candidate["technical_option"]["detail"].get("waste_trim", {}).get("status") == timing_contract.WASTE_TRIM_RELAXED):
                continue
            revised = {**row, "parts": [{"shot_id": candidate["shot_id"], "audio_duration_us": parts[0]["audio_duration_us"]}],
                       "reason": f"同等卖点覆盖的已复核画面，以{other}替代本稿已出现的{action}。"}
            trial = [revised if r["index"] == index else r for r in plan["rows"]]
            try:
                edit = _assemble(manifest, analysis, {**plan, "rows": trial}, narration_timing=timing, verify_sources=True)
            except (ValueError, KeyError, TypeError, OSError):
                continue
            cut = edit["segments"][index - 1]
            if window_action(shots[candidate["shot_id"]], cut["source_start"], cut["source_end"]) != other:
                continue
            plan["rows"] = trial
            counts[action] -= 1
            counts[other] += 1
            occupied.add(shots[candidate["shot_id"]]["video"])
            seen.add(other)
            break


def _hook_neighbor(manifest, original, sentence, row):
    """Resolve a neutral line's proposed neighbor under the actual script and notes."""
    from . import shot_notes
    index = sentence["index"]
    side = row.get("hook_neighbor", sentence.get("hook_neighbor"))
    if (side not in ("next", "previous") or sentence["kind"] not in {"hook", "product_intro"}
            or sentence.get("selling_points") or sentence.get("scene_ids") or sentence.get("qc_feedback")
            or manifest.get("shot_notes_general") or row.get("continuity_key")
            or original[index - 1].get("visual_requirements")
            or shot_notes.note_for(manifest, index, original[index - 1])):
        return None
    neighbor = index + (1 if side == "next" else -1)
    if 1 <= neighbor <= len(original):
        return side, neighbor
    return None


def _hook_requests(manifest, plan):
    if plan.get("_frozen_segments"):
        return
    original = timing_contract.split_sentences(manifest)
    rows = {r.get("index"): r for r in plan.get("rows") or [] if isinstance(r, dict)}
    for sentence in plan["sentences"]:
        row = rows.get(sentence["index"], {})
        link = _hook_neighbor(manifest, original, sentence, row)
        if link and link[1] in rows:
            yield sentence, row, *link


def _hook_cut(manifest, plan, directory, sentence, side, shot, edge, anchor_text, duration):
    """Review the exact original-speed interval touching the neighbor's cut."""
    from . import shot_analyzer
    index = sentence["index"]
    if shot["shot_id"] in qc_rejected_shots(manifest).get(index, set()):
        return None
    length = duration + timing_contract.us_of(manifest.get("tail_pad_s"), 0)
    start = edge["window_us"]["start"] - length if side == "next" else edge["window_us"]["end"]
    end = start + length
    lo = round(float(shot["source_start"]) * 1e6) + timing_contract.us_of(
        manifest.get("head_trim_s"), round(shot_analyzer.DEFAULT_HEAD_TRIM_S * 1e6))
    hi = round(float(shot["source_end"]) * 1e6) - timing_contract.us_of(
        manifest.get("tail_trim_s"), round(shot_analyzer.DEFAULT_TAIL_TRIM_S * 1e6))
    if start < lo or end > hi:
        return None
    context = {"side": side, "text": anchor_text, "observation": edge["observation"], "window_us": edge["window_us"]}
    key = _review_proposed_window(manifest, plan, directory,
        {**sentence, "action_continuation": True, "hook_neighbor_context": context}, shot, (start, end))
    if key is None:
        return None
    plan["window_reviews"][key].update(action_continuation=True, hook_neighbor_side=side)
    return {"index": index, "parts": [{"shot_id": shot["shot_id"], "source_start_us": start,
            "audio_duration_us": duration, "window_review_key": key}],
            "reason": "无独立画面诉求，提前引入下一镜" if side == "next" else "无独立画面诉求，顺延上一镜"}


def _anchor_receipt(anchor, side):
    parts = anchor.get("span_parts") or [anchor]
    return parts[0 if side == "next" else -1]["ai_selection"]


def _touching(segments, index, neighbor):
    if not 1 <= neighbor <= len(segments):
        return False
    hook = segments[index - 1]["ai_selection"]
    anchor = segments[neighbor - 1]
    forward = neighbor > index
    edge = _anchor_receipt(anchor, "next" if forward else "previous")
    return (hook["source_identity"] == edge["source_identity"]
            and hook["window_us"]["end" if forward else "start"] == edge["window_us"]["start" if forward else "end"])


def _seed_unplaced_hooks(manifest, analysis, plan, directory, timing, *, failed_index=None):
    """Try an adjoining cut for an empty line or a standalone cut that failed to fit."""
    rows = plan.get("rows")
    if (not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows)
            or (failed_index is None and not any(r.get("parts") == [] for r in rows))):
        return False
    original = timing_contract.split_sentences(manifest)
    shots = {shot["shot_id"]: shot for shot in analysis["shots"]}
    changed = False
    for sentence, row, side, neighbor in _hook_requests(manifest, plan):
        if failed_index is not None and sentence["index"] != failed_index:
            continue
        if failed_index is None and row.get("parts") != []:
            continue
        index = sentence["index"]
        duration, certainty = timing_contract.duration_for(timing, index, segment=original[index - 1])
        if not duration or certainty == timing_contract.TIMING_CERTAINTY_ESTIMATED:
            continue
        try:
            anchors, _ = _assemble_segments(manifest, analysis, plan, timing, True, only_index=neighbor)
        except (ValueError, TypeError, KeyError, OSError):
            continue  # The ordinary repair loop may still repair this anchor.
        anchor = anchors[0]
        edge = _anchor_receipt(anchor, side)
        candidate = _hook_cut(manifest, plan, directory, sentence, side, shots[edge["shot_id"]],
                              edge, anchor["text"], duration)
        if candidate and candidate["parts"] != row.get("parts"):
            row.update(candidate)  # Full assembly must still validate the seam and source use.
            changed = True
    return changed


def _place_hook_neighbors(manifest, analysis, plan, directory, timing):
    """Prefer a continuous cut over an already valid standalone display."""
    requests = list(_hook_requests(manifest, plan))
    if not requests:
        return
    shots = {shot["shot_id"]: shot for shot in analysis["shots"]}
    baseline = _assemble(manifest, analysis, plan, narration_timing=timing, verify_sources=True)["segments"]
    protected = {i for pair in _linked_hooks(plan) for i in pair}
    for sentence, row, side, neighbor in requests:
        index = sentence["index"]
        segment, anchor = baseline[index - 1], baseline[neighbor - 1]
        if (index in protected or len(row.get("parts", [])) != 1
                or segment["timing_certainty"] == timing_contract.TIMING_CERTAINTY_ESTIMATED):
            continue
        if _touching(baseline, index, neighbor):
            protected.update((index, neighbor))
            continue
        edge = _anchor_receipt(anchor, side)
        try:
            candidate = _hook_cut(manifest, plan, directory, sentence, side, shots[edge["shot_id"]],
                                  edge, anchor["text"], segment["audio_duration_us"])
        except ValueError as exc:
            if failure_fields(str(exc)).get("failure_class") == "vision":
                return  # Stop optional calls and retain the valid standalone edit.
            raise
        if candidate:
            trial = [candidate if r["index"] == index else r for r in plan["rows"]]
            try:
                solved = _assemble(manifest, analysis, {**plan, "rows": trial},
                                   narration_timing=timing, verify_sources=True)["segments"]
            except (ValueError, KeyError, TypeError, OSError):
                continue
            if _touching(solved, index, neighbor):
                plan["rows"], baseline = trial, solved
                protected.update((index, neighbor))


def _edit_plan(manifest: dict, analysis: dict, plan: dict, directory: Path,
               narration_timing: dict | None = None) -> None:
    """Compile current editing instructions from frozen, pixel-reviewed candidates."""
    shots = {str(s["shot_id"]): s for s in analysis.get("shots", [])}
    plan["ok"] = False
    instructions = """
输出JSON {"rows":[{"index":1,"parts":[{"shot_id":"...","audio_duration_us":2000000,"source_start_us":0}],"reason":"整稿选择理由"}]}。
每句恰好一行，不能选择本句候选以外的镜头。parts的audio_duration_us为正整数且总和必须等于该句audio_duration_us。
多卖点可组合多镜，至少覆盖本句一个经复看的具体卖点，未覆盖项如实记录，不声称全部已证实。
单镜或多镜根据句意与实际配音节奏选择：当一段口播包含不同卖点或存在多个贴切画面，且每个镜头有足够时间清楚呈现时，优先考虑句内多镜，避免机械地每句一镜；不要为了镜头数量裁掉动作、重复播放或制造闪帧。
根据全稿句意与配音节奏，纯招呼、吊胃口或承上启下的短句优先和相邻画面连播，同时为该句照常选好独立备用镜头。
在该句 row 增加 hook_neighbor：开场“姐妹们”、指向下句的“那你看看”填 "next"；在描述上一动作之后自然接话填 "previous"。
有具体痛点（如“干纸擦得屁股疼”）、价格数量、功能动作、指定画面或其他独立信息时省略。不要单凭秒数或标点判定。程序另行复核相邻源窗。
同源连续实验顺接后续区间，不每句重播。source_start_us可省略，提供时不得裁掉必需证据。
镜头变化和去重只是质量接近时的偏好，不得抢走唯一卖点候选。没有合格候选返回空parts，不编造支持。
候选若带selling_point_value，它是本次画面中具体原文信息的辅助判断：同等匹配时优先能展示相关特征或直接演示的镜头；一般包装只适合无具体卖点的展示句。无此字段不代表不合格，不得把它当成独立质检或编造未展示的卖点。
候选若带usability，仅表示所选实际窗口内主体的可见性与清晰时长；质量接近时优先主体清楚、呈现充分者。主体清晰不等于证明卖点；未返回该字段表示未知。
technical_option是按本句完整配音求出的实际可行窗口；分配多镜后程序会重新求解，不可用单镜无解淘汰组合。
匹配质量相近时优先within_preferred_speed=true；普通完整放置短窗保留最高2倍许可，2到3倍仅限已复看的纯过程。实验分相的关键动作结果原速，无许可时原速。technical_option无解时必须换完整短窗口或如实报告冲突。
同品其他任务使用过该文件和镜头的任务数分别为prior_source_tasks和prior_scene_tasks。实际卖点、可读性、时长相当时，亲肤柔软等卖点和全稿任何位置的空泛钩子优先选较少用的文件、镜头及动作。同等条件按editorial_order选择，以便同时启动的任务也能混选。唯一贴切证据保留。
通读整条片子，对照每句的实际观察和window_action_hint；即使换了文件、背景或机位，重新举包或重新倒液仍是重复。
有等效画面时优先换有新信息的动作；连续实验从倒液推进到下渗、压纸、揭纸和结果，不在另一布景重新开始。
同一镜内自然延续和首次明确展示结果要保留。避开调整机位、空等、拿错或撤手重拍的余量，结果展示所需停留照常保留。
确实只有一个实际支持的候选、卖点不得不复用时，可在该row写scarce_reuse_reason解释；介绍/钩子和有替代候选时不允许此例外，相关实验仍须顺接而非相邻重播。
"""
    source_limit = contract.source_reuse_limit(manifest)
    if source_limit is not None:
        instructions += f"\n本任务每个源素材最多使用{source_limit}次；经验证的连续实验组按一次计算，不得以稀缺复用理由突破此上限。"
    from . import usage_ledger
    history = usage_ledger.recent_presentations(manifest)
    history_hint = ("\n同品类其他稿已经使用的实际展示如下。同一卖点或钩子有同等合格候选时，"
                    "换动作或机位。这些描述只供避免重复，不能当作本稿证据。\n"
                    + json.dumps(history, ensure_ascii=False)) if history else ""
    def make_prompt(current_inventory) -> str:
        return (contract.policy_prompt(manifest) + "\n你是整稿剪辑师。以下候选均已经实际看图。先保留卖点稀缺候选，再给介绍和钩子分配展示池。\n"
                + json.dumps(current_inventory, ensure_ascii=False) + instructions + history_hint)

    focus = None
    duplicate_expanded: set[int] = set()
    repair_instruction = ""
    for attempt in range(3):
        current_inventory = _build_edit_inventory(manifest, analysis, plan, narration_timing)
        prompt = make_prompt(current_inventory) + repair_instruction
        if attempt == 0 and plan.pop("_resume_rows", False):
            response = {"rows": plan["rows"]}
        else:
            response = _call(prompt, directory / f"edit_{attempt}.json")
        if focus is not None:
            revised = [r for r in response.get("rows", []) if isinstance(r, dict) and r.get("index") == focus]
            if len(revised) != 1:
                raise ValueError(f"AI_EDITOR_NO_SELECTION:{focus}")
            _replace_focused_row(plan, focus, revised[0])
        else:
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
                    exhaustive=True, existing_reviews=plan["reviews"].get(key, []),
                    excluded_ids=qc_rejected_shots(manifest).get(index, set()))
                plan["reviews"][key] = reviews
                plan.setdefault("coverage", {})[key] = coverage
                expanded = True
            if expanded:
                continue  # second and final editor call sees the complete audited pool
        try:
            _repair_reviewed_rows(manifest, analysis, plan, directory, narration_timing, duplicate_expanded)
            _mix_repeated_displays(manifest, analysis, plan, narration_timing, current_inventory)
            _place_hook_neighbors(manifest, analysis, plan, directory, narration_timing)
            _vary_repeated_actions(manifest, analysis, plan, narration_timing, current_inventory)
            plan["ok"] = True
            plan["phase"] = "selection_complete"
            return
        except (ValueError, KeyError, TypeError, OSError) as exc:
            failure = exc
            plan.setdefault("repair_history", []).append({"round": attempt + 1, "reason": str(failure),
                                                           "index": failed_index(failure)})
            _save(directory / "plan.json", plan)
            if attempt == 2:
                raise failure
            focus = _duplicate_repair_focus(plan, failure)
            if focus is None:
                focus = failed_index(failure)
            repair_instruction = "\n上一版技术检查失败：" + str(failure) + "。"
            if focus is not None:
                repair_instruction += f"只返回index={focus}的一行rows，更换无效候选或起点；其他句由程序原样保留。"
            else:
                repair_instruction += "重新输出完整rows。"
            repair_instruction += "不得把同一句的备选或修正各写成一行，每个index只保留最终决定的一行；多个镜头放在该行parts数组。"
            current = [{"index": row.get("index"), "parts": [
                {key: part[key] for key in ("shot_id", "source_start_us", "audio_duration_us") if key in part}
                for part in row.get("parts", []) if isinstance(part, dict)]}
                for row in plan.get("rows", []) if isinstance(row, dict)]
            repair_instruction += "\n当前选择（仅供修复，不作为视觉证据）：" + json.dumps(current, ensure_ascii=False)
            repair_instruction += "\n已检查窗口（不可把被拒窗口重标为通过）：" + json.dumps(plan.get("window_repair_attempts", {}), ensure_ascii=False)


def _edit_with_review_fallback(manifest, analysis, plan, directory, narration_timing):
    """Place matched evidence first; only then exhaust for a shorter alternative.

    Search already stopped once selling-point evidence was matched. Local short
    crop, mild speed, and sentence groups run inside edit/repair. If those still
    cannot fit the audio, exhaust only that sentence's remaining pool so a later
    short complete match can replace the long one—never trim old evidence.
    """
    shots = {str(s['shot_id']): s for s in analysis['shots']}
    rejected = qc_rejected_shots(manifest)
    expanded_indices = set()
    while True:
        try:
            _edit_plan(manifest, analysis, plan, directory, narration_timing)
            return
        except (ValueError, KeyError, TypeError, OSError) as exc:
            index = failed_index(exc)
            coverage = plan.get('coverage', {}).get(str(index), {})
            sentence = next((s for s in plan['sentences'] if s.get('index') == index), None)
            if not sentence or index in expanded_indices:
                raise
            if sentence["kind"] != "selling_point":
                original = timing_contract.split_sentences(manifest)
                if (not _hook_neighbor(manifest, original, sentence, {})
                        or plan.get("coverage", {}).get("display_pool", {}).get("exhausted")):
                    raise
                # The neighbor could not carry this line. Resume the ordinary display search
                # with its own duration, keeping other sentences' reviews and QC exclusions.
                duration, _ = timing_contract.duration_for(narration_timing, index, segment=original[index - 1])
                shared = {**sentence, "index": 0, "kind": "product_intro", "text": "同商品通用产品展示池",
                          "selling_points": [], "audio_durations_us": [duration]}
                previous = {r["shot_id"]: r for r in plan["display_reviews"]}
                reviews, coverage = _review_pool(manifest, shared, shots, directory, narration_timing or {},
                    existing_reviews=list(previous.values()), excluded_ids=rejected.get(index, set()))
                if not any(r["shot_id"] not in previous or r != previous[r["shot_id"]] for r in reviews):
                    raise
                previous.update((r["shot_id"], r) for r in reviews)
                plan["display_reviews"] = list(previous.values())
            else:
                if coverage.get("exhausted") or coverage.get("reason") != "KNOWN_WINDOWS_INFEASIBLE":
                    raise
                reviews, coverage = _review_pool(manifest, sentence, shots, directory, narration_timing or {},
                    exhaustive=True, existing_reviews=plan["reviews"].get(str(index), []),
                    excluded_ids=rejected.get(index, set()))
                plan["reviews"][str(index)] = reviews
            expanded_indices.add(index)
            plan['coverage'][str(index)] = coverage
            plan.setdefault('review_expansions', []).append({'index': index, 'reason': str(exc),
                                                            'checked': coverage['checked']})
            plan['_resume_rows'] = bool(plan.get('rows'))
            _save(directory / 'plan.json', plan)
            if not coverage.get('feasible'):
                for review in reviews + plan.get('display_reviews', []):
                    if failure_fields(str(review.get('error', ''))):
                        raise ValueError(review['error'])


def prepare(manifest: dict, analysis: dict, report_dir: Path, *, narration_timing: dict | None = None) -> dict:
    contract.playback.task_absorb_speed_cap(manifest)
    contract.max_source_reuse(manifest)
    directory = report_dir / "ai_editor"
    directory.mkdir(parents=True, exist_ok=True)
    plan: dict = {"schema": contract.PLAN_SCHEMA, "policy_version": contract.POLICY_VERSION,
                  "playback_policy_version": contract.playback.VERSION,
                  "reviews": {}, "display_reviews": [], "coverage": {}, "issues": []}
    try:
        shots = [s for s in analysis.get("shots", []) if isinstance(s, dict) and s.get("status") == "ready_for_matching"]
        if not shots or len({s["shot_id"] for s in shots}) != len(shots):
            raise ValueError("AI_EDITOR_NO_UNIQUE_CANDIDATES")
        by_id = {str(s["shot_id"]): s for s in shots}
        editor_model = _editor_model()
        plan["identity"] = {"model": editor_model,
            "manifest_sha256": contract.digest(manifest), "timing_sha256": contract.digest(narration_timing),
            "analysis_sha256": _analysis_digest(analysis),
            "policy_version": contract.POLICY_VERSION,
            "editorial_rules": contract.rules_identity(manifest)}
        # Output naming and recovery controls do not alter observed pixels.
        logical_input = {k: v for k, v in manifest.items() if k not in {
            "draft_name", "force_overwrite", "parent_run_id", "recovery_stage", "shot_analysis_file", "_selection_attempt",
            "qc_rejected_shots"}}
        plan["resume_identity"] = {**plan["identity"], "manifest_sha256": contract.digest(logical_input),
                                   "editorial_prompt_sha256": contract.digest(contract.policy_prompt(manifest))}
        try:
            previous = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        duration_resume = (previous.get("resume_identity") != plan["resume_identity"]
                           and _duration_resume_safe(previous, plan["resume_identity"], analysis, directory))
        identity_only_resume = False
        if duration_resume:
            old_digest = previous["resume_identity"]["analysis_sha256"]
            snapshot = json.loads((directory / f"previous_analysis_{old_digest}.json").read_text(encoding="utf-8"))
            identity_only_resume = _analysis_digest(snapshot) == _analysis_digest(analysis)
        if previous.get("resume_identity") == plan["resume_identity"] or duration_resume:
            for key in ("sentences", "reviews", "display_reviews", "coverage", "rows", "window_reviews",
                        "duration_migration", "duration_migrations",
                        "window_repair_attempts", "repair_history", "duplicate_repair_reviews", "continuity_reviews", "continuity_attempts", "continuity_repairs", "experiment_attempts"):
                if key in previous:
                    plan[key] = copy.deepcopy(previous[key])
            if not duration_resume or identity_only_resume:
                _restore_interrupted_groups(manifest, analysis, plan, previous, narration_timing)
            _clear_incomplete_groups(plan)
            plan["_resume_rows"] = bool(plan.get("rows"))
            if duration_resume:
                old_digest = previous["resume_identity"]["analysis_sha256"]
                migration = {"previous_analysis_sha256": old_digest,
                             "current_analysis_sha256": plan["resume_identity"]["analysis_sha256"],
                             "kind": "analysis_identity" if identity_only_resume else "material_duration",
                             "snapshot": f"previous_analysis_{old_digest}.json",
                             "snapshot_content_sha256": contract.digest(snapshot)}
                plan["duration_migrations"] = [*plan.get("duration_migrations", []), migration]
                plan["duration_migration"] = migration
            groups = {**plan["reviews"], "display_pool": plan["display_reviews"]}
            for key, reviews in groups.items():
                retained = [r for r in reviews if r.get("shot_id") in by_id
                            and r.get("source_identity") == contract.source_identity(by_id[r["shot_id"]]["video"])]
                if len(retained) != len(reviews):
                    plan.setdefault("invalidated_review_groups", []).append(key)
                    plan["coverage"].pop(key, None)
                    if key == "display_pool":
                        plan["display_reviews"] = retained
                    else:
                        plan["reviews"][key] = retained
        plan["phase"] = "selection_inventory"
        plan["attempt_run_id"] = manifest.get("_selection_attempt")
        _save(directory / "plan.json", plan)
        if not plan.get("sentences"):
            plan["sentences"] = _inventory(manifest, shots, directory)
        original = timing_contract.split_sentences(manifest)
        rejected = qc_rejected_shots(manifest)
        for sentence in plan["sentences"]:
            sentence["audio_durations_us"] = [timing_contract.duration_for(narration_timing,
                sentence["index"], segment=original[sentence["index"] - 1])[0]]
            if sentence["kind"] == "selling_point":
                key = str(sentence["index"])
                excluded = rejected.get(sentence["index"], set())
                if not excluded and (_cached_row_feasible(manifest, analysis, plan, sentence, narration_timing)
                        or next(_short_complete_rows(manifest, analysis, plan, sentence["index"], narration_timing), None)):
                    plan.setdefault("coverage", {}).setdefault(key, {}).update(
                        feasible=True, playback_policy_version=contract.playback.VERSION)
                    continue
                # Coverage is a timing decision, not a reusable visual fact.
                # Recompute it even for old successful plans, preserving reviews.
                if (not excluded and plan.get("coverage", {}).get(key, {}).get("feasible")
                        and _cached_reviews_feasible(manifest, sentence, by_id,
                                                     plan["reviews"].get(key, []), narration_timing or {})):
                    plan["coverage"][key]["playback_policy_version"] = contract.playback.VERSION
                    continue
                plan["phase"] = "selection_candidate_review"
                reviews, coverage = _review_pool(manifest, sentence, by_id, directory, narration_timing or {},
                                                 existing_reviews=plan["reviews"].get(key, []), excluded_ids=excluded,
                                                 early_window_repair=True)
                plan["reviews"][str(sentence["index"])] = reviews
                plan["coverage"][str(sentence["index"])] = coverage
                supported = {p for r in reviews if r["verdict"] == "matched" for p in r.get("supported_point_ids", [])}
                missing = [p for p in sentence["selling_points"] if p["id"] not in supported]
                if missing and not supported and coverage["exhausted"] and not coverage["uncertain"]:
                    plan.setdefault("material_gaps", []).append({"index": sentence["index"],
                        "text": sentence["text"], "selling_points": missing, "reason": "ALL_CANDIDATES_REVIEWED_NO_SUPPORT"})
                _save(directory / "plan.json", plan)
        displays = [s for s in plan["sentences"] if s["kind"] != "selling_point"]
        if displays:
            shared = {**displays[0], "index": 0, "text": "同商品通用产品展示池", "kind": "product_intro", "selling_points": []}
            timings = [timing_contract.duration_for(narration_timing, s["index"], segment=original[s["index"] - 1])
                       for s in displays]
            shared["audio_durations_us"] = [duration for duration, _ in timings]
            # A neutral line can borrow its neighbor's edge. Do not exhaust the display pool
            # looking for an independent cut of that exact length before the editor can try it.
            standalone = [duration for sentence, (duration, certainty) in zip(displays, timings)
                          if certainty == timing_contract.TIMING_CERTAINTY_ESTIMATED
                          or not _hook_neighbor(manifest, original, sentence, {})]
            shared["standalone_durations_us"] = standalone or [max(shared["audio_durations_us"])]
            plan["display_reviews"], plan["coverage"]["display_pool"] = _review_pool(manifest, shared, by_id, directory, narration_timing or {},
                                                                                       existing_reviews=plan["display_reviews"])
            for sentence in displays:
                excluded = rejected.get(sentence["index"], set())
                if not excluded or any(r.get("verdict") == "matched" and r.get("shot_id") not in excluded
                                       for r in plan["display_reviews"]):
                    continue
                seen = {r.get("shot_id") for r in plan["display_reviews"]}
                fresh = [sid for sid in sentence["candidate_ids"] if sid not in seen and sid not in excluded][:ROUND_SIZE]
                if fresh:
                    extra, _ = _review_pool(manifest, {**sentence, "candidate_ids": fresh}, by_id,
                        directory, narration_timing or {}, excluded_ids=excluded)
                    plan["display_reviews"].extend(extra)
        inventory = [{"index": s["index"], "text": s["text"], "kind": s["kind"],
            "selling_points": s["selling_points"], "audio_duration_us": timing_contract.duration_for(narration_timing, s["index"], segment=timing_contract.split_sentences(manifest)[s["index"] - 1])[0],
            "candidates": plan["reviews"].get(str(s["index"]), []) if s["kind"] == "selling_point" else plan["display_reviews"]} for s in plan["sentences"]]
        # Do not pass paths or old gate/score fields to the editor.
        for row in inventory:
            row["candidates"] = [{k: r[k] for k in ("shot_id", "basis", "observation", "reason", "confidence", "supported_point_ids", "evidence_intervals_us", "time_sensitive") if k in r}
                                 for r in row["candidates"] if r["verdict"] == "matched"
                                 and r["shot_id"] not in rejected.get(row["index"], set())]
            coverage = plan["coverage"].get(str(row["index"]) if row["kind"] == "selling_point" else "display_pool", {})
            if not coverage.get("feasible"):
                for review in plan["reviews"].get(str(row["index"]), []) + plan.get("display_reviews", []):
                    if failure_fields(str(review.get("error", ""))):
                        raise ValueError(review["error"])
                # Matched evidence that is currently too long still enters
                # placement: short crop + mild speed, or a sentence group.
                # Never trim the original evidence and relabel it as new.
                if coverage.get("reason") == "KNOWN_WINDOWS_INFEASIBLE" and row["candidates"]:
                    continue
                sentence = plan["sentences"][row["index"] - 1]
                if (row["kind"] in {"hook", "product_intro"} and not row["selling_points"]
                        and _hook_neighbor(manifest, original, sentence, {})):
                    continue  # An empty row still needs a reviewed adjoining cut in the edit.
                if any(row["index"] in {s["index"] for s in group}
                       for group in experiment_groups.groups(plan["sentences"])):
                    # Individual fragments may not certify the whole experiment.
                    # Its bounded joint review will still have to pass before assembly.
                    continue
                raise ValueError(f"AI_EDITOR_OBSERVATION_OR_WINDOW_PENDING:{row['index']}:{coverage.get('reason', 'UNKNOWN')}")
        plan["phase"] = "selection_edit"
        # Evidence search already stopped on a matched selling point. Edit and
        # local repair place it; timing conflict does not reopen the pool.
        _edit_with_review_fallback(manifest, analysis, plan, directory, narration_timing)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        plan["ok"] = False
        plan["failure"] = failure_fields(str(exc), stage=plan.get("phase", "semantic_match"))
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
        if identity.get("analysis_sha256") != _analysis_digest(analysis):
            raise ValueError("AI_EDITOR_ANALYSIS_CHANGED")
        current = copy.deepcopy(plan)
        result = _revalidate_rows(manifest, analysis, current, narration_timing=narration_timing)
        result["playback_policy_version"] = contract.playback.VERSION
        result["playback_repairs"] = current.get("playback_repairs", [])
        result["continuity_repairs"] = current.get("continuity_repairs", [])
        return result
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return {"ok": False, "selection_mode": "ai_editor", "segments": [], "manifest": dict(manifest),
                "pending_items": plan.get("issues") or [{"type": "AI_EDITOR_UNCERTIFIED", "message": str(exc)}],
                "recommendations": [], "degraded_matches": [], "material_gaps": plan.get("material_gaps", []),
                "material_gap_count": len(plan.get("material_gaps", [])),
                **plan.get("failure", {}), **failure_fields(str(exc))}


def _heuristic_sentence(index: int, text: str) -> dict:
    """Keep explicit clauses as separate possible points without inventing claims."""
    value = str(text or "").strip()
    parts = [part.strip() for part in re.split(r"[，,；;、]|以及|还有|并且|同时|和", value) if part.strip()]
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


def _observed_text_shortlist(text: str, shots: dict, *, limit: int = 12) -> list[str]:
    """Retrieve candidates only; descriptions never certify a visual match.

    Prefer rare overlapping Chinese phrases, so a specific object such as a
    paper holder is not buried behind hundreds of generic packaging clips.
    The caller keeps the full pool for the pixel-review fallback.
    """
    def terms(value: str) -> set[str]:
        result = set()
        for word in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", value.lower()):
            if "\u4e00" <= word[0] <= "\u9fff":
                for size in (2, 3):
                    result.update(word[i:i + size] for i in range(len(word) - size + 1))
            elif len(word) > 1:
                result.add(word)
        return result

    query = terms(text)
    if not query or limit <= 0:
        return []
    fields = ("description", "visual_description", "visual_tags", "action",
              "object", "result", "action_segments", "text_reads")
    rows = []
    frequency = Counter()
    for sid, shot in shots.items():
        observed = " ".join(str(shot.get(key) or "") for key in fields)
        words = terms(observed) & query
        rows.append((sid, words))
        frequency.update(words)
    size = len(rows)
    ranked = []
    for sid, words in rows:
        score = math.fsum(math.log((size + 1) / (frequency[word] + 1)) + 1 for word in words)
        if score > 0:
            ranked.append((sid, score))
    ranked.sort(key=lambda row: -row[1])  # stable ties keep source order
    return [sid for sid, _ in ranked[:limit]]


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
    usage = _prior_use_by_shot(manifest, shots.values())
    task = manifest.get("record_id") or manifest.get("ledger_task") or manifest.get("task_id")
    # Repeated text can identify several distinct narration positions. Keep
    # every candidate position and validate any explicit index before reusing
    # an existing selection; do not silently overwrite an earlier sentence.
    script_texts = ["".join(timing_contract.sentence_text(s).split()) for s in original]
    script_indices = {}
    for i, text in enumerate(script_texts, 1):
        script_indices.setdefault(text, []).append(i)
    segments = matched.get("segments", [])
    by_index = {}
    for enum_i, segment in enumerate(segments, 1):
        row = copy.deepcopy(segment)
        text = "".join(str(row.get("text") or row.get("claim") or "").split())
        explicit = row.get("index")
        if isinstance(explicit, str) and explicit.isdecimal():
            explicit = int(explicit)
        index = None
        if explicit is not None:
            if (type(explicit) is int and 1 <= explicit <= len(original)
                    and (not text or script_texts[explicit - 1] == text)):
                index = explicit
        elif len(script_indices.get(text, [])) == 1 and text:
            index = script_indices[text][0]
        elif (len(segments) == len(original) and enum_i <= len(original)
              and text and script_texts[enum_i - 1] == text):
            index = enum_i
        if index is None or index in by_index:
            reason = (f"AI_REFILL_SEGMENT_INDEX_DUPLICATE:{index}" if index in by_index
                      else f"AI_REFILL_SEGMENT_IDENTITY_AMBIGUOUS:{enum_i}")
            return {**matched, "ok": False, "refill_indices": wanted, "refill_replaced": [],
                    "refill_errors": [reason], "pending_items": [*(matched.get("pending_items") or []),
                        {"type": "AI_EDITOR_UNCERTIFIED", "message": reason}]}
        by_index[index] = row
    # A reviewed sentence-continuity group is one allocation of one source
    # across its members. Replacing any member releases the binding for all of
    # them and re-plans each, instead of leaving neighbours bound to a group
    # that no longer exists (CONTINUITY_GROUP_INCOMPLETE).
    from ..policy import continuity as continuity_policy
    expanded = set(wanted)
    for index in wanted:
        group = (by_index.get(index) or {}).get("continuity_group")
        if isinstance(group, dict) and not continuity_policy.is_experiment(group):
            for member in (group.get("input") or {}).get("members") or []:
                if isinstance(member, dict) and type(member.get("index")) is int and 1 <= member["index"] <= len(original):
                    expanded.add(member["index"])
    wanted = sorted(expanded)
    directory = report_dir / "ai_editor_refill"
    directory.mkdir(parents=True, exist_ok=True)
    grouped_replaced, grouped_errors = experiment_groups.refill_groups(
        manifest, analysis, by_index, wanted, directory, narration_timing)
    reserved = {str(part.get("shot_id") or part.get("temporary_shot_id"))
                for index, row in by_index.items() if index not in wanted
                for part in (row.get("span_parts") or [row])}
    replaced, errors, refill_unresolved = list(grouped_replaced), list(grouped_errors.values()), []
    reuse_limit = contract.source_reuse_limit(manifest)

    def retained_rows():
        return [{**row, "_source_reuse_index": i} for i, row in sorted(by_index.items()) if (i not in wanted or i in replaced)
                and not row.get("visual_missing") and (row.get("video") or row.get("span_parts"))]

    for index in wanted:
        if index in grouped_replaced or index in grouped_errors:
            continue
        if index > len(original):
            errors.append(f"AI_REFILL_INDEX_OOB:{index}")
            continue
        sentence = _heuristic_sentence(index, timing_contract.sentence_text(original[index - 1]))
        audio_us, certainty = timing_contract.duration_for(narration_timing, index,
                                                          segment=original[index - 1])
        excluded = qc_rejected_shots(manifest).get(index, set())
        retained = retained_rows()
        allowed = {sid: shot for sid, shot in shots.items() if sid not in reserved and sid not in excluded
                   and not contract.source_reuse_conflict_details(
                       retained + [{"video": shot.get("video")}], reuse_limit)}
        preferred = [sid for sid in _rule_shortlist_ids(matched, analysis, index, limit=max(ROUND_SIZE, 16))
                     if sid in allowed]
        observed = _observed_text_shortlist(sentence["text"], allowed)
        feedbacks = manifest.get("ai_refill_feedback")
        feedback = feedbacks.get(str(index)) if isinstance(feedbacks, dict) else None
        recalled, previous, window_hints, recut_id = [], set(), {}, None
        if (allowed and isinstance(feedback, dict) and feedback.get("claim") == sentence["text"]
                and all(isinstance(feedback.get(k), str) and feedback[k].strip() for k in ("observed", "reason"))):
            sentence["qc_feedback"] = copy.deepcopy(feedback)
            previous = {str(sid) for sid in feedback.get("prior_shot_ids") or []}
            # Existing catalog inventory is retrieval only; all returned IDs
            # still pass independent image review and the usual evidence gates.
            hint = json.dumps({"old_qc_observed": feedback["observed"], "old_qc_reason": feedback["reason"]}, ensure_ascii=False)
            retrieval_manifest = {**manifest, "full_script": sentence["text"],
                "segments": [{"text": sentence["text"], "visual_requirements": hint}],
                "revision_feedback": (str(manifest.get("revision_feedback") or "") +
                    "\n本句旧稿质检仅供定位待核查缺口，可能有误；与生效业务规则冲突时不要采纳额外要求：" + hint)}
            recut = feedback.get("same_shot_recut")
            if isinstance(recut, dict) and recut.get("shot_id") in previous and recut.get("shot_id") in allowed:
                target = allowed[recut["shot_id"]]
                try:
                    a, b = float(recut["start"]), float(recut["end"])
                    if (math.isfinite(a + b) and float(target["source_start"]) <= a < b <= float(target["source_end"])
                            and any(isinstance(s, dict) and float(s["start"]) < b and float(s["end"]) > a
                                    for s in target.get("action_segments") or [])):
                        recut_id = recut["shot_id"]
                except (ValueError, TypeError, KeyError, OverflowError):
                    pass
            try:
                inventory = ([{"candidate_ids": [recut_id], "candidate_windows": [recut]}] if recut_id else
                    _inventory(retrieval_manifest, list(allowed.values()), directory / f"qc_recall_{index:02d}"))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                errors.append(str(exc))
                continue
            recalled = inventory[0].get("candidate_ids") or []
            for hint in inventory[0].get("candidate_windows") or []:
                if not isinstance(hint, dict) or hint.get("shot_id") not in allowed:
                    continue
                sid = hint["shot_id"]
                shot = allowed[sid]
                try:
                    start, end = float(hint["start"]), float(hint["end"])
                    lo, hi = float(shot["source_start"]), float(shot["source_end"])
                    segments = shot.get("action_segments") or []
                    overlaps = any(isinstance(a, dict) and float(a["start"]) < end and float(a["end"]) > start
                                   for a in segments)
                    if not (math.isfinite(start) and math.isfinite(end) and lo <= start < end <= hi and overlaps):
                        continue
                    # A hint identifies the action, not the editable source
                    # boundary. Include enough surrounding footage for the
                    # frozen narration and normal head/tail guards when possible.
                    from . import shot_analyzer
                    head = max(0.0, shot_analyzer._number(manifest.get("head_trim_s"), shot_analyzer.DEFAULT_HEAD_TRIM_S))
                    tail = max(0.0, shot_analyzer._number(manifest.get("tail_trim_s"), shot_analyzer.DEFAULT_TAIL_TRIM_S))
                    pad = max(0.0, shot_analyzer._number(manifest.get("tail_pad_s"), 0.0))
                    desired = max(end - start + .5, audio_us / 1_000_000 + head + tail + pad)
                    window_end = min(hi, end + .15)
                    window_start = max(lo, min(start - .35, window_end - desired))
                    window_end = min(hi, max(window_end, window_start + desired))
                    window_hints[sid] = {**shot, "source_start": window_start, "source_end": window_end}
                    if sid == recut_id:
                        window_hints[sid]["_review_focus_window"] = [start, end]
                except (ValueError, TypeError, KeyError, OverflowError):
                    continue
        # Give both retrieval signals room in the first review round. Existing
        # QC feedback remains first, and none of these hints certify a shot.
        interleaved = [group[i] for i in range(ROUND_SIZE)
                       for group in (preferred, observed) if i < len(group)]
        ranked = [sid for sid in dict.fromkeys(
            [*recalled, *interleaved, *preferred, *observed, *shots]) if sid in allowed]
        first, rest = ranked[:ROUND_SIZE], ranked[ROUND_SIZE:]
        sentence["candidate_ids"] = ([sid for sid in first if sid not in previous] +
                                     [sid for sid in first if sid in previous] + rest)
        sentence["audio_durations_us"] = [audio_us]
        try:
            if not sentence["candidate_ids"]:
                raise ValueError(f"AI_REFILL_NO_UNIQUE_CANDIDATES:{index}")
            # An explicit QC repair must seek all points before considering a
            # preview fallback; otherwise the first partial match ends review.
            explicit = any(str(raw).strip() == str(index) for raw in (manifest.get("ai_refill_indices") or []))
            review_manifest = ({**manifest, "allow_partial_selling_points": False}
                               if explicit and len(sentence["selling_points"]) > 1 else manifest)
            review_shots = {**shots, **window_hints}
            if recut_id in window_hints:
                # Re-look at the actual omitted source interval first. A hint
                # neither certifies the action nor forces it into the final cut.
                focused = {**sentence, "candidate_ids": [recut_id]}
                reviews, coverage = _review_pool(review_manifest, focused, review_shots, directory, narration_timing or {})
                alternatives = [sid for sid in sentence["candidate_ids"] if sid != recut_id]
                if not coverage.get("feasible") and alternatives:
                    alternate_reviews, alternate_coverage = _review_pool(
                        review_manifest, {**sentence, "candidate_ids": alternatives},
                        review_shots, directory, narration_timing or {})
                    # A confirmed same-shot action may still need the existing
                    # short-window/retiming placement repair. Do not discard it
                    # just because the alternative pool has no usable match.
                    if alternate_coverage.get("feasible") or not any(r.get("verdict") == "matched" for r in reviews):
                        reviews, coverage = alternate_reviews, alternate_coverage
            else:
                reviews, coverage = _review_pool(review_manifest, sentence, review_shots, directory, narration_timing or {})
            good = [r for r in reviews if r.get("verdict") == "matched"
                    and r.get("shot_id") in allowed]
            partial = manifest.get("delivery_mode") == "preview" and manifest.get("allow_partial_selling_points") is True
            if not good or (not coverage.get("feasible") and not partial
                            and coverage.get("reason") != "KNOWN_WINDOWS_INFEASIBLE"):
                raise ValueError(f"AI_REFILL_NO_MATCH:{index}")
            mini_manifest = {k: v for k, v in manifest.items() if k not in ("segments", "full_script", "script")}
            # Keep this sentence's real audio and other input attributes. The
            # one-sentence assembly replaces only its visual selection.
            mini_manifest.update(full_script=sentence["text"], selection_mode="ai_editor",
                                 segments=[copy.deepcopy(original[index - 1])],
                                 qc_rejected_shots={"1": sorted(excluded)})
            timing_row = {"key": "s1", "text": sentence["text"], "audio_duration_us": audio_us,
                          "timing_certainty": certainty}
            if narration_timing and index <= len(narration_timing.get("rows", [])):
                timing_row = {**narration_timing["rows"][index - 1], **timing_row}
            mini_timing = {**(narration_timing or {}), "rows": [timing_row]}
            last_error = f"AI_REFILL_WINDOW_INFEASIBLE:{index}"
            from . import shot_analyzer
            required = {p["id"] for p in sentence["selling_points"]}
            point_order = {p["id"]: i for i, p in enumerate(sentence["selling_points"])}
            def candidate_order(review):
                solution = shot_analyzer.candidate_window(_observed_shot(shots[review["shot_id"]], review),
                    audio_duration_us=audio_us, manifest=manifest, time_sensitive=review["time_sensitive"])
                rank = (0 if solution.speed <= contract.playback.PROCESS_PREFERRED_MAX else 1) if solution.ok else 2
                sid = review["shot_id"]
                seed = contract.digest([task, index, shots[sid]["video"]]) if task else ""
                return rank, -float(review.get("confidence", 0)), usage[sid], seed
            ordered_good = sorted(good, key=candidate_order)
            # Singles stay first; multi-shot combinations are considered only
            # when their real reviewed evidence covers the whole sentence.
            def candidate_groups():
                for size in range(1, min(len(ordered_good), int(manifest.get("span_max_parts", 3))) + 1):
                    for group in combinations(ordered_good, size):
                        covered = {p for review in group for p in review.get("supported_point_ids", [])}
                        if required <= covered:
                            yield group
                if partial:
                    yield from ((r,) for r in ordered_good if not required <= set(r.get("supported_point_ids", [])))
            for group in candidate_groups():
                def point_span(review):
                    positions = [point_order[p] for p in review.get("supported_point_ids", []) if p in point_order]
                    return (min(positions), max(positions)) if positions else (len(point_order), len(point_order))
                group = tuple(sorted(group, key=point_span))
                # A point first shown after a later point would reverse the
                # spoken progression even if the union covers every claim.
                assignment = [next((i for i, review in enumerate(group)
                                    if point["id"] in review.get("supported_point_ids", [])), None)
                              for point in sentence["selling_points"]]
                present = [i for i in assignment if i is not None]
                if present != sorted(present):
                    continue
                try:
                    allocations = _reviewed_allocations(manifest, shots, group, audio_us)
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    last_error = str(exc)
                    continue
                if allocations is None:
                    if len(group) != 1:
                        continue
                    allocations = [audio_us]
                plan = {"schema": contract.PLAN_SCHEMA, "policy_version": contract.POLICY_VERSION,
                        "sentences": [{**sentence, "index": 1}], "reviews": {"1": reviews},
                        "display_reviews": [], "rows": [{"index": 1, "parts": [
                            {"shot_id": review["shot_id"], "audio_duration_us": duration}
                            for review, duration in zip(group, allocations)],
                            "reason": f"rule_then_ai refill for S{index:02d}"}],
                        "identity": {"model": _editor_model(),
                                     "editorial_rules": contract.rules_identity(manifest)}, "ok": True}
                try:
                    try:
                        assembled = _assemble(mini_manifest, analysis, plan, narration_timing=mini_timing,
                                              verify_sources=True)
                    except WindowFailure:
                        assembled = _repair_reviewed_rows(mini_manifest, analysis, plan, directory,
                                                          mini_timing, set())
                    row = _reindex_ai_segment(assembled["segments"][0], index)
                    checked = contract.validate_segment_receipt(row, index=index, verify_source=True,
                                                                 manifest=manifest)
                    if not checked["ok"]:
                        raise ValueError(";".join(checked["reasons"]))
                    reuse_errors = contract.source_reuse_conflict_details(
                        retained + [{**row, "_source_reuse_index": index}], reuse_limit)
                    if reuse_errors:
                        raise ValueError(reuse_errors[0][0])
                    by_index[index] = row
                    reserved.update(str(review["shot_id"]) for review in group)
                    replaced.append(index)
                    if row.get("uncovered_selling_points"):
                        refill_unresolved.append({"index": index, "text": sentence["text"],
                            "type": "PARTIAL_SELLING_POINTS",
                            "missing_selling_points": row["uncovered_selling_points"]})
                    _save(directory / f"refill_{index:02d}.json", {
                        "index": index, "shot_ids": [review["shot_id"] for review in group], "coverage": coverage})
                    break
                except (ValueError, KeyError, TypeError, OSError) as exc:
                    last_error = str(exc)
            else:
                raise ValueError(last_error)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            errors.append(str(exc))
    # First-run TTS selections may not carry an audio field. During a QC
    # refill the frozen input supplies each sentence's original audio; bind it
    # to every retained row as well as the newly selected rows.
    ordered = []
    for i in sorted(by_index):
        row = by_index[i]
        source = original[i - 1] if 1 <= i <= len(original) else None
        audio = str(source.get("audio") or "") if isinstance(source, dict) else ""
        if audio:
            existing = str(row.get("audio") or "")
            if existing and Path(existing).resolve() != Path(audio).resolve():
                errors.append(f"AI_REFILL_AUDIO_BINDING_CHANGED:{i}")
            else:
                duration = source.get("audio_duration_us")
                existing_duration = row.get("audio_duration_us")
                if (type(duration) is not int or duration <= 0 or
                        (existing_duration is not None and existing_duration != duration)):
                    errors.append(f"AI_REFILL_AUDIO_TIMING_CHANGED:{i}")
                else:
                    row = {**row, "audio": audio, "audio_duration_us": duration}
                    if source.get("timing_certainty"):
                        row["timing_certainty"] = source["timing_certainty"]
        ordered.append(row)
    reuse_errors = contract.source_reuse_conflict_details(
        [{**row, "_source_reuse_index": i} for i, row in sorted(by_index.items())
         if not row.get("visual_missing") and (row.get("video") or row.get("span_parts"))],
        reuse_limit)
    errors.extend(detail[0] for detail in reuse_errors if detail[0] not in errors)
    sequence_errors = contract.sequence_errors_all(ordered, ai_only=True)
    errors.extend(error for error in sequence_errors if error not in errors)
    def keep(row):
        return not isinstance(row, dict) or row.get("index") not in replaced
    output_manifest = dict(matched.get("manifest") or manifest)
    # Reuse the selected visual rows, not the prior run's writer controls.
    # In particular a refill must never write into the previous draft name.
    for key in ("draft_name", "force_overwrite", "delivery_mode", "parent_run_id",
                "recovery_stage", "run_id", "task_id", "_selection_attempt",
                "variant_index", "variant_count", "ai_refill_indices", "ai_refill_feedback", "qc_rejected_shots"):
        output_manifest.pop(key, None)
        if key in manifest:
            output_manifest[key] = copy.deepcopy(manifest[key])
    all_ai = bool(ordered) and all(isinstance(row, dict)
        and row.get("selection_mode") == "ai_editor" and isinstance(row.get("ai_selection"), dict)
        for row in ordered)
    result_mode = "ai_editor" if all_ai else "rule_then_ai"
    output_manifest.update(selection_mode=result_mode, segments=ordered)
    result = {**matched, "selection_mode": result_mode, "selection_policy": result_mode + "_refill",
              "segments": ordered, "refill_indices": wanted, "refill_replaced": replaced,
              "refill_errors": errors, "manifest": output_manifest}
    for field in ("pending_items", "material_gaps", "degraded_matches", "visual_missing_matches", "preview_unresolved"):
        result[field] = [row for row in matched.get(field, []) if keep(row)]
    result["preview_unresolved"].extend(refill_unresolved)
    result["pending_items"].extend({"type": "AI_EDITOR_UNCERTIFIED", "message": error} for error in errors)
    if errors:
        result.update(failure_fields(";".join(errors)))
    result["ok"] = (bool(ordered) and len(ordered) == len(original) and not result["pending_items"]
                    and set(wanted) <= set(replaced) and not sequence_errors)
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
        coverage = {"mode": result_mode,
            "certification": "UNCERTIFIED" if outstanding else ("CERTIFIED" if all_ai else "MIXED_RULE_AND_AI_REFILL"),
            "validation_passed": not bool(outstanding), "uncertified_reasons": outstanding,
            "scope": "independent_qc_required"}
        result["claim_gate_coverage"] = coverage
        result["manifest"]["claim_gate_coverage"] = coverage
        result["manifest"]["preview_unresolved"] = result["preview_unresolved"]
        result["manifest"]["visual_missing_matches"] = result["visual_missing_matches"]
        result["accepted_shots"] = [p.get("shot_id") or p.get("temporary_shot_id")
            for row in ordered for p in (row.get("span_parts") or [row])]
    return result
