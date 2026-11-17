"""Validate explicitly supplied sentence audio against its script."""
from pathlib import Path
from typing import Any

from . import sentence_planner


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
