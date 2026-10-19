"""Whole-script planning from verified observations, with independent slice review.

The injected model contract is ``model(request: dict) -> dict | str``.  Requests
carry a short JSON-only prompt, structured payload, images, stage and output
path.  A transport adapter can therefore use the normal vision_analyzer route;
offline tests inject a callable and never need a provider or media installation.
This module authorizes local construction only, never formal delivery.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import math
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

from . import contextual_plan as cp

US = 1_000_000
Model = Callable[[dict], dict | str]


class IndexedPlanError(cp.ContextualPlanError):
    pass


def _fail(code: str, message: str, **details: Any) -> None:
    raise IndexedPlanError(message, code, **details)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _write(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _json(value) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return cp._hash(path)


def _path(value: Any, base: Path) -> Path:
    return cp._path(value, base)


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail("INVALID_TIMING", f"{name} must be integer >= {minimum}")
    return value


def _time(row: dict, key: str, default: int | None = None) -> int:
    if key + "_us" in row:
        return _integer(row[key + "_us"], key + "_us")
    if key in row:
        value = float(row[key])
        if math.isfinite(value) and value >= 0:
            return round(value * US)
    if default is not None:
        return default
    _fail("INVALID_TIMING", f"missing {key}_us")


def _frozen(manifest: dict, base: Path) -> list[dict]:
    originals = manifest.get("segments")
    if not isinstance(originals, list) or not originals:
        _fail("FROZEN_NARRATION_REQUIRED", "nonempty frozen segments required")
    result = []
    for index, original in enumerate(originals, 1):
        if not isinstance(original, dict) or not str(original.get("text") or "").strip():
            _fail("FROZEN_NARRATION_REQUIRED", "sentence text missing", index=index)
        if original.get("multi_shot") or original.get("span_parts"):
            _fail("FROZEN_NARRATION_REQUIRED", "use frozen sentences before visual composition", index=index)
        audio = _path(original.get("audio"), base)
        if not audio.is_file() or not audio.stat().st_size:
            _fail("FROZEN_AUDIO_MISSING", "frozen audio missing", index=index)
        duration = _integer(original.get("audio_duration_us"), "audio_duration_us", 1)
        result.append({"index": index, "text": original["text"], "audio": str(audio),
                       "audio_sha256": cp._hash(audio), "audio_duration_us": duration})
    return result


def _validate_groups(value: Any, count: int) -> list[dict]:
    if not isinstance(value, list) or not value:
        _fail("INVALID_SCRIPT_GROUPS", "semantic groups required")
    cursor, ids, groups = 1, set(), []
    for row in value:
        if not isinstance(row, dict):
            _fail("INVALID_SCRIPT_GROUPS", "invalid semantic group")
        gid, members = row.get("id"), row.get("members")
        if (not isinstance(gid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", gid)
                or gid in ids or not isinstance(members, list) or not members
                or any(type(i) is not int for i in members)
                or members != list(range(cursor, cursor + len(members)))
                or members[-1] > count or type(row.get("require_complete_action")) is not bool
                or not str(row.get("query") or "").strip()):
            _fail("INVALID_SCRIPT_GROUPS", "groups must preserve contiguous sentence coverage", group_id=gid)
        ids.add(gid)
        required = row.get("required_requirements", [])
        if (not isinstance(required, list) or any(not isinstance(r, str) or not r.strip() for r in required)
                or len(set(required)) != len(required)):
            _fail("INVALID_SCRIPT_GROUPS", "required_requirements must contain unique requirement ids", group_id=gid)
        groups.append(copy.deepcopy(row))
        cursor += len(members)
    if cursor != count + 1:
        _fail("INVALID_SCRIPT_GROUPS", "every frozen sentence must belong to one group")
    return groups


PROMPTS = {
    "group": """理解整篇口播后，结合available_observations里真实存在的动作划分连续语义组。原文、句序、音频时长不变。每组最终只能使用一个原片中的连续窗口，因此拆封取出、湿敷、擦脸等不同动作应在自然转折处分组；说明性相关句可跨句延续同一动作，不能每个短句重起手。不要把拆包装和另一原片上的敷脸硬绑成一组。明确动作或效果句require_complete_action=true；包装不能替代功能。叙述、情绪、承接、未给具体数字的促销价可用同商品的相关画面，不需虚构皮肤问题实拍或凭空要求价签；不能把文案没要求的促销图、提醒手势等加成素材硬要求。required_requirements只能选required_visual_checks提供的id，未提供则必须[]。query描述本组实际视觉目标，retrieval_query用不超过30个汉字简述对象和动作供检索，不写长叙述。只输出JSON：{\"groups\":[{\"id\":\"g1\",\"members\":[1,2],\"query\":\"具体视觉目标\",\"retrieval_query\":\"生理盐水湿巾拆封抽出\",\"require_complete_action\":true,\"required_requirements\":[]}]}。members连续按序，每句恰好一次。""",
    "plan": """为当前句组选择一个已核验候选及连续动作窗口。结合完整口播和前后组，不能改文案/音频/句序。locked_visual_windows是已锁定前组，优先避免重复同一源窗起手，不能以不相关画面换取差异。只能选择提供的 candidate_id，source_start_us/source_end_us 在候选范围内；普通承接/情绪/无具体数字促销句可延续同商品相关画面，不新增价签或手势要求；完整动作必须覆盖候选已观察的起手和结果。源窗长度/句组时长得到倍速，必须在 speed_range 内。包装、透光、拿纸不能证明吸水或擦净；缺素材则输出 gap，不准补包装/黑屏。只输出 JSON：{\"candidate_id\":\"...\",\"source_start_us\":0,\"source_end_us\":4000000,\"reason\":\"基于可见事实\"}；无解输出 {\"gap\":\"具体缺口\"}。""",
    "repair": """仅修复提供的失败句组，其他组已锁定不可改。遵守 plan 输入契约，只选择已核验候选、合法源窗和倍速。认真处理独立审核指出的语义/动作/结果/速度问题；透光不等于吸水，拿纸不等于擦净。无解输出 gap，不准改文案、重跑整稿、用包装或黑屏补位。只输出 {\"candidate_id\":\"...\",\"source_start_us\":0,\"source_end_us\":4000000,\"reason\":\"修复依据\"} 或 {\"gap\":\"具体缺口\"}。""",
    "observe": """你是独立切片观察员。不要信任选镜理由或标签。图片来自已按实际源窗和倍速渲染的连续句组；根据时间顺序、口播、前后文判断实际可见对象、动作、结果是否对应。包装不能证明功能，透光不能证明吸水，动作后定住不能冒充全过程。查看是否重复起手、跳过动作、截断结果，以及此倍速是否仍能看明白；证据不足填 uncertain，不猜测。完整动作需能看到开始与结果。按口播实际要求审核，不附加原文没有的准备步骤或主观效果验证：湿敷动作的可见结果是巾体贴合稳定，不要求额外泡水或皮肤疾病消失；但若画面开头已经贴好而没看到开始，仍是不完整。擦脸清洁必须可见擦脸动作，明确污渍擦净/吸水实验仍须看到前后过程与结果。已有require_complete_action=true时不能因口播是介绍就用not_required忽略动作完整性。只输出 JSON：{\"status\":\"complete|not_required|unsupported|uncertain\",\"observation\":\"实际看见的内容\",\"action_complete\":true,\"visible_at_speed\":true,\"issues\":[],\"observations\":[{\"index\":1,\"status\":\"supported|contextual|unsupported|uncertain\",\"observation\":\"实际画面与该句关系\"}]}。每个成员必须一项；无法凭采样判断动感时保持 uncertain。""",
}


