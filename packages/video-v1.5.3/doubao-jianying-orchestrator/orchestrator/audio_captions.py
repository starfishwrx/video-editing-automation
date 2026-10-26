"""Original script captions timed by the final TTS narration, without ASR.

Each TTS sentence already has known text and a measured audio duration. A
sentence spanning several visual cuts receives one continuous subtitle cue.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import subprocess
import wave
from typing import Any, Callable

CAPTION_VERSION = "tts-sentence-timing-v1"


def _get(value: Any, key: str, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _audio_duration_us(path: Path) -> int:
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as audio:
                return round(audio.getnframes() / audio.getframerate() * 1_000_000)
        except (wave.Error, EOFError):
            pass
    from .platform_env import find_ffmpeg
    ffprobe = find_ffmpeg()[1]
    if not ffprobe:
        raise ValueError("CAPTION_AUDIO_PROBE_MISSING")
    result = subprocess.run([str(ffprobe), "-v", "error", "-show_entries", "format=duration",
                             "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("CAPTION_AUDIO_UNREADABLE: " + (result.stderr or "")[-300:])
    return round(float(json.loads(result.stdout)["format"]["duration"]) * 1_000_000)


def _resolve_audio(clip: Any, overrides: dict) -> Path:
    source = str(_get(clip, "tts_path") or _get(clip, "audio") or "")
    keys = [source, str(Path(source)), str(Path(source).resolve())]
    index = _get(clip, "audio_clip_index", _get(clip, "parent_index", -1))
    keys.extend((index, str(index)))
    for key in keys:
        if key in overrides:
            value = overrides[key]
            source = str(value.get("path") or value.get("audio") or "") if isinstance(value, dict) else str(value)
            break
    path = Path(source)
    if not source or not path.is_file():
        raise ValueError("CAPTION_NARRATION_MISSING")
    return path.resolve()


def build_captions(manifest: dict, clips: list, report_dir: Path, *,
                   audio_overrides: dict | None = None, probe: Callable | None = None) -> dict:
    """Use real post-tempo/trim audio lengths, never character-weighted timings."""
    report_dir = Path(report_dir)
    cues, sources = [], []
    try:
        groups: dict[tuple, list] = {}
        for index, clip in enumerate(clips):
            text = str(_get(clip, "text", "") or "").strip()
            if not text:
                continue
            if _get(clip, "audio_mode", "tts") not in {"tts", "file"}:
                raise ValueError("CAPTION_NARRATION_REQUIRED")
            parent = int(_get(clip, "parent_index", -1))
            key = ("parent", parent) if parent >= 0 else ("clip", index)
            groups.setdefault(key, []).append(clip)
        durations = {}
        for group in groups.values():
            group.sort(key=lambda clip: int(_get(clip, "start_us", 0)))
            text = str(_get(group[0], "text")).strip()
            path = _resolve_audio(group[0], audio_overrides or {})
            if any(str(_get(clip, "text")).strip() != text or
                   _resolve_audio(clip, audio_overrides or {}) != path for clip in group):
                raise ValueError("CAPTION_SENTENCE_AUDIO_CONFLICT")
            if path not in durations:
                measured = (float(probe(str(path))["duration"]) * 1_000_000
                            if probe else _audio_duration_us(path))
                if not math.isfinite(measured) or measured <= 0:
                    raise ValueError("CAPTION_AUDIO_DURATION_INVALID")
                durations[path] = round(measured)
            start = int(_get(group[0], "start_us", 0))
            cursor = start
            for clip in group:
                clip_start = int(_get(clip, "start_us", 0))
                duration = int(_get(clip, "duration_us", 0) or 0)
                if not duration:
                    duration = round(float(_get(clip, "duration", 0)) * 1_000_000)
                if clip_start != cursor or duration <= 0:
                    raise ValueError("CAPTION_SENTENCE_TIMELINE_INVALID")
                if _get(clip, "multi_shot", False) and int(_get(clip, "audio_clip_start_us", 0)) != start:
                    raise ValueError("CAPTION_AUDIO_ORIGIN_CHANGED")
                cursor = clip_start + duration
            end = start + durations[path]
            if start < 0 or end > cursor + 40_000:
                raise ValueError("CAPTION_AUDIO_EXCEEDS_TIMELINE")
            cues.append({"text": text, "start_us": start, "end_us": min(end, cursor)})
            sources.append({"path": str(path), "duration_us": durations[path], "text": text})
        cues.sort(key=lambda cue: cue["start_us"])
        if not cues or any(right["start_us"] < left["end_us"] for left, right in zip(cues, cues[1:])):
            raise ValueError("CAPTION_TIMELINE_EMPTY_OR_OVERLAPPING")
        report = {"ok": True, "cues": cues, "identity": {"source": CAPTION_VERSION, "narration": sources},
                  "issues": [], "status": "timed_from_tts"}
    except (OSError, ValueError, TypeError, KeyError, OverflowError, subprocess.SubprocessError) as exc:
        report = {"ok": False, "cues": [], "identity": {"source": CAPTION_VERSION},
                  "issues": [f"caption_generation_failed: {exc}"], "status": "review_required"}
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "audio_captions.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {key: report[key] for key in ("ok", "cues", "identity", "issues")}
