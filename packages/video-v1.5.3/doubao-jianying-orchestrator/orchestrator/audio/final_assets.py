"""Finish cached narration before layout. Never synthesize or change its tempo."""
import hashlib
import json
import math
from pathlib import Path

from .audio_loudness import normalize_narration, TARGET_LUFS
from ..media.probe import probe_media


def prepare_narration(path, directory, *, mode="file", loudness=None,
                      ffmpeg="ffmpeg", ffprobe="ffprobe"):
    source = Path(path).resolve()
    before = probe_media(source, ffprobe=ffprobe)
    if not before["audio"]:
        raise ValueError("NARRATION_AUDIO_STREAM_MISSING")
    spec = loudness if isinstance(loudness, dict) else {}
    unknown = set(spec) - {"enabled", "target_lufs", "gain_db"}
    if unknown:
        raise ValueError("NARRATION_LOUDNESS_OPTION_UNSUPPORTED:" + ",".join(sorted(unknown)))
    target = spec.get("target_lufs", TARGET_LUFS) + spec.get("gain_db", 0.0)
    if type(target) not in (int, float) or not math.isfinite(target):
        raise ValueError("NARRATION_LOUDNESS_INVALID")
    final, processing = source, {"mode": "operator_file_unchanged"}
    if mode == "tts" and spec.get("enabled") is not False:
        # Existing normalizer caches by output name; bind that name to source + settings.
        key = json.dumps([before["path"], before["size"], before["mtime_ns"], target])
        digest = hashlib.sha256(key.encode()).hexdigest()[:20]
        final = Path(directory) / ("narration-" + digest + ".m4a")
        processing = normalize_narration(source, final, ffmpeg=str(ffmpeg), ffprobe=str(ffprobe),
                                         target_lufs=target)
        if processing.get("skipped") or not final.is_file():
            raise ValueError("NARRATION_NORMALIZATION_FAILED:" + str(processing.get("skipped")))
    elif mode not in {"tts", "file"}:
        raise ValueError("NARRATION_MODE_UNSUPPORTED")
    actual = probe_media(final, ffprobe=ffprobe)
    if not actual["audio"]:
        raise ValueError("FINAL_NARRATION_AUDIO_STREAM_MISSING")
    # Measured final length is authoritative. The caller must replan if it changed.
    return {"source": str(source), "path": str(final.resolve()),
            "source_duration_us": before["duration_us"], "duration_us": actual["duration_us"],
            "processing": processing}