def _call(stage: str, payload: dict, report: Path, model: Model | None,
          *, images: list[dict] | None = None, stats: dict | None = None) -> dict:
    images = images or []
    identity = {"stage": stage, "payload": payload, "prompt": PROMPTS[stage],
                "images": [{"sha256": i["sha256"], "timestamp_us": i.get("timestamp_us")}
                           for i in images]}
    key = _digest(identity)
    output = report / "model" / f"{stage}_{key}.json"
    if output.is_file():
        record = json.loads(output.read_text(encoding="utf-8-sig"))
        if (record.get("input_sha256") == key and isinstance(record.get("response"), dict)
                and record.get("response_sha256") == _digest(record["response"])):
            if stats is not None:
                stats["model_cache_hits"] += 1
            return {**record["response"], "_response_file": str(output),
                    "_response_sha256": cp._hash(output)}
    if model is None:
        _fail("MODEL_REQUIRED", "no model transport supplied and no matching receipt", stage=stage)
    output.parent.mkdir(parents=True, exist_ok=True)
    request = {"stage": stage, "prompt": PROMPTS[stage], "payload": payload,
               "images": copy.deepcopy(images), "output": str(output.with_suffix(".raw.json")),
               "failed_groups": payload.get("failed_groups", [])}
    _write(output.with_suffix(".request.json"), request)
    if stats is not None:
        stats["model_calls"] += 1
        stats[stage + "_calls"] = stats.get(stage + "_calls", 0) + 1
    try:
        response = model(request)
        if isinstance(response, str):
            from .json_extract import iter_json_values
            response = next((r for r in iter_json_values(response) if isinstance(r, dict)), None)
        if not isinstance(response, dict):
            _fail("MODEL_RESPONSE_INVALID", "model did not return a JSON object", stage=stage)
    except IndexedPlanError:
        raise
    except Exception as exc:
        _write(output.with_suffix(".failure.json"), {"stage": stage, "error": str(exc),
                                                     "formal_delivery_allowed": False})
        _fail("MODEL_CALL_FAILED", str(exc), stage=stage)
    response = {k: v for k, v in response.items() if not str(k).startswith("_")}
    digest = _write(output, {"input_sha256": key, "stage": stage, "response": response,
                             "response_sha256": _digest(response)})
    return {**response, "_response_file": str(output), "_response_sha256": digest}


def group_script(manifest: dict, report_dir: Path, model: Model | None = None,
                 *, base_dir: Path | None = None, stats: dict | None = None) -> list[dict]:
    """Create stable groups before retrieval; reuse explicit indexed_groups."""
    base, report = Path(base_dir or Path.cwd()), Path(report_dir)
    frozen = _frozen(manifest, base)
    if manifest.get("indexed_groups"):
        return _validate_groups(manifest["indexed_groups"], len(frozen))
    response = _call("group", {"script": [r["text"] for r in frozen],
                     "required_visual_checks": manifest.get("indexed_required_requirements", []),
                     "available_observations": manifest.get("indexed_available_observations", []),
                     "segments": [{"index": r["index"], "text": r["text"],
                                   "audio_duration_us": r["audio_duration_us"]} for r in frozen]},
                     report, model, stats=stats)
    groups = _validate_groups(response.get("groups"), len(frozen))
    checks = manifest.get("indexed_required_requirements", [])
    allowed = {str(c.get("id")) if isinstance(c, dict) else str(c) for c in checks}
    unknown = sorted({r for g in groups for r in g.get("required_requirements", [])} - allowed)
    if unknown:
        _fail("UNDECLARED_VISUAL_REQUIREMENT", "group invented checks absent from task requirements", requirements=unknown)
    return groups


