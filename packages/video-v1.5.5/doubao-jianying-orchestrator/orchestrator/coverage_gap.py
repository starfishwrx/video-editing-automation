"""Fill one uncovered action interval on an already indexed candidate.

Representative frames and stored observations stay in the 1.5.4 index. A gap is
the source-time span whose evidence frames miss an action boundary by more than
half a second. Sampling stays inside that span, and one candidate gets at most
one extra remote vision request.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Iterable


US = 1_000_000
BOUNDARY_US = 500_000
MAX_GAP_FRAMES = 12
SUPPLEMENT_PROTOCOL = "coverage-gap-v1"
PHASES = ("prepare", "execute", "result", "settled")
ROLES = ("direct_evidence", "demonstration")
_STATIC = {"", "无动作", "静止", "静态", "静态陈列", "stable", "settled", "static", "no action", "none"}

SUPPLEMENT_PROMPT = """只观察这次给出的缺口帧，时间是原素材微秒。已有观察没有覆盖动作开始、执行或可见结果。
不要把稳定陈列写成整段动作完成，不要把道具示意或仪器读数升级为直接功能证据。
准备、执行、结果展示、稳定陈列分开写。每段使用整数微秒，并标明 evidence_role 为 direct_evidence 或 demonstration。
只输出 JSON：
{"description":"缺口里实际看见的内容","action_phases":[{"phase":"prepare|execute|result|settled","start_us":0,"end_us":1,"action":"","object":"","result":null,"complete":false,"evidence_role":"demonstration"}],"action_segments":[{"start_us":0,"end_us":1,"action":"","result":null}]}"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def gap_signature(material_id: str, intervals: Iterable[tuple[int, int]]) -> str:
    raw = _json({"material_id": material_id, "intervals": [list(item) for item in intervals],
                 "protocol": SUPPLEMENT_PROTOCOL})
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _integer(value: Any) -> int | None:
    if type(value) is int and not isinstance(value, bool):
        return value
    return None


