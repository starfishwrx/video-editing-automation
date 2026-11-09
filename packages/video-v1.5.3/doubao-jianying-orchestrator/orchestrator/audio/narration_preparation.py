"""Finalize paper narration before sentence timing and selection; never synthesize."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import subprocess
import uuid
import wave

import numpy as np

from . import audio_loudness
from ..runtime.file_safety import atomic_write_text, content_identity, file_lock

VERSION = "paper-narration-v2"
THRESHOLD_DB = -40.0
KEEP_MS = 25
MAX_EDGE_TRIM_S = .5
MIN_RETAINED_RATIO = .6


class NarrationError(ValueError):
    def __init__(self, reason, *, status="NARRATION_AUDIO_UNVERIFIED", report=None):
        super().__init__(reason)
        self.status, self.report = status, report

    def result(self):
        return {"status": self.status, "failure_code": str(self),
                "formal_delivery_allowed": False, "narration_gap": self.report}


def settings(loudness=None, *, edge_trim=True):
    spec = loudness if isinstance(loudness, dict) else {}
    if set(spec) - {"enabled", "target_lufs", "gain_db"}:
        raise NarrationError("NARRATION_LOUDNESS_OPTION_UNSUPPORTED", status="NARRATION_PREPARATION_FAILED")
    values = [spec.get("target_lufs", audio_loudness.TARGET_LUFS), spec.get("gain_db", 0.)]
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise NarrationError("NARRATION_LOUDNESS_INVALID", status="NARRATION_PREPARATION_FAILED")
    target = sum(values)
    return {"version": VERSION, "normalization": audio_loudness.NORMALIZATION_VERSION,
            "enabled": spec.get("enabled") is not False, "target_lufs": target,
            "edge_trim": bool(edge_trim), "threshold_db": THRESHOLD_DB, "keep_ms": KEEP_MS,
            "max_edge_trim_s": MAX_EDGE_TRIM_S, "min_retained_ratio": MIN_RETAINED_RATIO,
            "sample_rate": 48000, "channels": 2, "codec": "pcm_s16le"}


def pcm_edges(path):
    """Only contiguous boundary samples below threshold; never merge internal gaps."""
    try:
        with wave.open(str(path), "rb") as stream:
            rate, channels, width, count = (stream.getframerate(), stream.getnchannels(),
                                           stream.getsampwidth(), stream.getnframes())
            if (rate, channels, width) != (48000, 2, 2) or count <= 0:
                raise NarrationError("NARRATION_PCM_FORMAT_INVALID")
            first, last, cursor = None, None, 0
            threshold = math.ceil(32768 * 10 ** (THRESHOLD_DB / 20))
            while cursor < count:
                block = stream.readframes(min(65536, count - cursor))
                if not block or len(block) % (channels * width):
                    raise NarrationError("NARRATION_PCM_DATA_TRUNCATED")
                data = np.frombuffer(block, dtype="<i2").reshape(-1, channels).astype(np.int32)
                active = np.flatnonzero(np.any(np.abs(data) >= threshold, axis=1))
                if len(active):
                    first = cursor + int(active[0]) if first is None else first
                    last = cursor + int(active[-1])
                cursor += len(data)
            if cursor != count:
                raise NarrationError("NARRATION_PCM_DATA_TRUNCATED")
    except (wave.Error, EOFError) as exc:
        raise NarrationError("NARRATION_PCM_FORMAT_INVALID") from exc
    if first is None:
        raise NarrationError("NARRATION_ALL_SILENT")
    return {"sample_count": count, "sample_rate": rate, "first_active": first,
            "last_active": last, "leading_s": first / rate,
            "trailing_s": (count - last - 1) / rate,
            "duration_us": round(count * 1_000_000 / rate)}


def prepared(path, policy):
    """Validate an already frozen final asset without applying gain/trim twice."""
    path = Path(path)
    receipt = Path(str(path) + ".json")
    if not path.name.startswith(VERSION + "-"):
        return None
    try:
        record = json.loads(receipt.read_text(encoding="utf-8"))
        if record["settings"] != policy or record["output"]["sha256"] != content_identity(path)["sha256"]:
            raise NarrationError("NARRATION_PREPARED_ASSET_CHANGED")
        facts = pcm_edges(path)
        if facts["duration_us"] != record["duration_us"]:
            raise NarrationError("NARRATION_PREPARED_DURATION_CHANGED")
        return record
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise NarrationError("NARRATION_PREPARED_ASSET_INVALID:" + str(exc)) from exc


def frozen(path, *, loudness=None):
    """Reuse a finalized frozen asset with its saved edge policy, without editing it."""
    path = Path(path)
    if not path.name.startswith(VERSION + "-"):
        return None
    try:
        record = json.loads(Path(str(path) + ".json").read_text(encoding="utf-8"))
        edge_trim = record["settings"]["edge_trim"]
        if type(edge_trim) is not bool:
            raise NarrationError("FROZEN_NARRATION_POLICY_INVALID")
        return prepared(path, settings(loudness, edge_trim=edge_trim))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise NarrationError("FROZEN_NARRATION_INVALID:" + str(exc)) from exc


def prepare(path, directory, *, loudness=None, edge_trim=True, ffmpeg="ffmpeg", ffprobe="ffprobe"):
    """One bounded edge-only repair, after tempo and loudness, in lossless PCM.

    Cache identity includes source bytes and exact final-processing parameters.
    Raw synthesis and tempo are owned by voice_tts; this function never changes tempo.
    """
    source = Path(path).resolve(strict=True)
    policy = settings(loudness, edge_trim=edge_trim)
    existing = prepared(source, policy)
    if existing:
        return existing
    identity = content_identity(source)
    key = hashlib.sha256(json.dumps([identity["sha256"], policy], sort_keys=True).encode()).hexdigest()[:24]
    directory = Path(directory)
    target = directory / (VERSION + "-" + key + ".wav")
    with file_lock(target, timeout=120):
        if target.is_file():
            try:
                cached = prepared(target, policy)
                if cached:
                    return cached
            except NarrationError:
                pass  # Rebuild a corrupt derived cache from the unchanged source.
        directory.mkdir(parents=True, exist_ok=True)
        working = target.with_name("." + target.stem + "-" + uuid.uuid4().hex + ".wav")
        temporary = target.with_name("." + target.stem + "-" + uuid.uuid4().hex + ".wav")
        try:
            if policy["enabled"]:
                normal = audio_loudness.normalized_path(source, directory / "normalized",
                                                       target_lufs=policy["target_lufs"])
                detail = audio_loudness.normalize_narration(source, normal, ffmpeg=str(ffmpeg),
                    ffprobe=str(ffprobe), target_lufs=policy["target_lufs"])
                if detail.get("skipped"):
                    raise NarrationError("NARRATION_NORMALIZATION_FAILED:" + detail["skipped"],
                        status="NARRATION_AUDIO_UNVERIFIED" if detail["skipped"] == "below_floor" else "NARRATION_GAP_UNVERIFIED")
            else:
                normal = working
                proc = subprocess.run([str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                    "-i", str(source), "-vn", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(normal)],
                    capture_output=True, timeout=60)
                if proc.returncode:
                    raise NarrationError("NARRATION_PCM_CONVERSION_FAILED", status="NARRATION_GAP_UNVERIFIED")
            facts = pcm_edges(normal)
            count, rate = facts["sample_count"], facts["sample_rate"]
            keep = round(KEEP_MS * rate / 1000)
            start = max(0, facts["first_active"] - keep) if edge_trim else 0
            end = min(count, facts["last_active"] + 1 + keep) if edge_trim else count
            if (max(start, count - end) > MAX_EDGE_TRIM_S * rate
                    or end - start < MIN_RETAINED_RATIO * count):
                raise NarrationError("NARRATION_EDGE_REPAIR_UNSAFE")
            with wave.open(str(normal), "rb") as reader, wave.open(str(temporary), "wb") as writer:
                writer.setparams(reader.getparams())
                reader.setpos(start)
                remaining = end - start
                while remaining:
                    block = reader.readframes(min(remaining, 65536))
                    if not block:
                        raise NarrationError("NARRATION_PCM_DATA_TRUNCATED")
                    writer.writeframesraw(block)
                    remaining -= len(block) // 4
            final = pcm_edges(temporary)
            if final["sample_count"] != end - start:
                raise NarrationError("NARRATION_EDGE_REPAIR_LENGTH_INVALID")
            if content_identity(source)["sha256"] != identity["sha256"]:
                raise NarrationError("NARRATION_SOURCE_CHANGED_DURING_PREPARATION")
            temporary.replace(target)
            record = {"path": str(target.resolve()), "source": identity, "settings": policy,
                      "output": content_identity(target), "duration_us": final["duration_us"],
                      "trim_start_samples": start, "trim_end_samples": count - end,
                      "before": facts, "after": final, "repair_attempts": int(bool(start or end != count))}
            atomic_write_text(str(target) + ".json", json.dumps(record, ensure_ascii=False, indent=2))
            return record
        finally:
            working.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)


def preflight(timing, directory, *, fps, max_frames, ffmpeg):
    """Check real finalized sentence files before any paid shot selection."""
    from ..policy.production_parameters import silence_edges, gap_errors
    rows, cursor = [], 0.
    try:
        for row in timing.get("rows", []):
            if row.get("timing_certainty") != "audio" or row.get("probe_error"):
                raise NarrationError("NARRATION_TIMING_UNVERIFIED", status="NARRATION_GAP_UNVERIFIED")
            duration = row["audio_duration_us"] / 1_000_000
            edges = silence_edges(row["tts_path"], start_s=0, duration_s=duration, ffmpeg=ffmpeg)
            if edges.get("all_silent"):
                raise NarrationError("NARRATION_ALL_SILENT")
            rows.append({**edges, "sentence": row["index"], "path": row["tts_path"],
                         "start_s": cursor, "end_s": cursor + duration})
            cursor += duration
        if not rows:
            raise NarrationError("NARRATION_TIMING_UNVERIFIED")
        issues = gap_errors(rows, fps=fps, max_frames=max_frames)
        status = "NARRATION_GAP_EXCEEDED" if issues else "PASS"
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        status = exc.status if isinstance(exc, NarrationError) else "NARRATION_GAP_UNVERIFIED"
        issues = [{"reason": str(exc)}]
    report = {"status": status, "rows": rows, "issues": issues, "max_gap_frames": max_frames,
              "fps": fps, "measurement_version": "contiguous-eof-v2", "stage": "before_selection"}
    atomic_write_text(Path(directory) / "narration_preflight_report.json",
                      json.dumps(report, ensure_ascii=False, indent=2))
    if issues:
        raise NarrationError(status, status=status, report=report)
    return report