def _candidates(rows: list[dict], base: Path) -> tuple[list[dict], list[dict]]:
    result, rejected, ids = [], [], set()
    # Multiple observed windows of one source share its media and often its
    # evidence frames. Hash each physical path once during this validation.
    hashes: dict[str, str] = {}
    def digest_file(path: Path) -> str:
        key = cp._identity(path)
        if key not in hashes:
            hashes[key] = cp._hash(path)
        return hashes[key]
    for index, row in enumerate(rows):
        try:
            if not isinstance(row, dict):
                _fail("CANDIDATE_NOT_VERIFIED", "candidate must be an object")
            cid = str(row.get("candidate_id") or row.get("shot_id") or row.get("id")
                      or row.get("material_id") or "")
            verified = row.get("verification_status") in {"verified", "human_verified"} or row.get("verified") is True
            provenance = row.get("provenance") or row.get("observation_provenance")
            if not verified or not provenance:
                _fail("CANDIDATE_NOT_VERIFIED", "explicit observation verification and provenance required")
            video = _path(row.get("video"), base)
            start, end = _time(row, "source_start"), _time(row, "source_end")
            observation = str(row.get("observation") or row.get("description") or "").strip()
            if not cid or cid in ids or not observation or end <= start:
                _fail("CANDIDATE_INVALID", "candidate id, observation or source aperture invalid")
            if not video.is_file() or not video.stat().st_size:
                _fail("CANDIDATE_MEDIA_MISSING", "candidate video missing")
            video_digest = digest_file(video)
            if row.get("video_sha256") and row["video_sha256"] != video_digest:
                _fail("CANDIDATE_MEDIA_CHANGED", "candidate video hash mismatch")
            evidence = []
            for frame in row.get("evidence") or []:
                path = _path(frame.get("path"), base)
                stamp = _time(frame, "timestamp")
                if not path.is_file() or not path.stat().st_size or not start <= stamp <= end:
                    _fail("CANDIDATE_EVIDENCE_INVALID", "candidate observation evidence unavailable/outside source")
                digest = digest_file(path)
                if frame.get("sha256") and frame["sha256"] != digest:
                    _fail("CANDIDATE_EVIDENCE_CHANGED", "candidate observation evidence changed")
                evidence.append({**frame, "path": str(path), "sha256": digest, "timestamp_us": stamp})
            if not evidence:
                _fail("CANDIDATE_EVIDENCE_REQUIRED", "candidate observation needs bound evidence files")
            ids.add(cid)
            result.append({**copy.deepcopy(row), "candidate_id": cid, "video": str(video),
                           "video_sha256": video_digest, "source_start_us": start,
                           "source_end_us": end, "description": observation, "evidence": evidence})
        except (OSError, ValueError, TypeError) as exc:
            rejected.append({"candidate_index": index, **(exc.as_dict() if isinstance(exc, IndexedPlanError)
                                                         else {"type": "CANDIDATE_INVALID", "message": str(exc)})})
    return result, rejected


def _material_identity(candidate: dict, base: Path) -> tuple[str, str]:
    material_id = str(candidate.get("material_id") or "").strip()
    if material_id:
        return "material_id", material_id
    video_sha = str(candidate.get("video_sha256") or "").strip().lower()
    if video_sha:
        return "video_sha256", video_sha
    if candidate.get("video"):
        return "video_path", cp._identity(_path(candidate["video"], base))
    return "candidate_id", str(candidate.get("candidate_id") or "")


def _limit_materials(candidates: list[dict], base: Path, maximum: int = 16) -> tuple[list[dict], int]:
    """Limit distinct sources while retaining every verified window of them."""
    identities: set[tuple[str, str]] = set()
    selected = []
    for candidate in candidates:
        identity = _material_identity(candidate, base)
        if identity not in identities and len(identities) >= maximum:
            continue
        identities.add(identity)
        selected.append(candidate)
    return selected, len(identities)


def _public_candidate(candidate: dict) -> dict:
    return {k: candidate.get(k) for k in ("candidate_id", "material_id", "shot_id", "source_start_us",
            "source_end_us", "description", "action_segments", "provenance", "observation_provenance",
            "verified_requirements")}


def _verified_requirements(candidate: dict) -> set[str]:
    raw = candidate.get("verified_requirements") or []
    if isinstance(raw, list):
        return {value for value in raw if isinstance(value, str)}
    if isinstance(raw, dict):
        return {str(key) for key, value in raw.items() if value is True or value == "verified"
                or (isinstance(value, dict) and value.get("status") in {"verified", "PASS"})}
    return set()


