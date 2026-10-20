"""Opt-in experimental planner adapter; observations are external, never invented here.

This module validates identities, observed evidence and geometry. It neither runs a
model nor reselects a shot. A valid receipt authorizes local draft construction,
not publication or a claim that the saved video has passed human review.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

US = 1_000_000


class ContextualPlanError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_CONTEXTUAL_PLAN", **details: Any):
        super().__init__(message)
        self.code, self.details = code, details

    def as_dict(self) -> dict:
        return {"type": self.code, "message": str(self), **self.details}


def requested(manifest: dict) -> bool:
    return "contextual_plan" in manifest or manifest.get("experiment_arm") == "B"


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ContextualPlanError(message)


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    _require(type(value) is int and value >= minimum, f"{name}: expected integer >= {minimum}")
    return value


def _path(value: Any, base: Path) -> Path:
    _require(isinstance(value, str) and bool(value.strip()), "missing file path")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else base / path).absolute()


def _identity(path: Path) -> str:
    return str(path.resolve()).casefold()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _binding(path: Path, expected: Any, cache: dict[str, str]) -> None:
    _require(isinstance(expected, str) and len(expected) == 64,
             f"missing SHA-256: {path}")
    key = _identity(path)
    if key not in cache:
        cache[key] = _hash(path)
    _require(cache[key] == expected.lower(), f"SHA-256 mismatch: {path}")


def _receipt(path: Path, expected: Any, cache: dict[str, str]) -> dict:
    _binding(path, expected, cache)
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    _require(isinstance(value, dict), f"expected object: {path}")
    return value


def _observed_evidence(record: dict, base: Path, start: int, end: int,
                       cache: dict[str, str]) -> list[dict]:
    evidence = record.get("evidence")
    _require(isinstance(evidence, list) and evidence, "observation requires evidence files")
    result = []
    for row in evidence:
        _require(isinstance(row, dict), "invalid evidence record")
        timestamp = _integer(row.get("timestamp_us"), "timestamp_us")
        _require(start <= timestamp <= end, "evidence timestamp outside observed source window")
        path = _path(row.get("path"), base)
        _binding(path, row.get("sha256"), cache)
        _require(path.stat().st_size > 0, "empty observation evidence")
        result.append({**row, "path": str(path), "timestamp_us": timestamp})
    return result


@dataclass
class ContextualPlan:
    plan: dict
    observer: dict
    segments: list[dict]
    rows: list[dict]
    groups: list[dict]
    references: dict
    tolerance_us: int
    bindings: list[tuple[Path, str]]

    def report(self) -> dict:
        return {"schema": "contextual_build_observation.v1",
                "plan_sha256": self.references["plan_sha256"],
                "observer_sha256": self.references["observer_sha256"],
                "planner_id": self.plan["planner_id"],
                "reviewer_id": self.observer["reviewer_id"],
                "observations": self.observer["observations"],
                "groups": self.observer.get("groups", []),
                "old_matcher_calls": 0, "old_scene_swap_calls": 0,
                "old_semantic_policy_calls": 0,
                "scope": "external observations accepted for local construction; saved output still requires review",
                "formal_delivery_allowed": False, "human_review": "PENDING"}

    def align(self, clips: list, audio_probe, material_probe, tail_pad_us: int) -> list[dict]:
        """Execute immutable windows after independent duration probes, without a solver."""
        issues, staged = [], []
        if len(clips) != len(self.rows):
            return [{"type": "CONTEXTUAL_CLIP_MAPPING_CHANGED"}]
        for index, (clip, row) in enumerate(zip(clips, self.rows), 1):
            audio_us = round(float(audio_probe(clip.audio)["duration"]) * US)
            material_s = float(material_probe(clip))
            material_us = round(material_s * US)
            if (audio_us <= 0 or abs(audio_us - row["audio_duration_us"]) > self.tolerance_us
                    or row["duration_us"] + 1000 < audio_us + tail_pad_us
                    or abs(row["duration_us"] - audio_us - tail_pad_us) > self.tolerance_us):
                issues.append({"index": index, "type": "CONTEXTUAL_AUDIO_DURATION_CHANGED",
                               "actual_audio_us": audio_us, "planned_audio_us": row["audio_duration_us"]})
            consumed_end_us = row["source_start_us"] + round(row["duration_us"] * row["speed"])
            if (not math.isfinite(material_s)
                    or max(row["source_end_us"], consumed_end_us) > material_us + 1):
                issues.append({"index": index, "type": "CONTEXTUAL_SOURCE_OUT_OF_BOUNDS",
                               "source_end_us": row["source_end_us"], "material_us": material_us})
            staged.append((clip, row, audio_us, material_s))
        if issues:
            return issues
        for clip, row, audio_us, material_s in staged:
            clip.video_duration = material_s
            clip.runtime_audio_us = audio_us
            clip.source_start = row["source_start_us"] / US
            clip.source_end = row["source_end_us"] / US
            clip.duration = row["duration_us"] / US
            clip.start_us = row["timeline_start_us"]
            clip.video_speed = row["speed"]
            clip.solved_window = copy.deepcopy(row["window_solution"])
        return []

    def final_issues(self, clips: list, *, verify_files: bool = True) -> list[dict]:
        issues = []
        if len(clips) != len(self.rows):
            return [{"type": "CONTEXTUAL_CLIP_MAPPING_CHANGED"}]
        for index, (clip, row) in enumerate(zip(clips, self.rows), 1):
            actual = [round(float(clip.source_start) * US),
                      round((float(clip.source_start) + float(clip.duration) * float(clip.video_speed)) * US)]
            expected = [row["source_start_us"], row["source_end_us"]]
            if (_identity(Path(clip.video)) != _identity(Path(row["video"]))
                    or _identity(Path(clip.audio)) != _identity(Path(row["audio"]))
                    or clip.text != row["text"] or getattr(clip, "visual_missing", False)
                    or abs(int(clip.start_us) - row["timeline_start_us"]) > 1
                    or abs(round(float(clip.duration) * US) - row["duration_us"]) > 1
                    or abs(float(clip.video_speed) - row["speed"]) > 1e-9
                    or abs(actual[0] - expected[0]) > 1
                    or abs(actual[1] - expected[1]) > self.tolerance_us):
                issues.append({"index": index, "type": "CONTEXTUAL_LOCKED_PLAN_CHANGED",
                               "expected_source_us": expected, "actual_source_us": actual})
        if verify_files:
            cache: dict[str, str] = {}
            try:
                for path, digest in self.bindings:
                    _binding(path, digest, cache)
            except (OSError, ContextualPlanError) as exc:
                issues.append({"type": "CONTEXTUAL_BOUND_FILE_CHANGED", "message": str(exc)})
        return issues

    def readback_issues(self, draft: dict, *, base: Path | None = None) -> list[dict]:
        """Compare saved Jianying windows to the locked plan, never re-solve them."""
        materials = {str(v.get("id")): v for v in
                     (draft.get("materials") or {}).get("videos", []) if isinstance(v, dict)}
        speeds = {str(v.get("id")): v for v in
                  (draft.get("materials") or {}).get("speeds", []) if isinstance(v, dict)}
        tracks = [t for t in draft.get("tracks", []) if isinstance(t, dict)
                  and t.get("type") == "video"]
        if len(tracks) != 1:
            return [{"type": "CONTEXTUAL_READBACK_VIDEO_TRACK_COUNT", "actual": len(tracks)}]
        segments = sorted(tracks[0].get("segments", []),
                          key=lambda s: (s.get("target_timerange") or {}).get("start", -1))
        if len(segments) != len(self.rows):
            return [{"type": "CONTEXTUAL_READBACK_CLIP_COUNT", "actual": len(segments),
                     "expected": len(self.rows)}]
        issues = []
        for index, (actual, row) in enumerate(zip(segments, self.rows), 1):
            material = materials.get(str(actual.get("material_id")), {})
            source, target = actual.get("source_timerange") or {}, actual.get("target_timerange") or {}
            try:
                video = _path(material.get("path"), base or Path.cwd())
                source_start = _integer(source.get("start"), "readback.source_start")
                source_end = source_start + _integer(source.get("duration"), "readback.source_duration", 1)
                target_start = _integer(target.get("start"), "readback.target_start")
                target_duration = _integer(target.get("duration"), "readback.target_duration", 1)
                same_media = (_identity(video) == _identity(Path(row["video"]))
                              or (video.is_file() and _hash(video) == row["video_sha256"]))
                actual_speeds = [float(actual["speed"])] if "speed" in actual else []
                actual_speeds.extend(float(speeds[str(ref)]["speed"])
                                     for ref in actual.get("extra_material_refs", []) if str(ref) in speeds)
                valid = (same_media
                         and all(math.isfinite(v) and abs(v - row["speed"]) <= 1e-6 for v in actual_speeds)
                         and abs(source_start - row["source_start_us"]) <= 1
                         and abs(source_end - row["source_end_us"]) <= self.tolerance_us
                         and abs(target_start - row["timeline_start_us"]) <= 1
                         and abs(target_duration - row["duration_us"]) <= self.tolerance_us)
            except (ValueError, OSError, TypeError):
                valid = False
            if not valid:
                issues.append({"type": "CONTEXTUAL_READBACK_LOCK_CHANGED", "index": index})
        return issues


def load(manifest: dict, base: Path) -> ContextualPlan:
    """Validate the exact opt-in contract and compile ordinary per-sentence clips."""
    _require(manifest.get("experiment_arm") == "B" and manifest.get("delivery_mode") == "formal",
             "contextual adapter requires experiment_arm=B and formal mode")
    _require(not manifest.get("temporary_material_analysis") and not manifest.get("auto_match_shots"),
             "contextual adapter must not run legacy matching")
    _require(not manifest.get("auto_transition"), "contextual plan requires explicit hard cuts")
    refs = manifest.get("contextual_plan")
    _require(isinstance(refs, dict), "contextual_plan object required")
    cache: dict[str, str] = {}
    plan_path = _path(refs.get("plan_file"), base)
    observer_path = _path(refs.get("observer_file"), base)
    plan = _receipt(plan_path, refs.get("plan_sha256"), cache)
    observer = _receipt(observer_path, refs.get("observer_sha256"), cache)
    _require(plan.get("schema") == "contextual_plan.v1", "unknown planner schema")
    _require(observer.get("schema") == "contextual_observer.v1", "unknown observer schema")
    _require(observer.get("plan_sha256") == refs["plan_sha256"], "observer is for a different plan")
    _require(observer.get("method") == "independent_visual_review", "independent visual observation required")
    _require(bool(str(plan.get("planner_id") or "").strip()), "planner_id required")
    _require(bool(str(observer.get("reviewer_id") or "").strip())
             and observer["reviewer_id"] != plan["planner_id"], "independent reviewer_id required")
    _require(bool(plan.get("task_id")) and plan["task_id"] == str(
        manifest.get("task_id") or manifest.get("record_id") or ""), "task identity mismatch")
    fps = float(manifest.get("fps") or 30)
    _require(math.isfinite(fps) and fps > 0, "invalid fps")
    tolerance = round(US / fps)
    pad_s = manifest.get("tail_pad_s", .12)
    pad_us = round(float(pad_s) * US)
    _require(pad_us >= 0, "invalid tail padding")
    originals, rows = manifest.get("segments"), plan.get("segments")
    _require(isinstance(originals, list) and isinstance(rows, list) and rows
             and len(rows) == len(originals), "planner must preserve all frozen narration segments")
    observations = observer.get("observations")
    _require(isinstance(observations, list) and len(observations) == len(rows),
             "every segment requires a visual observation")
    by_index = {o.get("index"): o for o in observations if isinstance(o, dict)}
    _require(len(by_index) == len(rows), "duplicate or missing observation index")
    compiled, normalized, cursor = [], [], 0
    bindings = [(plan_path, refs["plan_sha256"]), (observer_path, refs["observer_sha256"])]
    for index, (row, original) in enumerate(zip(rows, originals), 1):
        _require(isinstance(row, dict) and isinstance(original, dict), "invalid segment")
        _require(row.get("index") == index and row.get("text") == original.get("text")
                 and bool(str(row.get("text") or "").strip()), "frozen narration text/order changed")
        _require(not original.get("multi_shot") and not original.get("span_parts"),
                 "contextual adapter requires ordinary frozen sentence segments")
        audio = _path(row.get("audio"), plan_path.parent)
        video = _path(row.get("video"), plan_path.parent)
        _require(_identity(audio) == _identity(_path(original.get("audio"), base)),
                 "planner substituted frozen narration file")
        for path, digest in ((audio, row.get("audio_sha256")), (video, row.get("video_sha256"))):
            _binding(path, digest, cache)
            bindings.append((path, digest))
        sound = _integer(row.get("audio_duration_us"), "audio_duration_us", 1)
        _require(sound == original.get("audio_duration_us"), "frozen audio duration changed")
        start = _integer(row.get("source_start_us"), "source_start_us")
        end = _integer(row.get("source_end_us"), "source_end_us", 1)
        duration = _integer(row.get("duration_us"), "duration_us", 1)
        timeline = _integer(row.get("timeline_start_us"), "timeline_start_us")
        speed = float(row.get("speed", 1))
        _require(math.isfinite(speed) and speed > 0 and end > start, "invalid source aperture/speed")
        from .shot_analyzer import EVIDENCE_FIT_SPEED_MAX
        _require(speed <= EVIDENCE_FIT_SPEED_MAX, "contextual speed exceeds configured evidence limit")
        _require(abs(round(duration * speed) - (end - start)) <= tolerance,
                 "source aperture differs from planned duration and speed")
        _require(duration + 1 >= sound + pad_us
                 and abs(duration - sound - pad_us) <= tolerance and timeline == cursor,
                 "planned timeline has narration mismatch/gap/overlap")
        obs = by_index.get(index)
        _require(isinstance(obs, dict) and obs.get("status") in ("supported", "contextual")
                 and bool(str(obs.get("observation") or "").strip()),
                 f"segment {index}: unsupported, unknown or missing independent observation")
        evidence = _observed_evidence(obs, observer_path.parent, start, end, cache)
        for evidence_row in evidence:
            bindings.append((Path(evidence_row["path"]), evidence_row["sha256"]))
        intervals = row.get("evidence_intervals")
        _require(isinstance(intervals, list) and intervals, "observed source intervals required")
        out_intervals = []
        for interval in intervals:
            lo = _integer(interval.get("start_us"), "evidence.start_us")
            hi = _integer(interval.get("end_us"), "evidence.end_us", 1)
            _require(start <= lo < hi <= end, "local evidence must be inside its own slice")
            frame = next((e for e in evidence if lo <= e["timestamp_us"] <= hi), None)
            _require(frame is not None, "evidence interval has no observed frame")
            out_intervals.append({"start": lo / US, "end": hi / US,
                                  "frame_path": frame["path"], "frame_time": frame["timestamp_us"] / US,
                                  "evidence_source": "contextual_observer"})
        solution = {"ok": True, "speed": speed, "source_start": start / US,
                    "source_end": end / US, "segment_s": duration / US,
                    "material_s": (end-start) / US, "handle_room_s": 0,
                    "reason": "", "detail": {"source": "contextual_plan.v1", "index": index}}
        normalized.append({**row, "audio": str(audio), "video": str(video), "speed": speed,
                           "window_solution": solution})
        compiled.append({"text": row["text"], "audio": str(audio), "audio_mode": "file",
                         "audio_duration_us": sound, "timing_certainty": "audio", "video": str(video),
                         "source_start": start / US, "source_end": end / US, "duration": duration / US,
                         "video_speed": speed, "window_solution": solution,
                         "temporary_shot_id": f"contextual_{index:03d}",
                         "visual_role": "contextual_planned", "speed_range_override": [speed, speed],
                         "guard_override": [0.0, 0.0], "head_waste": 0.0, "tail_waste": 0.0,
                         "evidence_intervals": out_intervals,
                         "scene_source_start": start / US, "scene_source_end": end / US,
                         "rationale": obs["observation"]})
        cursor += duration
    groups = plan.get("groups", [])
    group_observations = observer.get("groups", [])
    _require(isinstance(groups, list) and isinstance(group_observations, list), "invalid group observations")
    observed_groups = {g.get("id"): g for g in group_observations if isinstance(g, dict)}
    _require(len(observed_groups) == len(group_observations) == len(groups), "group observation mapping mismatch")
    seen_ids, seen_members = set(), set()
    for group in groups:
        gid, members = group.get("id"), group.get("members")
        _require(isinstance(gid, str) and gid.strip() and gid not in seen_ids, "invalid/duplicate group id")
        _require(isinstance(members, list) and members and all(type(i) is int for i in members)
                 and 1 <= members[0] <= members[-1] <= len(rows)
                 and members == list(range(members[0], members[-1]+1)), "group members must be consecutive")
        _require(not seen_members.intersection(members), "overlapping continuous groups")
        seen_ids.add(gid)
        seen_members.update(members)
        pieces = [normalized[i-1] for i in members]
        for previous, current in zip(pieces, pieces[1:]):
            _require(_identity(Path(previous["video"])) == _identity(Path(current["video"]))
                     and previous["video_sha256"] == current["video_sha256"]
                     and abs(previous["source_end_us"] - current["source_start_us"]) <= tolerance
                     and abs(previous["speed"] - current["speed"]) <= 1e-9,
                     "continuous group restarts/skips/reorders source or changes speed")
        start = _integer(group.get("required_source_start_us"), "group.start_us")
        end = _integer(group.get("required_source_end_us"), "group.end_us", 1)
        _require(pieces[0]["source_start_us"] <= start < end <= pieces[-1]["source_end_us"],
                 "continuous group does not cover required action")
        _require(type(group.get("require_complete_action")) is bool, "group completion requirement must be explicit")
        obs = observed_groups.get(gid)
        accepted_statuses = ("complete",) if group["require_complete_action"] else ("complete", "not_required")
        _require(isinstance(obs, dict) and obs.get("status") in accepted_statuses
                 and bool(str(obs.get("observation") or "").strip()), "group observation unverified")
        evidence = _observed_evidence(obs, observer_path.parent,
                                      pieces[0]["source_start_us"], pieces[-1]["source_end_us"], cache)
        if observer.get("producer") == "indexed_planner":
            _require(obs.get("visible_at_speed") is True,
                     "actual slice must remain intelligible at planned speed")
            preview = _path(obs.get("preview_file"), observer_path.parent)
            _binding(preview, obs.get("preview_sha256"), cache)
            _require(preview.stat().st_size > 0, "empty observer preview")
            bindings.append((preview, obs["preview_sha256"]))
            for record, path_key, sha_key in ((group, "planner_response_file", "planner_response_sha256"),
                                               (obs, "response_file", "response_sha256")):
                response_path = _path(record.get(path_key), observer_path.parent)
                _binding(response_path, record.get(sha_key), cache)
                bindings.append((response_path, record[sha_key]))
        if group["require_complete_action"]:
            stamps = [e["timestamp_us"] for e in evidence]
            boundary_tolerance = tolerance
            if observer.get("producer") == "indexed_planner":
                boundary_tolerance = math.ceil(tolerance * pieces[0]["speed"])
            _require(len(set(stamps)) >= 2 and min(stamps) <= start + tolerance
                     and max(stamps) >= end - boundary_tolerance,
                     "complete action needs observed start and result evidence")
        for evidence_row in evidence:
            bindings.append((Path(evidence_row["path"]), evidence_row["sha256"]))
        for member in members:
            compiled[member-1]["continuous_group"] = copy.deepcopy(group)
    _require(seen_members == set(range(1, len(rows)+1)),
             "every sentence must belong to an independently observed group")
    return ContextualPlan(plan, observer, compiled, normalized, groups, dict(refs), tolerance, bindings)
