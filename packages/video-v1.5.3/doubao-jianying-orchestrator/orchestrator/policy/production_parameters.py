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
    if (not Path(path).is_file() or not math.isfinite(start_s) or start_s < 0
            or not math.isfinite(duration_s) or duration_s <= 0):
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
    # Some FFmpeg builds render a zero silence_start as "invalid param".
    # Recover only from the explicit end+duration evidence; never assume zero.
    events = [(m.group(1), float(m.group(2)),
               float(m.group(3)) if m.group(3) is not None else None) for m in re.finditer(
        r"silence_(start|end):\s*(-?[\d.]+)(?:\s*\|\s*silence_duration:\s*([\d.]+))?", result.stderr)]
    if "silence_" in result.stderr and not events:
        raise ValueError("NARRATION_GAP_EVENTS_UNVERIFIED")
    leading = 0.0
    trailing = 0.0
    start = None
    for kind, value, interval in events:
        if kind == "start":
            start = value
        else:
            if start is None:
                if interval is None:
                    raise ValueError("NARRATION_GAP_EVENTS_UNVERIFIED")
                start = max(0., value - interval)
            if start <= .000001:
                leading = value
            # Timestamp rounding only: never absorb speech after an internal pause.
            if value >= decoded - .00003:
                trailing = max(0.0, decoded - start)
            start = None
    if start is not None:
        trailing = max(0.0, decoded - start)
        if start <= .000001:
            leading = duration_s
    if decoded <= 0:
        raise ValueError("NARRATION_GAP_DURATION_UNVERIFIED")
    return {"leading_s": min(leading, duration_s), "trailing_s": trailing + missing,
        "all_silent": leading >= decoded - .00003,
        "decoded_duration_s": decoded, "missing_audio_s": missing}



def sentence_edges(rows: list[dict]) -> list[dict]:
    """Aggregate contiguous sentence slices, including silent slices and timeline holes."""
    groups = []
    for row in rows:
        if not groups or groups[-1][0]["sentence"] != row["sentence"]:
            groups.append([])
        groups[-1].append(row)
    result = []
    def silent(row):
        duration = row["end_s"] - row["start_s"]
        return row.get("all_silent", False) or (row["leading_s"] >= duration and row["trailing_s"] >= duration)
    for group in groups:
        start, end = group[0]["start_s"], group[-1]["end_s"]
        voiced = [row for row in group if not silent(row)]
        leading = voiced[0]["start_s"] - start + voiced[0]["leading_s"] if voiced else end - start
        trailing = end - voiced[-1]["end_s"] + voiced[-1]["trailing_s"] if voiced else end - start
        result.append({"sentence": group[0]["sentence"], "start_s": start, "end_s": end,
                       "leading_s": leading, "trailing_s": trailing, "all_silent": not voiced})
    return result


def gap_errors(rows: list[dict], *, fps: float, max_frames: int = 5) -> list[dict]:
    if not math.isfinite(fps) or fps <= 0 or type(max_frames) is not int or max_frames < 0:
        raise ValueError("NARRATION_GAP_FRAME_CLOCK_INVALID")
    errors = []
    sentences = sentence_edges(rows)
    for left, right in zip(sentences, sentences[1:]):
        gap = max(0.0, right["start_s"] - left["end_s"]) + left["trailing_s"] + right["leading_s"]
        if gap * fps > max_frames + .01:
            errors.append({"left": left["sentence"], "right": right["sentence"],
                "gap_frames": round(gap * fps, 3), "max_gap_frames": max_frames,
                "timeline_gap_s": max(0.0, right["start_s"] - left["end_s"]),
                "left_trailing_s": left["trailing_s"], "right_leading_s": right["leading_s"]})
    return errors