def _compile(group: dict, response: dict, candidates: list[dict], frozen: list[dict],
             pad_us: int, speed_max: float, timeline: list[int]) -> tuple[list[dict], dict]:
    if response.get("gap"):
        _fail("MATERIAL_GAP", str(response["gap"]), group_id=group["id"], members=group["members"])
    candidate = next((c for c in candidates if c["candidate_id"] == response.get("candidate_id")), None)
    if candidate is None:
        _fail("CANDIDATE_NOT_IN_VERIFIED_POOL", "planner selected unknown/unverified candidate", group_id=group["id"])
    lo = _integer(response.get("source_start_us"), "source_start_us")
    hi = _integer(response.get("source_end_us"), "source_end_us", 1)
    if not candidate["source_start_us"] <= lo < hi <= candidate["source_end_us"]:
        _fail("SOURCE_WINDOW_OUT_OF_BOUNDS", "planner window exceeds verified candidate", group_id=group["id"])
    total = sum(frozen[i-1]["audio_duration_us"] + pad_us for i in group["members"])
    speed = (hi - lo) / total
    if not 1.0 <= speed <= speed_max:
        _fail("GROUP_DURATION_UNSOLVABLE", "continuous action cannot fit frozen narration", group_id=group["id"],
              source_span_us=hi-lo, target_duration_us=total, speed=speed, speed_range=[1, speed_max])
    if not str(response.get("reason") or "").strip():
        _fail("PLAN_REASON_REQUIRED", "planner observation-based reason missing", group_id=group["id"])
    action_start = response.get("required_source_start_us", lo)
    action_end = response.get("required_source_end_us", hi)
    actions = candidate.get("action_segments") or []
    if group["require_complete_action"] and actions:
        complete = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            verb = str(action.get("action") or "").strip().lower()
            if verb in {"", "无动作", "静止", "静态", "静态陈列", "static", "no action", "none"}:
                continue
            if action.get("action_complete") is False:
                continue
            try:
                action_lo, action_hi = _time(action, "start"), _time(action, "end")
            except IndexedPlanError:
                continue
            if lo <= action_lo < action_hi <= hi:
                complete.append((action_lo, action_hi))
        if not complete:
            _fail("ACTION_NOT_COVERED", "selected slice truncates every observed action", group_id=group["id"])
        action_start, action_end = min(a[0] for a in complete), max(a[1] for a in complete)
    _integer(action_start, "required_source_start_us")
    _integer(action_end, "required_source_end_us", 1)
    if not lo <= action_start < action_end <= hi:
        _fail("ACTION_NOT_COVERED", "required action outside selected window", group_id=group["id"])
    cursor, elapsed, result = lo, 0, []
    for member in group["members"]:
        original = frozen[member-1]
        duration = original["audio_duration_us"] + pad_us
        elapsed += duration
        end = hi if member == group["members"][-1] else lo + round(elapsed * speed)
        result.append({**original, "video": candidate["video"], "video_sha256": candidate["video_sha256"],
                       "candidate_id": candidate["candidate_id"], "source_start_us": cursor,
                       "source_end_us": end, "duration_us": duration, "timeline_start_us": timeline[member-1],
                       "speed": speed, "evidence_intervals": [{"start_us": cursor, "end_us": end}],
                       "reason": response["reason"], "candidate_provenance": candidate.get("provenance")
                       or candidate.get("observation_provenance")})
        cursor = end
    planned_group = {**group, "required_source_start_us": action_start, "required_source_end_us": action_end,
                     "candidate_id": candidate["candidate_id"], "planner_response_file": response["_response_file"],
                     "planner_response_sha256": response["_response_sha256"]}
    return result, planned_group