def _merge(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted((start, end) for start, end in intervals if end > start)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def evidence_frames(record: dict[str, Any], frames: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use frames the stored observation actually cites, not every indexed frame."""
    listed = list(frames)
    ids = {str(item) for item in (record.get("provenance") or {}).get("evidence") or []}
    chosen = [frame for frame in listed if ids and str(frame.get("frame_id")) in ids]
    if not chosen:
        times = []
        raw = record.get("observation") or {}
        for item in list(raw.get("vision_frames") or []) + list(raw.get("evidence_frames") or []):
            if isinstance(item, dict):
                stamp = _integer(item.get("timestamp_us"))
                if stamp is not None:
                    times.append(stamp)
        chosen = [{"timestamp_us": stamp} for stamp in times]
    return sorted(chosen, key=lambda frame: int(frame.get("timestamp_us") or 0))


def _segments(observation: dict[str, Any]) -> list[tuple[int, int, dict[str, Any]]]:
    result = []
    for segment in observation.get("action_segments") or []:
        if not isinstance(segment, dict):
            continue
        verb = str(segment.get("action") or "").strip().lower()
        if verb in _STATIC:
            continue
        start, end = _integer(segment.get("start_us")), _integer(segment.get("end_us"))
        if start is None or end is None or end <= start:
            continue
        result.append((start, end, segment))
    return result


def gap_intervals(record: dict[str, Any], frames: Iterable[dict[str, Any]], *,
                  duration_us: int) -> list[tuple[int, int]]:
    """Return uncovered source intervals. Empty means the stored evidence is enough."""
    evidence = evidence_frames(record, frames)
    if not evidence or duration_us <= 0:
        return []
    times = [int(frame["timestamp_us"]) for frame in evidence]
    earliest, latest = min(times), max(times)
    intervals: list[tuple[int, int]] = []
    segments = _segments(record.get("observation") or {})
    if segments:
        for start, end, segment in segments:
            if earliest - start > BOUNDARY_US:
                intervals.append((max(0, start), min(duration_us, earliest)))
            result = segment.get("result")
            needs_result = bool(result) or segment.get("phase") == "result"
            if needs_result and end - latest > BOUNDARY_US:
                intervals.append((max(0, latest), min(duration_us, end)))
    elif earliest > BOUNDARY_US:
        intervals.append((0, min(duration_us, earliest)))
    phases = [phase for phase in (record.get("observation") or {}).get("action_phases") or []
              if isinstance(phase, dict)]
    if phases and all(phase.get("phase") == "settled" for phase in phases):
        start = min(_integer(phase.get("start_us")) or earliest for phase in phases)
        if start > BOUNDARY_US:
            intervals.append((0, min(duration_us, start)))
    return _merge(intervals)


def sample_times(start_us: int, end_us: int, *, count: int = MAX_GAP_FRAMES) -> list[int]:
    """Even integer microseconds inside the uncovered side of a gap."""
    if end_us <= start_us:
        return []
    last = end_us - 1
    span = last - start_us
    if span <= 0:
        return [start_us]
    count = max(2, min(int(count), MAX_GAP_FRAMES, max(2, span // 100_000)))
    return [start_us + (span * index) // (count - 1) for index in range(count)]


def sample_gap(intervals: Iterable[tuple[int, int]], *, count: int = MAX_GAP_FRAMES) -> list[int]:
    intervals = _merge(list(intervals))
    if not intervals:
        return []
    per_interval = max(2, count // len(intervals))
    times: list[int] = []
    for start, end in intervals:
        times.extend(sample_times(start, end, count=per_interval))
    unique = sorted(set(times))
    if len(unique) <= count:
        return unique
    if count == 1:
        return [unique[0]]
    picked = [unique[(index * (len(unique) - 1)) // (count - 1)] for index in range(count)]
    return sorted(set(picked))


def gap_ffmpeg_command(ffmpeg: str, video: str, start_us: int, end_us: int,
                       count: int, output_pattern: str) -> list[str]:
    duration_s = max(0.05, (end_us - start_us) / US)
    frames = max(2, min(count, MAX_GAP_FRAMES))
    rate = max(frames - 1, 1) / duration_s
    return [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{start_us / US:.6f}", "-t", f"{duration_s:.6f}",
            "-i", video, "-an", "-vf", f"fps={rate:.6f}",
            "-frames:v", str(frames), output_pattern]


def extract_gap_frames(video: str | Path, intervals: Iterable[tuple[int, int]], output_dir: str | Path,
                       *, ffmpeg: str | None = None, existing: Iterable[dict[str, Any]] = (),
                       count: int = MAX_GAP_FRAMES, frame_extractor=None) -> dict[str, Any]:
    """Decode the uncovered span once. Fall back to per-time extraction only if that fails."""
    times = sample_gap(intervals, count=count)
    ready: list[dict[str, Any]] = []
    missing: list[int] = []
    pool = list(existing)
    for stamp in times:
        hit = next((frame for frame in pool if abs(int(frame.get("timestamp_us") or -1) - stamp) <= 100_000
                    and frame.get("path")), None)
        if hit:
            ready.append(hit)
        else:
            missing.append(stamp)
    if not missing:
        return {"frames": ready, "decode_commands": 0, "timestamps_us": times}
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    start, end = min(missing), max(missing) + 1
    command = gap_ffmpeg_command(ffmpeg or "ffmpeg", str(video), start, end, len(missing),
                                 str(output / "gap_%03d.jpg"))
    decoded: list[dict[str, Any]] = []
    try:
        proc = subprocess.run(command, capture_output=True, timeout=120)
        files = sorted(output.glob("gap_*.jpg"))
        if proc.returncode != 0 or len(files) < len(missing):
            raise RuntimeError("gap decode did not return the requested frames")
        for stamp, path in zip(sample_times(start, end, count=len(files)), files):
            decoded.append({"timestamp_us": stamp, "path": str(path)})
        return {"frames": ready + decoded, "decode_commands": 1, "timestamps_us": [frame["timestamp_us"] for frame in ready + decoded]}
    except (OSError, subprocess.SubprocessError, RuntimeError):
        if frame_extractor is None:
            from .shot_analyzer import extract_frame
            frame_extractor = lambda source, stamp, path, ffmpeg=None: extract_frame(  # noqa: E731
                source, stamp / US, path, ffmpeg=ffmpeg)
        for index, stamp in enumerate(missing, 1):
            path = output / f"gap_{index:03d}.jpg"
            if frame_extractor(Path(video), stamp, path, ffmpeg):
                decoded.append({"timestamp_us": stamp, "path": str(path)})
        return {"frames": ready + decoded, "decode_commands": 1 if decoded else 0,
                "timestamps_us": [frame["timestamp_us"] for frame in ready + decoded]}


def segment_fields(answer: dict[str, Any]) -> dict[str, Any]:
    """Separate phase completeness from the legacy whole-shot action_complete flag."""
    phases = []
    for raw in answer.get("action_phases") or []:
        if not isinstance(raw, dict) or raw.get("phase") not in PHASES:
            continue
        start, end = _integer(raw.get("start_us")), _integer(raw.get("end_us"))
        if start is None or end is None or end <= start:
            continue
        role = raw.get("evidence_role") if raw.get("evidence_role") in ROLES else "demonstration"
        result = raw.get("result")
        result = str(result).strip() if result is not None else ""
        phases.append({"phase": raw["phase"], "start_us": start, "end_us": end,
                       "action": str(raw.get("action") or "").strip(),
                       "object": str(raw.get("object") or "").strip(),
                       "result": result or None, "complete": raw.get("complete") is True,
                       "evidence_role": role})
    direct = [phase for phase in phases if phase["evidence_role"] == "direct_evidence"]
    execute = [phase for phase in direct if phase["phase"] == "execute" and phase["complete"]]
    shown = [phase for phase in direct if phase["phase"] == "result" and phase["complete"] and phase["result"]]
    settled_only = bool(phases) and all(phase["phase"] == "settled" for phase in phases)
    segment_complete = bool(execute and shown and not settled_only)
    segments = []
    for phase in phases:
        segments.append({"start_us": phase["start_us"], "end_us": phase["end_us"],
                         "action": phase["action"], "object": phase["object"], "result": phase["result"],
                         "phase": phase["phase"], "evidence_role": phase["evidence_role"],
                         "action_complete": phase["complete"] and phase in execute + shown})
    return {"description": str(answer.get("description") or "").strip(),
            "action_phases": phases, "action_segments": segments or list(answer.get("action_segments") or []),
            "segment_complete": segment_complete, "action_complete": segment_complete,
            "settled_only": settled_only,
            "action": execute[0]["action"] if execute else "",
            "object": execute[0]["object"] if execute else "",
            "result": shown[0]["result"] if shown else None,
            "evidence_role": "direct_evidence" if segment_complete else "demonstration"}


def boundary_error(predicted_start: int, predicted_end: int,
                   reference_start: int, reference_end: int) -> dict[str, Any]:
    start_error = abs(predicted_start - reference_start)
    end_error = abs(predicted_end - reference_end)
    return {"start_error_us": start_error, "end_error_us": end_error,
            "within_0_5s": start_error <= BOUNDARY_US and end_error <= BOUNDARY_US}


def _inside(stamp: int, intervals: Iterable[tuple[int, int]]) -> bool:
    return any(start <= stamp < end for start, end in intervals)


def supplement_candidates(candidates: list[dict[str, Any]], *, store, product: str, group: dict[str, Any],
                          paths: dict[str, str], model, report: Path, stats: dict[str, Any],
                          env: dict | None = None) -> list[dict[str, Any]]:
    """One remote supplement per gapped material. A stored supplement is reused."""
    if not candidates:
        return []
    env = env or {}
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        material_id = candidate["material_id"]
        if material_id in seen:
            continue
        records = store.get_observations(material_id, product_id=product)
        record = next((item for item in records if item["observation_id"] == candidate.get("candidate_id")), None)
        if record is None or record.get("protocol") == SUPPLEMENT_PROTOCOL:
            kept.append(candidate)
            continue
        frames = store.get_frames(material_id)
        duration = int(record["end_us"])
        intervals = gap_intervals(record, frames, duration_us=duration)
        if not intervals:
            kept.append(candidate)
            continue
        signature = gap_signature(material_id, intervals)
        prior = next((item for item in records if item.get("protocol") == SUPPLEMENT_PROTOCOL
                      and (item.get("provenance") or {}).get("gap_signature") == signature), None)
        if prior is None:
            with store.lease(material_id, "coverage-supplement", timeout_s=120, lease_s=300):
                records = store.get_observations(material_id, product_id=product)
                prior = next((item for item in records if item.get("protocol") == SUPPLEMENT_PROTOCOL
                              and (item.get("provenance") or {}).get("gap_signature") == signature), None)
                if prior is None:
                    if stats.get("coverage_supplement_calls_by_material", {}).get(material_id, 0) >= 1:
                        continue
                    extracted = extract_gap_frames(
                        paths[material_id], intervals, Path(report) / "coverage_frames" / material_id,
                        ffmpeg=(env or {}).get("ffmpeg"), existing=frames,
                        frame_extractor=None)
                    images = []
                    for frame in extracted["frames"]:
                        stamp = int(frame["timestamp_us"])
                        if not _inside(stamp, intervals):
                            continue
                        if not frame.get("frame_id"):
                            saved = store.save_frame(material_id, stamp, frame["path"], protocol=SUPPLEMENT_PROTOCOL)
                        else:
                            saved = frame
                        images.append(saved)
                    images = images[:MAX_GAP_FRAMES]
                    if not images:
                        continue
                    answer = model({"stage": "coverage_supplement", "prompt": SUPPLEMENT_PROMPT,
                                    "payload": {"material_id": material_id, "gap_intervals_us": intervals,
                                                "group_id": group.get("id"),
                                                "required_requirements": list(group.get("required_requirements") or []),
                                                "prior_description": (record.get("observation") or {}).get("description")},
                                    "images": images,
                                    "output": str(Path(report) / "coverage" / f"{signature[:16]}.json")})
                    fields = segment_fields(answer if isinstance(answer, dict) else {})
                    start, end = intervals[0][0], intervals[-1][1]
                    if end <= start:
                        continue
                    prior = store.save_observation(
                        material_id, {**fields, "usable": fields["segment_complete"],
                                      "source_time_us": {"start_us": start, "end_us": end}},
                        product_id=product, protocol=SUPPLEMENT_PROTOCOL, start_us=start, end_us=end,
                        state="observed", provenance={
                            "kind": "coverage-supplement", "gap_signature": signature,
                            "evidence": [frame.get("frame_id") for frame in images if frame.get("frame_id")],
                            "evaluated_requirements": list(group.get("required_requirements") or []),
                            "decode_commands": extracted["decode_commands"]})
                    stats["coverage_supplement_calls"] = stats.get("coverage_supplement_calls", 0) + 1
                    stats["material_understanding_calls"] = stats.get("material_understanding_calls", 0) + 1
                    by_material = stats.setdefault("coverage_supplement_calls_by_material", {})
                    by_material[material_id] = by_material.get(material_id, 0) + 1
                    stats["coverage_decode_commands"] = stats.get("coverage_decode_commands", 0) + int(extracted["decode_commands"])
        if prior is None:
            continue
        seen.add(material_id)
        fields = prior["observation"]
        if group.get("require_complete_action") and fields.get("segment_complete") is not True:
            continue
        merged = {**candidate, "description": fields.get("description") or candidate.get("description"),
                  "action_phases": fields.get("action_phases") or [],
                  "action_segments": fields.get("action_segments") or candidate.get("action_segments"),
                  "segment_complete": fields.get("segment_complete"),
                  "action_complete": fields.get("action_complete"),
                  "evidence_role": fields.get("evidence_role"),
                  "action": fields.get("action"), "object": fields.get("object"), "result": fields.get("result"),
                  "coverage_supplement": {"observation_id": prior["observation_id"], "gap_signature": signature,
                                          "source_start_us": prior["start_us"], "source_end_us": prior["end_us"]}}
        kept.append(merged)
    return kept


# Fixed offline cases. The head-miss sample is additional to the 30 semantic cases.
EVAL_CASES: list[dict[str, Any]] = [
    {"id": "unseal-covered", "category": "拆封", "kind": "positive", "duration_us": 8_000_000,
     "evidence_us": [200_000, 1_500_000, 3_000_000], "reference": (200_000, 3_000_000),
     "segments": [{"start_us": 200_000, "end_us": 3_000_000, "action": "unseal", "result": "bag open"}],
     "expect_remote": 0, "expect_complete": True},
    {"id": "unseal-head-gap", "category": "拆封", "kind": "positive", "duration_us": 12_000_000,
     "evidence_us": [4_000_000], "reference": (300_000, 2_400_000),
     "segments": [{"start_us": 0, "end_us": 2_400_000, "action": "unseal", "result": "bag open"}],
     "answer": {"description": "拆封从片头开始", "action_phases": [
         {"phase": "execute", "start_us": 400_000, "end_us": 1_600_000, "action": "unseal", "object": "bag", "result": None, "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 1_600_000, "end_us": 2_200_000, "action": "unseal", "object": "bag", "result": "bag open", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
    {"id": "unseal-result-gap", "category": "拆封", "kind": "positive", "duration_us": 10_000_000,
     "evidence_us": [200_000, 1_000_000], "reference": (200_000, 4_200_000),
     "segments": [{"start_us": 200_000, "end_us": 4_500_000, "action": "unseal", "result": "seal removed"}],
     "answer": {"description": "补上拆封结果", "action_phases": [
         {"phase": "execute", "start_us": 250_000, "end_us": 2_000_000, "action": "unseal", "object": "seal", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 2_000_000, "end_us": 4_400_000, "action": "unseal", "object": "seal", "result": "seal removed", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
    {"id": "unseal-settled", "category": "拆封", "kind": "negative", "duration_us": 6_000_000,
     "evidence_us": [200_000, 3_000_000], "segments": [{"start_us": 200_000, "end_us": 3_000_000, "action": "display"}],
     "answer": {"description": "只有拆开后的定格", "action_phases": [
         {"phase": "settled", "start_us": 200_000, "end_us": 3_000_000, "action": "display", "object": "bag", "result": "opened bag held still", "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "pull-covered", "category": "抽取", "kind": "positive", "duration_us": 7_000_000,
     "evidence_us": [100_000, 2_000_000], "reference": (100_000, 2_000_000),
     "segments": [{"start_us": 100_000, "end_us": 2_000_000, "action": "pull", "result": "one sheet out"}],
     "expect_remote": 0, "expect_complete": True},
    {"id": "pull-head-gap", "category": "抽取", "kind": "positive", "duration_us": 15_000_000,
     "evidence_us": [6_000_000], "reference": (100_000, 1_800_000),
     "segments": [{"start_us": 0, "end_us": 1_800_000, "action": "pull", "result": "one sheet out"}],
     "answer": {"description": "抽取发生在片头", "action_phases": [
         {"phase": "execute", "start_us": 200_000, "end_us": 1_200_000, "action": "pull", "object": "sheet", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 1_200_000, "end_us": 1_700_000, "action": "pull", "object": "sheet", "result": "one sheet out", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
    {"id": "pull-short", "category": "抽取", "kind": "positive", "duration_us": 3_000_000,
     "evidence_us": [50_000, 400_000, 900_000], "reference": (50_000, 900_000),
     "segments": [{"start_us": 80_000, "end_us": 850_000, "action": "pull", "result": "sheet out"}],
     "expect_remote": 0, "expect_complete": True},
    {"id": "pull-wrong-object", "category": "抽取", "kind": "negative", "duration_us": 8_000_000,
     "evidence_us": [100_000, 2_000_000], "segments": [{"start_us": 100_000, "end_us": 2_000_000, "action": "pull"}],
     "answer": {"description": "抽出的是纸巾包装盒而不是巾体", "action_phases": [
         {"phase": "execute", "start_us": 100_000, "end_us": 2_000_000, "action": "pull", "object": "carton", "result": None, "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "absorb-covered", "category": "吸水", "kind": "positive", "duration_us": 9_000_000,
     "evidence_us": [200_000, 3_000_000, 5_000_000], "reference": (200_000, 5_000_000),
     "segments": [{"start_us": 200_000, "end_us": 5_000_000, "action": "absorb", "result": "liquid gone"}],
     "expect_remote": 0, "expect_complete": True},
    {"id": "absorb-head-gap", "category": "吸水", "kind": "positive", "duration_us": 20_000_000,
     "evidence_us": [9_000_000], "reference": (0, 3_000_000),
     "segments": [{"start_us": 0, "end_us": 3_000_000, "action": "absorb", "result": "liquid gone"}],
     "answer": {"description": "倒液和吸入都在片头", "action_phases": [
         {"phase": "execute", "start_us": 100_000, "end_us": 1_500_000, "action": "pour", "object": "liquid", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 1_500_000, "end_us": 2_800_000, "action": "absorb", "object": "sheet", "result": "liquid gone", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
    {"id": "absorb-tail", "category": "吸水", "kind": "positive", "duration_us": 12_000_000,
     "evidence_us": [500_000, 2_000_000], "reference": (500_000, 8_000_000),
     "segments": [{"start_us": 500_000, "end_us": 8_200_000, "action": "absorb", "result": "surface dry"}],
     "answer": {"description": "补上吸干结果", "action_phases": [
         {"phase": "execute", "start_us": 600_000, "end_us": 4_000_000, "action": "absorb", "object": "liquid", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 4_000_000, "end_us": 8_100_000, "action": "absorb", "object": "surface", "result": "surface dry", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
    {"id": "absorb-meter", "category": "吸水", "kind": "negative", "duration_us": 8_000_000,
     "evidence_us": [200_000, 4_000_000], "segments": [{"start_us": 200_000, "end_us": 4_000_000, "action": "show meter"}],
     "answer": {"description": "仪器示数不是擦拭过程", "action_phases": [
         {"phase": "settled", "start_us": 200_000, "end_us": 4_000_000, "action": "show meter", "object": "gauge", "result": "0.00", "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "light-covered", "category": "透光", "kind": "positive", "duration_us": 6_000_000,
     "evidence_us": [100_000, 2_500_000], "reference": (100_000, 2_500_000),
     "segments": [{"start_us": 100_000, "end_us": 2_500_000, "action": "hold to light", "result": "light through sheet"}],
     "expect_remote": 0, "expect_complete": True},
    {"id": "light-gap", "category": "透光", "kind": "positive", "duration_us": 11_000_000,
     "evidence_us": [7_000_000], "reference": (200_000, 2_000_000),
     "segments": [{"start_us": 0, "end_us": 2_000_000, "action": "hold to light", "result": "light through sheet"}],
     "answer": {"description": "透光动作在片头", "action_phases": [
         {"phase": "execute", "start_us": 300_000, "end_us": 1_200_000, "action": "hold to light", "object": "sheet", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 1_200_000, "end_us": 1_900_000, "action": "hold to light", "object": "sheet", "result": "light through sheet", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
    {"id": "light-as-absorb", "category": "透光", "kind": "negative", "duration_us": 6_000_000,
     "evidence_us": [100_000, 2_000_000], "segments": [{"start_us": 100_000, "end_us": 2_000_000, "action": "hold to light"}],
     "answer": {"description": "透光不能当成吸水", "action_phases": [
         {"phase": "execute", "start_us": 100_000, "end_us": 2_000_000, "action": "hold to light", "object": "sheet", "result": None, "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "light-prop", "category": "透光", "kind": "negative", "duration_us": 5_000_000,
     "evidence_us": [200_000, 2_000_000], "segments": [{"start_us": 200_000, "end_us": 2_000_000, "action": "point lamp"}],
     "answer": {"description": "只是灯具示意", "action_phases": [
         {"phase": "settled", "start_us": 200_000, "end_us": 2_000_000, "action": "point lamp", "object": "lamp", "result": "lamp on", "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "wipe-covered", "category": "擦拭", "kind": "positive", "duration_us": 8_000_000,
     "evidence_us": [150_000, 2_000_000, 4_000_000], "reference": (150_000, 4_000_000),
     "segments": [{"start_us": 150_000, "end_us": 4_000_000, "action": "wipe", "result": "stain removed"}],
     "expect_remote": 0, "expect_complete": True},
    {"id": "wipe-head-gap", "category": "擦拭", "kind": "positive", "duration_us": 18_000_000,
     "evidence_us": [8_000_000], "reference": (0, 3_500_000),
     "segments": [{"start_us": 0, "end_us": 3_500_000, "action": "wipe", "result": "stain removed"}],
     "answer": {"description": "擦拭从片头开始并擦净", "action_phases": [
         {"phase": "execute", "start_us": 200_000, "end_us": 2_000_000, "action": "wipe", "object": "stain", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 2_000_000, "end_us": 3_400_000, "action": "wipe", "object": "surface", "result": "stain removed", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
    {"id": "wipe-clean-hand", "category": "擦拭", "kind": "positive", "duration_us": 9_000_000,
     "evidence_us": [100_000, 3_000_000], "reference": (100_000, 6_000_000),
     "segments": [{"start_us": 100_000, "end_us": 6_200_000, "action": "wipe", "result": "clean hand"}],
     "answer": {"description": "补上干净手部", "action_phases": [
         {"phase": "execute", "start_us": 150_000, "end_us": 3_000_000, "action": "wipe", "object": "hand", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 3_000_000, "end_us": 6_100_000, "action": "wipe", "object": "hand", "result": "clean hand", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
    {"id": "wipe-settled-result", "category": "擦拭", "kind": "negative", "duration_us": 7_000_000,
     "evidence_us": [200_000, 3_000_000], "segments": [{"start_us": 200_000, "end_us": 3_000_000, "action": "hold clean sheet"}],
     "answer": {"description": "只有擦完后的定格", "action_phases": [
         {"phase": "settled", "start_us": 200_000, "end_us": 3_000_000, "action": "hold clean sheet", "object": "sheet", "result": "clean sheet", "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "pack-front", "category": "包装", "kind": "positive", "duration_us": 5_000_000,
     "evidence_us": [100_000, 2_000_000], "reference": (100_000, 2_000_000),
     "segments": [{"start_us": 100_000, "end_us": 2_000_000, "action": "show pack", "result": "front label visible"}],
     "expect_remote": 0, "expect_complete": True},
    {"id": "pack-point", "category": "包装", "kind": "positive", "duration_us": 8_000_000,
     "evidence_us": [5_000_000], "reference": (200_000, 1_500_000),
     "segments": [{"start_us": 0, "end_us": 1_500_000, "action": "point label", "result": "label readable"}],
     "answer": {"description": "指向包装文字", "action_phases": [
         {"phase": "execute", "start_us": 250_000, "end_us": 900_000, "action": "point label", "object": "label", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 900_000, "end_us": 1_400_000, "action": "point label", "object": "label", "result": "label readable", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
    {"id": "pack-as-function", "category": "包装", "kind": "negative", "duration_us": 6_000_000,
     "evidence_us": [100_000, 2_500_000], "segments": [{"start_us": 100_000, "end_us": 2_500_000, "action": "show pack"}],
     "answer": {"description": "包装不能证明吸水", "action_phases": [
         {"phase": "settled", "start_us": 100_000, "end_us": 2_500_000, "action": "show pack", "object": "pack", "result": "pack visible", "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "pack-blur", "category": "包装", "kind": "negative", "duration_us": 6_000_000,
     "evidence_us": [200_000, 3_000_000], "segments": [{"start_us": 200_000, "end_us": 3_000_000, "action": "show side"}],
     "answer": {"description": "文字看不清", "action_phases": [
         {"phase": "execute", "start_us": 200_000, "end_us": 3_000_000, "action": "show side", "object": "pack", "result": None, "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "opposite-pour", "category": "相反语义", "kind": "negative", "duration_us": 8_000_000,
     "evidence_us": [100_000, 3_000_000], "segments": [{"start_us": 100_000, "end_us": 3_000_000, "action": "pour away"}],
     "answer": {"description": "液体被倒掉而不是吸入", "action_phases": [
         {"phase": "execute", "start_us": 100_000, "end_us": 2_000_000, "action": "pour away", "object": "liquid", "complete": True, "evidence_role": "demonstration"},
         {"phase": "result", "start_us": 2_000_000, "end_us": 3_000_000, "action": "pour away", "object": "liquid", "result": "liquid discarded", "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "opposite-stack", "category": "相反语义", "kind": "negative", "duration_us": 7_000_000,
     "evidence_us": [200_000, 2_000_000], "segments": [{"start_us": 200_000, "end_us": 2_000_000, "action": "scatter"}],
     "answer": {"description": "巾体被散开而不是成堆", "action_phases": [
         {"phase": "result", "start_us": 200_000, "end_us": 2_000_000, "action": "scatter", "object": "sheets", "result": "sheets scattered", "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "opposite-covered-reject", "category": "相反语义", "kind": "negative", "duration_us": 9_000_000,
     "evidence_us": [100_000, 4_000_000], "segments": [{"start_us": 100_000, "end_us": 4_000_000, "action": "squeeze water out"}],
     "answer": {"description": "挤出水来，不能当作吸水", "action_phases": [
         {"phase": "execute", "start_us": 100_000, "end_us": 2_000_000, "action": "squeeze water out", "object": "sheet", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 2_000_000, "end_us": 4_000_000, "action": "squeeze water out", "object": "sheet", "result": "water expelled", "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 0, "expect_complete": False},
    {"id": "opposite-gap-reject", "category": "相反语义", "kind": "negative", "duration_us": 16_000_000,
     "evidence_us": [9_000_000], "segments": [{"start_us": 0, "end_us": 2_000_000, "action": "wipe onto stain"}],
     "answer": {"description": "缺口里是把污渍涂开", "action_phases": [
         {"phase": "execute", "start_us": 200_000, "end_us": 1_500_000, "action": "spread stain", "object": "stain", "complete": True, "evidence_role": "demonstration"}]},
     "expect_remote": 1, "expect_complete": False},
    {"id": "boundary-tight", "category": "擦拭", "kind": "positive", "duration_us": 4_000_000,
     "evidence_us": [50_000, 1_200_000], "reference": (50_000, 1_200_000),
     "segments": [{"start_us": 40_000, "end_us": 1_250_000, "action": "wipe", "result": "dry"}],
     "expect_remote": 0, "expect_complete": True},
    {"id": "boundary-just-over", "category": "抽取", "kind": "positive", "duration_us": 10_000_000,
     "evidence_us": [700_000, 2_000_000], "reference": (0, 2_000_000),
     "segments": [{"start_us": 0, "end_us": 2_000_000, "action": "pull", "result": "sheet out"}],
     "answer": {"description": "补上 0.7 秒以前的起手", "action_phases": [
         {"phase": "prepare", "start_us": 0, "end_us": 200_000, "action": "reach", "object": "sheet", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "execute", "start_us": 200_000, "end_us": 1_000_000, "action": "pull", "object": "sheet", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 1_000_000, "end_us": 1_900_000, "action": "pull", "object": "sheet", "result": "sheet out", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
    {"id": "head-miss-141s", "category": "片头漏采", "kind": "positive", "duration_us": 141_130_000,
     "evidence_us": [8_820_000], "reference": (0, 2_000_000),
     "segments": [{"start_us": 0, "end_us": 2_000_000, "action": "pour", "result": "liquid on sheet"}],
     "answer": {"description": "倒液体发生在第一帧之前", "action_phases": [
         {"phase": "execute", "start_us": 200_000, "end_us": 1_200_000, "action": "pour", "object": "liquid", "complete": True, "evidence_role": "direct_evidence"},
         {"phase": "result", "start_us": 1_200_000, "end_us": 1_900_000, "action": "pour", "object": "sheet", "result": "liquid on sheet", "complete": True, "evidence_role": "direct_evidence"}]},
     "expect_remote": 1, "expect_complete": True},
]


def evaluate_case(case: dict[str, Any], *, prior_signatures: set[str] | None = None) -> dict[str, Any]:
    prior_signatures = prior_signatures if prior_signatures is not None else set()
    frames = [{"frame_id": f"f{index}", "timestamp_us": stamp} for index, stamp in enumerate(case["evidence_us"])]
    record = {"provenance": {"evidence": [frame["frame_id"] for frame in frames]},
              "observation": {"action_segments": case.get("segments") or [],
                              "action_phases": case.get("phases") or [],
                              "description": case["id"]},
              "start_us": 0, "end_us": case["duration_us"]}
    intervals = gap_intervals(record, frames, duration_us=case["duration_us"])
    signature = gap_signature(case["id"], intervals) if intervals else ""
    remote = 1 if intervals and signature not in prior_signatures else 0
    times = sample_gap(intervals) if intervals else []
    if remote and signature:
        prior_signatures.add(signature)
    if case.get("answer"):
        verdict = segment_fields(case["answer"])
        predicted = (verdict["action_phases"][0]["start_us"], verdict["action_phases"][-1]["end_us"]) if verdict["action_phases"] else None
    else:
        segments = case.get("segments") or []
        has_result = any(segment.get("result") for segment in segments)
        verdict = {"segment_complete": bool(has_result and not intervals),
                   "action_complete": bool(has_result and not intervals),
                   "evidence_role": "direct_evidence" if has_result and not intervals else "demonstration",
                   "settled_only": False, "action_phases": []}
        predicted = (segments[0]["start_us"], segments[-1]["end_us"]) if segments else None
    reference = case.get("reference")
    error = boundary_error(predicted[0], predicted[1], reference[0], reference[1]) if reference and predicted else None
    return {"id": case["id"], "category": case["category"], "kind": case["kind"],
            "remote_requests": remote, "intervals_us": intervals, "sample_us": times,
            "samples_inside_gap": all(_inside(stamp, intervals) for stamp in times),
            "sample_count": len(times), "segment_complete": verdict["segment_complete"],
            "evidence_role": verdict.get("evidence_role"), "boundary": error,
            "signature": signature}
