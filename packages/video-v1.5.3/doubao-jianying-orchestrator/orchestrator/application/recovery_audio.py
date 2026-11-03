"""Bind the retained sentence audio before a recovery draft is written."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from ..selection import timing_contract


def bind(manifest: dict) -> dict:
    timing_path = manifest.get("narration_timing_file")
    if not timing_path:
        return manifest
    timing = json.loads(Path(timing_path).read_text(encoding="utf-8-sig"))
    rows = timing.get("rows", [])
    result = copy.deepcopy(manifest)
    segments = result.get("segments", [])
    if not rows or len(rows) != len(segments):
        raise ValueError("RECOVERY_SENTENCE_TIMING_REQUIRED")
    for index, (segment, row) in enumerate(zip(segments, rows), 1):
        if (row.get("key") != timing_contract.sentence_key(segment, index)
                or row.get("text") != timing_contract.sentence_text(segment)
                or int(row.get("audio_duration_us") or 0) <= 0 or row.get("timing_certainty") != "audio"):
            raise ValueError("RECOVERY_SENTENCE_TIMING_CHANGED")
        path = Path(str(row.get("tts_path") or ""))
        if not path.is_file() or not path.stat().st_size:
            raise ValueError("RECOVERY_SENTENCE_AUDIO_REQUIRED")
        segment.update(audio=str(path.resolve()), audio_mode="file",
                       audio_duration_us=row["audio_duration_us"], timing_certainty="audio")
    return result


def bind_and_record(manifest: dict, matched: dict, report_dir: Path) -> dict:
    """Caller has validated the complete match and its original input binding."""
    if not manifest.get("narration_timing_file"):
        return manifest
    bound = bind(manifest)
    if (bound["segments"] != matched.get("segments")
            or bound["segments"] != (matched.get("manifest") or {}).get("segments")):
        matched["segments"] = copy.deepcopy(bound["segments"])
        matched["manifest"] = copy.deepcopy(bound)
        (report_dir / "shot_match_report.json").write_text(
            json.dumps(matched, ensure_ascii=False, indent=2), encoding="utf-8")
        (report_dir / "matched_manifest.json").write_text(
            json.dumps(bound, ensure_ascii=False, indent=2), encoding="utf-8")
    return bound
