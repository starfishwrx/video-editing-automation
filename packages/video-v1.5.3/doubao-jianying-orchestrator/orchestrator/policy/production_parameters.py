"""Physical subtitle coordinates and measured narration gaps for paper tasks."""
from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path


def subtitle_transform(manifest: dict, width: int, height: int) -> dict:
    position = manifest.get("subtitle_position_px")
    if position is None:
        return {"x": 0.0, "y": -.55}
    if (not isinstance(position, dict) or set(position) != {"x", "y"}
            or any(type(position[k]) not in (int, float) or not math.isfinite(position[k]) for k in ("x", "y"))
            or width <= 0 or height <= 0):
        raise ValueError("SUBTITLE_POSITION_INVALID")
    result = {"x": position["x"] / (width / 2), "y": position["y"] / (height / 2)}
    if any(abs(v) >= 1 for v in result.values()):
        raise ValueError("SUBTITLE_POSITION_OUT_OF_CANVAS")
    return result


def silence_edges(path: str, *, start_s: float, duration_s: float, ffmpeg: str) -> dict:
    """Measure the audio slice actually scheduled, without editing its source."""
    if not Path(path).is_file() or duration_s <= 0:
        raise ValueError("NARRATION_GAP_AUDIO_MISSING")
    result = subprocess.run([str(ffmpeg), "-hide_banner", "-nostdin", "-nostats", "-progress", "pipe:1", "-ss", str(start_s),
        "-t", str(duration_s), "-i", str(path), "-vn", "-af", "silencedetect=noise=-40dB:d=0.005",
        "-f", "null", "-"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode:
        raise ValueError("NARRATION_GAP_PROBE_FAILED")
    progress = re.findall(r"out_time_us=(-?\d+)", result.stdout)
    if not progress:
        raise ValueError("NARRATION_GAP_DURATION_UNVERIFIED")
    decoded = min(duration_s, max(0.0, max(map(int, progress)) / 1e6))
    missing = max(0.0, duration_s - decoded)
    events = [(kind, float(value)) for kind, value in re.findall(
        r"silence_(start|end):\s*([\d.]+)", result.stderr)]
    leading = 0.0
    trailing = 0.0
    start = None
    for kind, value in events:
        if kind == "start":
            start = value
        elif start is not None:
            if start <= .005:
                leading = value
            if value >= decoded - .04:
                trailing = max(0.0, decoded - start)
            start = None
    if start is not None:
        trailing = max(0.0, decoded - start)
        if start <= .005:
            leading = duration_s
    return {"leading_s": min(leading, duration_s), "trailing_s": trailing + missing,
        "decoded_duration_s": decoded, "missing_audio_s": missing}


def gap_errors(rows: list[dict], *, fps: float, max_frames: int = 5) -> list[dict]:
    errors = []
    for left, right in zip(rows, rows[1:]):
        if left["sentence"] == right["sentence"]:
            continue
        gap = max(0.0, right["start_s"] - left["end_s"]) + left["trailing_s"] + right["leading_s"]
        if gap * fps > max_frames + .01:
            errors.append({"left": left["sentence"], "right": right["sentence"],
                "gap_frames": round(gap * fps, 3), "max_gap_frames": max_frames})
    return errors