def _render(rows: list[dict], output: Path, ffmpeg: str | None) -> Path:
    from .video_encode import run_h264_encode
    first, last = rows[0], rows[-1]
    spec = {"video_sha256": first["video_sha256"], "start_us": first["source_start_us"],
            "end_us": last["source_end_us"], "speed": first["speed"],
            "target_us": sum(r["duration_us"] for r in rows)}
    receipt = output.with_suffix(".render.json")
    if output.is_file() and receipt.is_file():
        previous = json.loads(receipt.read_text(encoding="utf-8-sig"))
        if previous.get("spec") == spec and previous.get("sha256") == cp._hash(output):
            return output
    output.parent.mkdir(parents=True, exist_ok=True)
    source_span = last["source_end_us"] - first["source_start_us"]
    encoder, _ = run_h264_encode(ffmpeg or os.environ.get("JY_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg",
        ["-y", "-hide_banner", "-loglevel", "error", "-ss", f"{first['source_start_us']/US:.6f}",
         "-i", first["video"], "-an", "-vf",
         f"trim=duration={source_span/US:.6f},setpts=(PTS-STARTPTS)/{first['speed']:.12f},scale=480:-2",
         "-t", f"{spec['target_us']/US:.6f}", "-r", "30"], output, timeout=120)
    _write(receipt, {"spec": spec, "sha256": cp._hash(output), "encoder": encoder})
    return output


def _frames(group: dict, rows: list[dict], report: Path, *, frame_extractor: Callable | None,
            slice_renderer: Callable | None, ffmpeg: str | None, fps: float) -> tuple[list[dict], Path]:
    from .shot_analyzer import extract_frame
    spec = [{k: r[k] for k in ("video_sha256", "source_start_us", "source_end_us", "duration_us", "speed")}
            for r in rows]
    root = report / "slices" / f"{group['id']}_{_digest(spec)[:20]}"
    preview = root / "actual_slice.mp4"
    preview.parent.mkdir(parents=True, exist_ok=True)
    if slice_renderer:
        rendered = slice_renderer(copy.deepcopy(rows), preview)
        if rendered:
            preview = Path(rendered)
    else:
        preview = _render(rows, preview, ffmpeg)
    if not preview.is_file() or not preview.stat().st_size:
        _fail("OBSERVER_SLICE_MISSING", "actual rendered slice missing", group_id=group["id"])
    extract = frame_extractor or (lambda source, timestamp_us, output:
                                  extract_frame(source, timestamp_us / US, output, ffmpeg=ffmpeg))
    result, offset = [], 0
    for row in rows:
        # The sequence comes from actual playback at the planned speed, not the
        # candidate's old contact sheet. Sampling is evidence, not human approval.
        duration = row["duration_us"]
        count = max(3, min(12, math.ceil(duration / 250_000)))
        margin = min(round(US / fps), max(1, duration // 10))
        stamps = sorted({0, max(0, duration - margin),
                         *(round((duration-margin) * i / (count-1)) for i in range(count))})
        for number, stamp in enumerate(stamps):
            output = root / f"s{row['index']:03d}_{number:02d}.jpg"
            if not output.is_file() or not output.stat().st_size:
                if not extract(preview, offset + stamp, output):
                    _fail("OBSERVER_FRAME_FAILED", "could not extract actual slice frame",
                          group_id=group["id"], index=row["index"])
            source_time = min(row["source_end_us"], row["source_start_us"] + round(stamp * row["speed"]))
            result.append({"path": str(output), "sha256": cp._hash(output), "timestamp_us": source_time,
                           "rendered_timestamp_us": offset + stamp, "index": row["index"]})
        offset += duration
    return result, preview


def _observe(group: dict, rows: list[dict], manifest: dict, report: Path, model: Model | None,
             stats: dict, *, frame_extractor: Callable | None, slice_renderer: Callable | None,
             ffmpeg: str | None) -> tuple[dict, list[dict], list[dict]]:
    images, preview = _frames(group, rows, report, frame_extractor=frame_extractor,
                              slice_renderer=slice_renderer, ffmpeg=ffmpeg,
                              fps=float(manifest.get("fps") or 30))
    response = _call("observe", {"full_script": [s["text"] for s in manifest["segments"]],
                     "group_id": group["id"], "members": group["members"],
                     "require_complete_action": group["require_complete_action"],
                     "required_source_start_us": group["required_source_start_us"],
                     "required_source_end_us": group["required_source_end_us"],
                     "speed": rows[0]["speed"], "segments": [{k: r[k] for k in
                         ("index", "text", "duration_us", "source_start_us", "source_end_us")} for r in rows],
                     "frame_mapping": [{k: i[k] for k in ("index", "timestamp_us", "rendered_timestamp_us")}
                                       for i in images], "temporal_review": "sampled_rendered_sequence"},
                     report, model, images=images, stats=stats)
    observations, issues = response.get("observations"), []
    by_index = {r.get("index"): r for r in observations or [] if isinstance(r, dict)}
    if not isinstance(observations, list) or len(observations) != len(rows) or set(by_index) != set(group["members"]):
        issues.append({"type": "OBSERVER_MEMBER_MAPPING", "group_id": group["id"]})
    compiled = []
    for row in rows:
        observed = by_index.get(row["index"], {})
        if observed.get("status") not in {"supported", "contextual"} or not str(observed.get("observation") or "").strip():
            issues.append({"type": "SLICE_SEMANTIC_UNSUPPORTED", "group_id": group["id"],
                           "index": row["index"], "observation": observed.get("observation", "")})
        compiled.append({"index": row["index"], "status": observed.get("status", "uncertain"),
                         "observation": observed.get("observation", ""),
                         "evidence": [i for i in images if i["index"] == row["index"]]})
    statuses = {"complete"} if group["require_complete_action"] else {"complete", "not_required"}
    if (response.get("status") not in statuses or not str(response.get("observation") or "").strip()
            or (group["require_complete_action"] and response.get("action_complete") is not True)):
        issues.append({"type": "ACTION_INCOMPLETE_OR_UNVERIFIED", "group_id": group["id"],
                       "observation": response.get("observation", "")})
    if response.get("visible_at_speed") is not True:
        issues.append({"type": "PLAYBACK_NOT_INTELLIGIBLE", "group_id": group["id"], "speed": rows[0]["speed"]})
    for item in response.get("issues") or []:
        issues.append({"type": "INDEPENDENT_OBSERVER_ISSUE", "group_id": group["id"], "message": str(item)})
    result = {"id": group["id"], "status": response.get("status", "uncertain"),
              "observation": response.get("observation", ""), "visible_at_speed": response.get("visible_at_speed"),
              "evidence": images, "preview_file": str(preview), "preview_sha256": cp._hash(preview),
              "response_file": response["_response_file"], "response_sha256": response["_response_sha256"],
              "temporal_review": "sampled_rendered_sequence"}
    return result, compiled, issues


def _review_html(report: Path, manifest: dict, plan: dict, observer: dict, issues: list[dict]) -> Path:
    by_index = {r["index"]: r for r in plan.get("segments", [])}
    observations = {r["index"]: r for r in observer.get("observations", [])}
    blocks = []
    for group in observer.get("groups", []):
        planned = next((g for g in plan.get("groups", []) if g["id"] == group["id"]), {})
        members = [by_index[i] for i in planned.get("members", []) if i in by_index]
        sentences = "".join(f"<p><b>{r['index']}.</b> {html.escape(r['text'])}<br>"
                            f"<small>{r['source_start_us']/US:.3f}–{r['source_end_us']/US:.3f}s · "
                            f"{r['speed']:.3f}×</small><br>{html.escape(observations.get(r['index'], {}).get('observation',''))}</p>"
                            for r in members)
        path = Path(group["preview_file"]).absolute().as_uri()
        original_link = ""
        if members:
            original = Path(members[0]["video"]).absolute().as_uri()
            original += f"#t={members[0]['source_start_us']/US:.6f},{members[-1]['source_end_us']/US:.6f}"
            original_link = f"<p><a href='{html.escape(original)}'>查看原片对应区间</a><br>选镜依据：{html.escape(members[0].get('reason',''))}</p>"
        blocks.append(f"<section><h2>{html.escape(group['id'])}</h2><video controls preload='metadata' src='{html.escape(path)}'></video>"
                      f"{sentences}{original_link}<p>{html.escape(group.get('observation',''))}</p>"
                      f"<small>preview SHA256: {group['preview_sha256']}</small></section>")
    feedback_binding = {"schema": "contextual_review_feedback.v1", "plan_sha256": cp._hash(Path(plan["_file"]))
                        if plan.get("_file") else "", "human_review": "PENDING", "reviewer_id": "", "groups": []}
    for group in observer.get("groups", []):
        planned = next((g for g in plan.get("groups", []) if g["id"] == group["id"]), {})
        pieces = [by_index[i] for i in planned.get("members", []) if i in by_index]
        feedback_binding["groups"].append({"group_id": group["id"], "members": planned.get("members", []),
            "video_sha256": pieces[0]["video_sha256"] if pieces else "",
            "preview_sha256": group["preview_sha256"], "decision": "PENDING", "issues": [], "note": ""})
    # Rebuilding an identical plan must not erase somebody's completed annotations.
    feedback_file = report / "feedback_template.json"
    if not feedback_file.is_file():
        _write(feedback_file, feedback_binding)
    else:
        previous = json.loads(feedback_file.read_text(encoding="utf-8-sig"))
        if previous.get("plan_sha256") != feedback_binding["plan_sha256"]:
            feedback_file = report / f"feedback_template_{feedback_binding['plan_sha256'][:16]}.json"
            if not feedback_file.is_file():
                _write(feedback_file, feedback_binding)
    document = "<!doctype html><html lang='zh'><meta charset='utf-8'><title>整稿匹配审核</title>" \
        "<style>body{max-width:1080px;margin:24px auto;font:16px system-ui;background:#f5f6f8;color:#18202a}section{background:white;padding:20px;margin:16px 0;border-radius:12px}video{width:300px;max-height:500px;float:left;margin-right:24px}section:after{content:'';display:block;clear:both}small{color:#52606e}pre{white-space:pre-wrap}</style>" \
        "<h1>整稿匹配本地审核</h1><p>仅用于本地验证；人工审核 PENDING，正式交付未授权。采样帧审核不替代完整播放。" \
        + f"反馈请使用 <a href='{html.escape(feedback_file.name)}'>{html.escape(feedback_file.name)}</a>，保留计划和素材哈希。</p>" \
        + "".join(blocks) + "<h2>未解决问题</h2><pre>" + html.escape(json.dumps(issues, ensure_ascii=False, indent=2)) + "</pre></html>"
    output = report / "review.html"
    output.write_text(document, encoding="utf-8")
    return output


def feedback_cases(feedback_file: Path, plan_file: Path, observer_file: Path) -> list[dict]:
    """Read bound human annotations as evaluation cases, without publishing approval."""
    feedback = json.loads(Path(feedback_file).read_text(encoding="utf-8-sig"))
    plan = json.loads(Path(plan_file).read_text(encoding="utf-8-sig"))
    observer = json.loads(Path(observer_file).read_text(encoding="utf-8-sig"))
    plan_sha = cp._hash(Path(plan_file))
    if (feedback.get("schema") != "contextual_review_feedback.v1" or feedback.get("plan_sha256") != plan_sha
            or observer.get("plan_sha256") != plan_sha):
        _fail("FEEDBACK_PLAN_MISMATCH", "feedback does not bind the current observed plan")
    rows = {r["index"]: r for r in plan.get("segments", [])}
    groups = {g["id"]: g for g in plan.get("groups", [])}
    observed = {g["id"]: g for g in observer.get("groups", [])}
    result, seen = [], set()
    for item in feedback.get("groups", []):
        gid = item.get("group_id")
        group, observation = groups.get(gid), observed.get(gid)
        if (not group or not observation or gid in seen or item.get("members") != group["members"]
                or item.get("video_sha256") != rows[group["members"][0]]["video_sha256"]
                or item.get("preview_sha256") != observation["preview_sha256"]):
            _fail("FEEDBACK_MEDIA_MISMATCH", "feedback source/preview/group binding changed", group_id=gid)
        seen.add(gid)
        if item.get("decision") == "PENDING":
            continue
        if item.get("decision") not in {"accept", "reject", "uncertain"} or not str(feedback.get("reviewer_id") or "").strip():
            _fail("FEEDBACK_REVIEWER_REQUIRED", "completed feedback requires named reviewer and explicit decision", group_id=gid)
        result.append({"schema": "contextual_eval_case.v1", "plan_sha256": plan_sha,
                       "feedback_sha256": cp._hash(Path(feedback_file)), "reviewer_id": feedback["reviewer_id"],
                       **copy.deepcopy(item), "formal_delivery_allowed": False})
    return result


def prepare(manifest: dict, candidates: list[dict], report_dir: Path, *, base_dir: Path | None = None,
            model: Model | None = None, frame_extractor: Callable | None = None,
            slice_renderer: Callable | None = None, ffmpeg: str | None = None,
            max_repairs: int = 2, candidate_expander: Callable | None = None) -> dict:
    """Plan and review selected verified candidates; return structured local build input.

    ``frame_extractor(preview_path, rendered_timestamp_us, output_path) -> bool``
    and ``slice_renderer(rows, output_path) -> Path`` are optional offline seams.
    A repair never regroups or changes successful groups.  No raw candidate
    outside the supplied verified pool is fetched, analyzed or substituted.
    ``candidate_expander(group, current_pool) -> list[dict]`` may supply one
    expanded, verified pool (at most 16 distinct materials, keeping all their
    observed windows) for an empty/gapped/infeasible group.
    Expansion retries the same planning attempt and does not consume repairs.
    """
    report, base = Path(report_dir), Path(base_dir or Path.cwd())
    report.mkdir(parents=True, exist_ok=True)
    stats = {"model_calls": 0, "model_cache_hits": 0, "group_repairs": {}, "group_expansions": {},
             "material_understanding_calls": 0, "temporal_review": "sampled_rendered_sequence"}
    issues, rows, planned_groups, observations, observed_groups = [], [], [], [], []
    result = {"ok": False, "status": "UNCERTIFIED", "formal_delivery_allowed": False,
              "human_review": "PENDING", "prepared_manifest": None, "contextual_plan": None, "stats": stats}
    try:
        if manifest.get("delivery_mode") != "formal":
            _fail("FORMAL_MODE_REQUIRED", "indexed contextual construction requires explicit formal mode")
        frozen = _frozen(manifest, base)
        groups = group_script(manifest, report, model, base_dir=base, stats=stats)
        verified, rejected = _candidates(candidates, base)
        stats["verified_candidates"], stats["rejected_candidates"] = len(verified), rejected
        if not verified and candidate_expander is None:
            _fail("NO_VERIFIED_CANDIDATES", "no candidates with usable observation evidence", rejected=rejected)
        pad = float(manifest.get("tail_pad_s", .12))
        if not math.isfinite(pad) or pad < 0:
            _fail("INVALID_TIMING", "tail padding invalid")
        pad_us = round(pad * US)
        from .shot_analyzer import EVIDENCE_FIT_SPEED_MAX
        timeline, cursor = [], 0
        for original in frozen:
            timeline.append(cursor)
            cursor += original["audio_duration_us"] + pad_us
        repairs = max(0, min(2, int(max_repairs)))
        for group in groups:
            required = set(group.get("required_requirements", []))
            raw_pool = [c for c in verified if not c.get("group_ids") or group["id"] in c["group_ids"]]
            pool = [c for c in raw_pool if required <= _verified_requirements(c)]
            expanded = False
            def expand_pool(current: list[dict], trigger: list[dict]) -> list[dict]:
                nonlocal expanded
                expanded = True
                stats["group_expansions"][group["id"]] = 1
                expansion_file = report / "attempts" / f"{group['id']}_expansion.json"
                receipt = {"group_id": group["id"], "trigger": trigger,
                           "previous_candidates": [c["candidate_id"] for c in current],
                           "maximum_materials": 16, "window_count": 0, "material_count": 0}
                _write(expansion_file, receipt)
                try:
                    expanded_rows = candidate_expander(copy.deepcopy(group), copy.deepcopy(current))
                    if not isinstance(expanded_rows, list):
                        _fail("CANDIDATE_EXPANSION_INVALID", "expander must return a verified candidate list", group_id=group["id"])
                    normalized, rejected_expansion = _candidates(expanded_rows, base)
                except cp.ContextualPlanError:
                    raise
                except Exception as exc:
                    _fail("CANDIDATE_EXPANSION_FAILED", str(exc), group_id=group["id"])
                stats.setdefault("expansion_rejections", []).extend(rejected_expansion)
                eligible = [c for c in normalized if (not c.get("group_ids") or group["id"] in c["group_ids"])
                            and required <= _verified_requirements(c)]
                selected, material_count = _limit_materials(eligible, base)
                _write(expansion_file, {**receipt, "input_window_count": len(expanded_rows),
                                       "window_count": len(selected), "material_count": material_count})
                return selected
            failure, group_rows, planned, observed, segment_obs = [], [], None, None, []
            if not pool:
                missing = [{"candidate_id": c["candidate_id"],
                            "missing_requirements": sorted(required - _verified_requirements(c))} for c in raw_pool]
                failure = [{"type": "GROUP_REQUIREMENTS_UNVERIFIED" if required else "GROUP_CANDIDATES_MISSING",
                            "group_id": group["id"], "required_requirements": sorted(required), "candidates": missing}]
                if candidate_expander is not None:
                    pool = expand_pool(raw_pool, failure)
                if not pool:
                    issues.extend(failure)
                    continue
            attempt = 0
            while attempt <= repairs:
                stage = "plan" if attempt == 0 else "repair"
                payload = {"full_script": [r["text"] for r in frozen], "groups": groups,
                           "group": group, "segments": [frozen[i-1] for i in group["members"]],
                           "locked_visual_windows": [{k: r[k] for k in
                               ("index", "video_sha256", "candidate_id", "source_start_us", "source_end_us")}
                               for r in rows],
                           "group_duration_us": sum(frozen[i-1]["audio_duration_us"] + pad_us for i in group["members"]),
                           "speed_range": [1, EVIDENCE_FIT_SPEED_MAX],
                           "candidates": [_public_candidate(c) for c in pool],
                           "failed_groups": [group["id"]] if failure else [], "issues": failure}
                try:
                    response = _call(stage, payload, report, model, stats=stats)
                    group_rows, planned = _compile(group, response, pool, frozen, pad_us,
                                                   EVIDENCE_FIT_SPEED_MAX, timeline)
                    observed, segment_obs, failure = _observe(planned, group_rows, manifest, report, model, stats,
                        frame_extractor=frame_extractor, slice_renderer=slice_renderer, ffmpeg=ffmpeg)
                except (OSError, ValueError, TypeError, RuntimeError) as exc:
                    failure = [exc.as_dict() if isinstance(exc, cp.ContextualPlanError)
                               else {"type": "GROUP_PROCESSING_FAILED", "message": str(exc), "group_id": group["id"]}]
                if (candidate_expander is not None and not expanded and any(i["type"] in
                        {"MATERIAL_GAP", "GROUP_DURATION_UNSOLVABLE", "GROUP_REQUIREMENTS_UNVERIFIED",
                         "GROUP_CANDIDATES_MISSING", "SLICE_SEMANTIC_UNSUPPORTED",
                         "ACTION_INCOMPLETE_OR_UNVERIFIED", "PLAYBACK_NOT_INTELLIGIBLE",
                         "INDEPENDENT_OBSERVER_ISSUE"} for i in failure)):
                    pool = expand_pool(pool, failure)
                    if pool:
                        continue
                    failure.append({"type": "CANDIDATE_EXPANSION_EXHAUSTED", "group_id": group["id"]})
                _write(report / "attempts" / f"{group['id']}_{attempt:02d}.json",
                       {"group": group, "attempt": attempt, "rows": group_rows, "observer": observed,
                        "issues": failure, "formal_delivery_allowed": False, "human_review": "PENDING"})
                stats["group_repairs"][group["id"]] = attempt
                if not failure or any(i["type"] in {"MODEL_REQUIRED", "MODEL_CALL_FAILED",
                                                    "CANDIDATE_EXPANSION_EXHAUSTED"} for i in failure):
                    break
                if expanded and any(i["type"] == "MATERIAL_GAP" for i in failure):
                    break
                attempt += 1
            if planned:
                rows.extend(group_rows)
                planned_groups.append(planned)
            if observed:
                observations.extend(segment_obs)
                observed_groups.append(observed)
            issues.extend(failure)
            if any(i["type"] in {"MODEL_REQUIRED", "MODEL_CALL_FAILED"} for i in failure):
                break
        plan = {"schema": "contextual_plan.v1", "task_id": str(manifest.get("task_id") or manifest.get("record_id") or ""),
                "planner_id": "indexed_contextual_planner", "segments": sorted(rows, key=lambda r: r["index"]),
                "groups": planned_groups, "formal_delivery_allowed": False, "human_review": "PENDING"}
        plan_path = report / "contextual_plan.json"
        plan_sha = _write(plan_path, plan)
        observer = {"schema": "contextual_observer.v1", "producer": "indexed_planner", "plan_sha256": plan_sha,
                    "reviewer_id": "independent_slice_observer", "method": "independent_visual_review",
                    "observations": observations, "groups": observed_groups,
                    "formal_delivery_allowed": False, "human_review": "PENDING"}
        observer_path = report / "contextual_observer.json"
        observer_sha = _write(observer_path, observer)
        result.update(plan_file=str(plan_path), observer_file=str(observer_path), plan_sha256=plan_sha,
                      observer_sha256=observer_sha)
        review = _review_html(report, manifest, {**plan, "_file": str(plan_path)}, observer, issues)
        result["review_file"] = str(review)
        if not issues:
            prepared = copy.deepcopy(manifest)
            prepared.update(experiment_arm="B", temporary_material_analysis=False, auto_match_shots=False,
                            auto_transition=False, indexed_groups=groups,
                            contextual_plan={"plan_file": str(plan_path), "plan_sha256": plan_sha,
                                             "observer_file": str(observer_path), "observer_sha256": observer_sha})
            contract = cp.load(prepared, base)
            result.update(ok=True, status="READY_FOR_LOCAL_BUILD", prepared_manifest=prepared,
                          contextual_plan=contract)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        issues.append(exc.as_dict() if isinstance(exc, cp.ContextualPlanError)
                      else {"type": "INDEXED_PLANNING_FAILED", "message": str(exc)})
    result["issues"] = issues
    _write(report / "indexed_planner_result.json", {k: v for k, v in result.items()
                                                    if k not in {"contextual_plan", "prepared_manifest"}})
    return result
