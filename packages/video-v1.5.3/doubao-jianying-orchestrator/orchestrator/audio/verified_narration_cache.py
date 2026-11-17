"""Cross-task cache of finalized narration with explicit synthesis identity."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid

from . import narration_preparation
from ..runtime.file_safety import atomic_write_text, content_identity, file_lock


def cache_root():
    explicit = os.environ.get("JY_TTS_VERIFIED_CACHE")
    shared = os.environ.get("JY_VISION_CACHE_DIR")
    work_root = os.environ.get("JY_WORK_ROOT")
    if explicit:
        return Path(explicit)
    if shared:
        return Path(shared) / "verified-narration"
    return Path(work_root) / ".cache" / "verified-narration" if work_root else None


def identity(text, speaker, tempo, loudness, edge_trim):
    from .voice_tts import VOICE_POST_VERSION, VOICE_SILENCE_THRESHOLD_DB
    from ..selection.timing_contract import AUDIO_PIPELINE_VERSION
    return {"version": 1, "text": text, "speaker": speaker, "tempo": tempo, "backend": "sami",
            "pipeline": AUDIO_PIPELINE_VERSION, "post_version": VOICE_POST_VERSION,
            "post_threshold_db": VOICE_SILENCE_THRESHOLD_DB,
            "policy": narration_preparation.settings(loudness, edge_trim=edge_trim)}


def _directory(root, data):
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return Path(root) / digest


def lookup(root, data):
    if root is None:
        return None
    directory = _directory(root, data)
    try:
        index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
        if index["identity"] != data:
            return None
        path = directory / index["file"]
        if path.parent != directory or not path.name.startswith(narration_preparation.VERSION + "-"):
            return None
        checked = narration_preparation.prepared(path, data["policy"])
        if checked and checked["output"]["sha256"] == index["sha256"]:
            return str(path.resolve())
    except (OSError, KeyError, TypeError, ValueError):
        return None
    return None


def remember(root, data, audio, *, provenance=None):
    if root is None:
        return
    source = Path(audio)
    record = narration_preparation.prepared(source, data["policy"])
    if not record:
        return
    directory = _directory(root, data)
    with file_lock(directory / "index.json", timeout=120):
        if lookup(root, data):
            return
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / source.name
        temporary = directory / ("." + uuid.uuid4().hex + ".tmp")
        try:
            shutil.copyfile(source, temporary)
            if content_identity(temporary)["sha256"] != record["output"]["sha256"]:
                raise ValueError("VERIFIED_NARRATION_SOURCE_CHANGED")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        copied = dict(record, path=str(target.resolve()), output=content_identity(target))
        atomic_write_text(str(target) + ".json", json.dumps(copied, ensure_ascii=False))
        atomic_write_text(directory / "index.json", json.dumps({"identity": data, "file": target.name,
            "sha256": copied["output"]["sha256"], "provenance": provenance}, ensure_ascii=False))


def import_history(task_file, planned_file, timing_file, preflight_file, root):
    """Import a prior task only when its text, voice and processed audio agree."""
    from ..selection.fixed_narration import fixed_narration_segments
    from ..policy.production_parameters import silence_edges, gap_errors
    from ..runtime.platform_env import find_ffmpeg
    from ..selection import sentence_planner
    from . import voice_tts
    from .voice_request import resolve_voice_request

    def read(path):
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))

    task, planned, timing, preflight = map(read, (task_file, planned_file, timing_file, preflight_file))
    task_id = str(task.get("record_id") or task.get("task_id") or "")
    if (not task_id or planned.get("task_id") != task_id or planned.get("record_id") != task_id
            or not task.get("voice") or task["voice"] != planned.get("voice")
            or task["voice"] != timing.get("voice")):
        raise ValueError("NARRATION_IMPORT_TASK_IDENTITY_MISMATCH")
    if ("".join(str(task.get("script") or "").split())
            != "".join(str(planned.get("full_script") or "").split())):
        raise ValueError("NARRATION_IMPORT_SCRIPT_MISMATCH")
    request = resolve_voice_request(task["voice"])
    if request.tempo != planned.get("voice_tempo"):
        raise ValueError("NARRATION_IMPORT_TEMPO_MISMATCH")
    segments = fixed_narration_segments(task)
    rows, accepted = timing.get("rows"), preflight.get("rows")
    if segments is None and isinstance(rows, list):
        planned_text = [row.get("text") for row in planned.get("segments", [])]
        validated = sentence_planner.validate(task["script"], {"segments": planned_text})
        if len(validated) != len(rows):
            raise ValueError("NARRATION_IMPORT_UNVERIFIED_TIMING")
        segments = []
        for row, text in zip(rows, validated):
            audio = Path(str(row.get("tts_path") or ""))
            if not audio.is_absolute() or not audio.is_file():
                raise ValueError("NARRATION_IMPORT_AUDIO_MISSING")
            segments.append({"text": text["text"], "audio": str(audio.resolve())})
    if (not segments or not isinstance(rows, list) or not isinstance(accepted, list)
            or len(rows) != len(segments) or len(accepted) != len(segments)
            or preflight.get("status") != "PASS" or preflight.get("issues")
            or preflight.get("stage") != "before_selection"
            or preflight.get("measurement_version") != "contiguous-eof-v2"
            or "".join("".join(str(row.get("text") or "").split()) for row in planned.get("segments", []))
               != "".join("".join(s["text"].split()) for s in segments)):
        raise ValueError("NARRATION_IMPORT_UNVERIFIED_TIMING")
    loudness = planned.get("narration_loudness") or {}
    edge_trim = bool(planned.get("voice_edge_trim", True))
    policy = narration_preparation.settings(loudness, edge_trim=edge_trim)
    fps = float(preflight.get("fps") or 0)
    maximum = preflight.get("max_gap_frames")
    if maximum != (planned.get("rulepack") or {}).get("voice_acceptance_max_gap_frames"):
        raise ValueError("NARRATION_IMPORT_GATE_MISMATCH")
    ffmpeg = str(find_ffmpeg()[0])
    verified, edges, cursor = [], [], 0.0
    for index, (segment, row, accepted_row) in enumerate(zip(segments, rows, accepted), 1):
        audio = Path(segment["audio"]).resolve()
        if (row.get("index") != index or row.get("text") != segment["text"]
                or row.get("key") != "s%d" % index or row.get("timing_certainty") != "audio"
                or row.get("probe_error") or Path(str(row.get("tts_path") or "")).resolve() != audio
                or accepted_row.get("sentence") != index
                or Path(str(accepted_row.get("path") or "")).resolve() != audio):
            raise ValueError("NARRATION_IMPORT_SEGMENT_MISMATCH:S%d" % index)
        record = narration_preparation.prepared(audio, policy)
        if not record or record["duration_us"] != row.get("audio_duration_us"):
            raise ValueError("NARRATION_IMPORT_RECEIPT_MISMATCH:S%d" % index)
        processed = Path(record["source"]["path"]).resolve()
        if content_identity(processed)["sha256"] != record["source"]["sha256"]:
            raise ValueError("NARRATION_IMPORT_SOURCE_CHANGED:S%d" % index)
        raw = processed.parent / voice_tts._cache_name(segment["text"], request.preset.sami)
        marker = Path(str(raw) + ".backend")
        if (not raw.is_file() or not marker.is_file() or marker.read_text(encoding="utf-8").strip() != "sami"):
            raise ValueError("NARRATION_IMPORT_RAW_IDENTITY_MISSING:S%d" % index)
        expected = (voice_tts._post_name(raw, request.tempo, voice_tts.VOICE_EDGE_KEEP_MS, False)
                    if abs(request.tempo - 1.0) >= 1e-6 else raw)
        if expected.resolve() != processed:
            raise ValueError("NARRATION_IMPORT_RAW_IDENTITY_MISMATCH:S%d" % index)
        duration = record["duration_us"] / 1_000_000
        measured = silence_edges(str(audio), start_s=0, duration_s=duration, ffmpeg=ffmpeg)
        if measured.get("all_silent"):
            raise ValueError("NARRATION_IMPORT_ALL_SILENT:S%d" % index)
        edges.append({**measured, "sentence": index, "start_s": cursor, "end_s": cursor + duration})
        cursor += duration
        verified.append((segment, audio, record, content_identity(raw)["sha256"]))
    if gap_errors(edges, fps=fps, max_frames=maximum):
        raise ValueError("NARRATION_IMPORT_GAP_EXCEEDED")
    imported = []
    for segment, audio, record, raw_sha256 in verified:
        data = identity(segment["text"], request.preset.sami, request.tempo, loudness, edge_trim)
        provenance = {"task_id": task_id, "task_file": str(Path(task_file).resolve()),
                      "timing_file": str(Path(timing_file).resolve()),
                      "preflight_file": str(Path(preflight_file).resolve()),
                      "original_audio": str(audio), "original_sha256": record["output"]["sha256"],
                      "raw_sha256": raw_sha256}
        was_cached = bool(lookup(root, data))
        remember(root, data, audio, provenance=provenance)
        cached = lookup(root, data)
        if not cached:
            raise ValueError("NARRATION_IMPORT_CACHE_WRITE_FAILED")
        cache_sha256 = content_identity(cached)["sha256"]
        imported.append({"text": segment["text"], "source_sha256": record["output"]["sha256"],
                         "cached_sha256": cache_sha256, "matched_before_import": was_cached})
    return {"status": "PASS", "task_id": task_id, "imported": imported,
            "voice": task["voice"], "tempo": request.tempo, "cache": str(Path(root).resolve())}


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Import verified historical narration from one task")
    parser.add_argument("--task", required=True)
    parser.add_argument("--planned-manifest", required=True)
    parser.add_argument("--timing", required=True)
    parser.add_argument("--preflight", required=True)
    parser.add_argument("--cache-root", required=True)
    args = parser.parse_args()
    print(json.dumps(import_history(args.task, args.planned_manifest, args.timing, args.preflight,
                                    Path(args.cache_root)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
