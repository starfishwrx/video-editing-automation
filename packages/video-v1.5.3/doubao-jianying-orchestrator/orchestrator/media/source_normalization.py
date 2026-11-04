"""Upright high-quality working copies with an unchanged relative source clock.

v4 encoded the whole source losslessly (QP 0), which inflated typical 15-20 Mbps
phone footage to ~165 Mbps and made draft write, NAS ship and archive hashing
IO-bound. v5 encodes at visually transparent quality capped near the source
bitrate. v4 receipts in already-built drafts stay readable and verifiable.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
import subprocess
import uuid

from .probe import probe_media
from .video_encode import run_h264_encode
from ..runtime.file_safety import atomic_write_text, file_lock

VERSION = "upright-source-v5"
LEGACY_VERSIONS = ("upright-source-v4",)
ACCEPTED_VERSIONS = (VERSION, *LEGACY_VERSIONS)
QUALITY_MODE = "high_quality"
# Average target and hard peak relative to the source video bitrate.
TARGET_RATIO, PEAK_RATIO = 1.2, 1.5
MIN_SOURCE_BITRATE = 2_000_000
RECEIPT_FILE = "prepared_sources.json"


def identity(path) -> dict:
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    return {"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def file_digest(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _video(facts):
    return next(s for s in facts["streams"] if s.get("codec_type") == "video")


def rotation(facts) -> int:
    stream = _video(facts)
    value = float(stream.get("tags", {}).get("rotate", 0))
    for side in stream.get("side_data_list", []):
        value = float(side.get("rotation", value))
    if not math.isfinite(value) or value % 90:
        raise ValueError("SOURCE_ROTATION_UNSUPPORTED")
    return round(value) % 360


def source_video_bitrate(facts) -> int:
    """Video bitrate of the camera original (bps), falling back to file size."""
    try:
        value = int(_video(facts).get("bit_rate") or 0)
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        value = int(facts["size"] * 8 * 1_000_000 / max(1, facts["duration_us"]))
    return max(MIN_SOURCE_BITRATE, value)


def _clock(path, ffprobe) -> list[int]:
    result = subprocess.run([str(ffprobe), "-v", "error", "-select_streams", "v:0",
        "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, check=True)
    frames = json.loads(result.stdout)["frames"]
    points = [round(Fraction(str(f["best_effort_timestamp_time"])) * 1_000_000) for f in frames]
    if not points or any(a >= b for a, b in zip(points, points[1:])):
        raise ValueError("NORMALIZED_SOURCE_FRAME_CLOCK_INVALID")
    return [p - points[0] for p in points]


def normalize_video(source, directory, *, ffmpeg="ffmpeg", ffprobe="ffprobe") -> dict:
    source = Path(source).resolve(strict=True)
    before = identity(source)
    facts = probe_media(source, ffprobe=ffprobe)
    angle = rotation(facts)
    if not angle:
        return {"changed": False, "source": before, "prepared": before}
    stream = _video(facts)
    pixel_format = stream.get("pix_fmt")
    # 90-degree rotation must preserve the chroma sampling axes too. Do not
    # silently convert 4:2:2, RGB, or unsupported bit depths to 8-bit 4:2:0.
    if pixel_format not in {"yuv420p", "yuvj420p", "yuv444p", "yuv420p10le", "yuv444p10le"}:
        raise ValueError(f"SOURCE_ROTATION_PIXEL_FORMAT_UNSUPPORTED:{pixel_format}")
    if pixel_format == "yuvj420p" and stream.get("color_range") != "pc":
        raise ValueError("SOURCE_ROTATION_FULL_RANGE_UNCERTAIN")
    colors = {key: stream[key] for key in
              ("color_range", "color_space", "color_transfer", "color_primaries") if key in stream}
    if any(any(term in side.get("side_data_type", "").lower() for term in
               ("dovi", "mastering", "content light", "hdr")) for side in stream.get("side_data_list", [])):
        raise ValueError("SOURCE_ROTATION_HDR_METADATA_UNSUPPORTED")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps([VERSION, before, angle], sort_keys=True).encode()).hexdigest()[:24]
    target = directory / f"upright-{key}.mp4"
    receipt = target.with_suffix(".json")
    with file_lock(receipt, timeout=1200):
        if receipt.is_file() and target.is_file():
            try:
                record = json.loads(receipt.read_text(encoding="utf-8"))
                if (record["schema"] == VERSION and record["source"] == before
                        and record["prepared"] == identity(target)
                        and record["prepared_sha256"] == file_digest(target)):
                    return {**record, "reused": True}
            except (OSError, ValueError, KeyError, TypeError):
                pass
        temporary = directory / f".{target.stem}-{uuid.uuid4().hex}.mp4"
        try:
            # FFmpeg applies the input display matrix to pixels. Keep frame
            # timestamps and native audio; do not resize, trim or resample video.
            source_rate = source_video_bitrate(facts)
            target_rate, peak_rate = round(source_rate * TARGET_RATIO), round(source_rate * PEAK_RATIO)
            # yuvj420p is the full-range 8-bit 4:2:0 representation. H.264
            # encoders accept yuv420p plus an explicit full-range signal.
            encoder_format = "yuv420p" if pixel_format == "yuvj420p" else pixel_format
            output_color = ["-color_range", "pc"] if pixel_format == "yuvj420p" else []
            encoder, _ = run_h264_encode(str(ffmpeg), ["-y", "-hide_banner", "-loglevel", "error",
                "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-fps_mode", "passthrough",
                "-enc_time_base:v", "demux"],
                temporary, trailing=["-c:a", "copy", "-metadata:s:v:0", "rotate=0", *output_color],
                quality_bitrate=target_rate, quality_maxrate=peak_rate,
                pixel_format=encoder_format, timeout=1200)
            actual = probe_media(temporary, ffprobe=ffprobe)
            width, height = facts["width"], facts["height"]
            if angle in {90, 270}:
                width, height = height, width
            if (rotation(actual) or (actual["width"], actual["height"]) != (width, height)
                    or actual["audio"] != facts["audio"]):
                raise ValueError("NORMALIZED_SOURCE_DISPLAY_OR_AUDIO_CHANGED")
            if (_video(actual).get("pix_fmt") != pixel_format
                    or any(_video(actual).get(key) != value for key, value in colors.items())):
                raise ValueError("NORMALIZED_SOURCE_PIXEL_FORMAT_OR_COLOR_CHANGED")
            original_clock, final_clock = _clock(source, ffprobe), _clock(temporary, ffprobe)
            if (len(original_clock) != len(final_clock)
                    or any(abs(a - b) > 1 for a, b in zip(original_clock, final_clock))):
                raise ValueError("NORMALIZED_SOURCE_FRAME_CLOCK_CHANGED")
            if identity(source) != before:
                raise ValueError("NORMALIZED_SOURCE_INPUT_CHANGED")
            temporary.replace(target)
            record = {"schema": VERSION, "changed": True, "source": before,
                "prepared": identity(target), "prepared_sha256": file_digest(target),
                "rotation_degrees": angle, "encoder": encoder, "lossless": False,
                "quality": {"mode": QUALITY_MODE, "source_video_bitrate": source_rate,
                            "target_bitrate": target_rate, "max_bitrate": peak_rate,
                            "prepared_video_bitrate": source_video_bitrate(actual)},
                "pixel_format": pixel_format, "color_metadata": colors,
                "time_mapping": {"offset_us": 0, "rate_num": 1, "rate_den": 1},
                "frame_clock": {"count": len(final_clock), "last_us": final_clock[-1]},
                "source_duration_us": facts["duration_us"], "prepared_duration_us": actual["duration_us"],
                "display_width": width, "display_height": height}
            atomic_write_text(receipt, json.dumps(record, ensure_ascii=False, indent=2))
            return record
        finally:
            temporary.unlink(missing_ok=True)


def _encoding_contract_ok(row) -> bool:
    if row.get("schema") in LEGACY_VERSIONS:
        return row.get("lossless") is True
    return (row.get("schema") == VERSION and row.get("lossless") is False
            and (row.get("quality") or {}).get("mode") == QUALITY_MODE)


def matches_prepared_source(actual_path, expected: dict, records: list[dict]) -> bool:
    """Recognize draft-owned byte copies of an explicitly normalized source."""
    try:
        before = identity(actual_path)
        for row in records:
            if (not _encoding_contract_ok(row) or row.get("source") != expected
                    or row.get("time_mapping") != {"offset_us": 0, "rate_num": 1, "rate_den": 1}
                    or row.get("rotation_degrees") not in {90, 180, 270}
                    or row.get("frame_clock", {}).get("count", 0) < 1
                    or before["size"] != row.get("prepared", {}).get("size")):
                continue
            if (identity(expected["path"]) == expected and file_digest(actual_path) == row["prepared_sha256"]
                    and identity(actual_path) == before and identity(expected["path"]) == expected):
                return True
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return False


def read_prepared_sources(draft) -> list[dict]:
    path = Path(draft) / RECEIPT_FILE
    if not path.is_file():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") not in ACCEPTED_VERSIONS or not isinstance(value.get("sources"), list):
        raise ValueError("PREPARED_SOURCE_RECEIPT_INVALID")
    return value["sources"]
