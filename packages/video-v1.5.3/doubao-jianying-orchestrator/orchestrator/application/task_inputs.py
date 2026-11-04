"""Meeting task input normalization shared by queue adapters and manifests.

Suggested schema fields: voice_original (verbatim operator entry), voice,
voice_tempo (numeric multiplier when explicitly supplied),
modified_reference_links (list), modified_reference_attachments (list).
Reference fields remain unknown when empty; their presence is not an evaluation
result or approval.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def normalize_meeting_task(task: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with single-output meeting defaults and preserved inputs.

    Explicit legacy profile retains a historical count value. All other profiles
    (including omitted profile) produce one normal worker task per script.
    """
    out = dict(task)
    profile = str(out.get("meeting_profile") or out.get("profile") or "meeting").strip().lower()
    if profile == "legacy":
        out["meeting_profile"] = "legacy"
        try:
            out["count"] = max(1, int(out.get("count") or 1))
        except (TypeError, ValueError):
            out["count"] = 1
    else:
        out["count"] = 1
        out["meeting_profile"] = "meeting"
    raw_voice = out.get("voice_original")
    if raw_voice is None or not str(raw_voice).strip():
        raw_voice = out.get("voice", "")
    voice = out.get("voice")
    if voice is None or not str(voice).strip():
        voice = raw_voice
    out["voice"] = voice
    tempo_status = out.get("voice_tempo_status")
    if not tempo_status:
        tempo_status = "explicit" if out.get("voice_tempo") is not None else "unknown"
    fields = {
        "voice_original": raw_voice,
        "voice": voice,
        "voice_tempo": out.get("voice_tempo"),
        "voice_tempo_original": out.get("voice_tempo_original"),
        "voice_tempo_status": tempo_status,
        "modified_reference_links": _as_list(out.get("modified_reference_links")),
        "modified_reference_attachments": _as_list(out.get("modified_reference_attachments")),
    }
    # Do not turn absence into a false negative or infer acceptance from a link.
    fields["reference_status"] = "unknown"
    out["meeting_inputs"] = fields
    return out


def manifest_meeting_fields(task: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_meeting_task(task)
    fields = dict(normalized["meeting_inputs"])
    return {"meeting_profile": normalized.get("meeting_profile", "meeting"),
            "meeting_inputs": fields}


def done_meeting_fields(task: dict[str, Any]) -> dict[str, Any]:
    """Task-to-output/eval association, explicitly not a PASS verdict."""
    return {"meeting_inputs": normalize_meeting_task(task)["meeting_inputs"],
            "meeting_reference_evaluation": "unknown"}


def fixed_narration_segments(task: dict[str, Any]) -> list[dict[str, str]] | None:
    """Accept explicit sentence audio without importing old selections or timing."""
    nested = task.get("manifest")
    if nested is None:
        return None
    if not isinstance(nested, dict):
        raise ValueError("INVALID_TASK_MANIFEST")
    if "segments" not in nested:
        return None
    rows = nested["segments"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("FIXED_NARRATION_SEGMENTS_REQUIRED")
    allowed = {"text", "audio", "audio_mode"}
    if any(not isinstance(row, dict) or set(row) - allowed for row in rows):
        raise ValueError("FIXED_NARRATION_FIELDS_INVALID")
    if any(str(row.get("audio_mode") or "file").strip().lower() != "file" for row in rows):
        raise ValueError("FIXED_NARRATION_FILE_MODE_REQUIRED")
    from ..selection import sentence_planner
    script = str(task.get("script") or "")
    texts = [row.get("text") for row in rows]
    try:
        validated = sentence_planner.validate(script, {"segments": texts})
    except (TypeError, ValueError) as exc:
        raise ValueError("FIXED_NARRATION_SCRIPT_MISMATCH: " + str(exc)) from exc
    result = []
    for index, (row, text) in enumerate(zip(rows, validated), 1):
        raw = str(row.get("audio") or "").strip()
        path = Path(raw).expanduser()
        if not raw or not path.is_absolute() or not path.is_file():
            raise ValueError("FIXED_NARRATION_AUDIO_MISSING:S%d" % index)
        result.append({"text": text["text"], "audio": str(path.resolve()), "audio_mode": "file"})
    return result


def validate_explicit_audio(segments: Any, base_dir: Path, *, ffprobe: str) -> Any:
    """Validate and resolve supplied audio before any model request."""
    from ..audio.narration_probe import probe_audio
    if not isinstance(segments, list):
        return segments
    normalized = []
    for index, row in enumerate(segments, 1):
        if not isinstance(row, dict) or not str(row.get("audio") or "").strip():
            normalized.append(row)
            continue
        path = Path(str(row["audio"])).expanduser()
        path = (path if path.is_absolute() else base_dir / path).resolve()
        try:
            if not path.is_file() or int(probe_audio(str(path), ffprobe=ffprobe)["duration_us"]) <= 0:
                raise ValueError("audio missing or empty")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("FIXED_NARRATION_AUDIO_INVALID:S%d" % index) from exc
        normalized.append({**row, "audio": str(path)})
    return normalized
