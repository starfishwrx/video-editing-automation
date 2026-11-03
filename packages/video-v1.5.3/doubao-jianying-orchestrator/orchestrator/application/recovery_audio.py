"""Bind the retained sentence audio before a recovery draft is written."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from ..selection import timing_contract


def bind(manifest: dict, *, input_manifest: dict | None = None) -> dict:
    timing_path = manifest.get("narration_timing_file")
    if not timing_path:
        return manifest
    timing = json.loads(Path(timing_path).read_text(encoding="utf-8-sig"))
    rows = timing.get("rows", [])
    result = copy.deepcopy(manifest)
    segments = result.get("segments", [])
    if not rows or len(rows) != len(segments):
        raise ValueError("RECOVERY_SENTENCE_TIMING_REQUIRED")
    retained_tts = set(result.get("retained_tts_audio", []))
    # Old recovery reports already changed TTS to file mode. Recover its origin
    # only from the original input that the caller has matched to this report.
    original_sentences = timing_contract.split_sentences(input_manifest) if input_manifest is not None else []
    original_bound = (len(original_sentences) == len(rows) and all(
        timing_contract.sentence_key(sentence, index) == row.get("key")
        and timing_contract.sentence_text(sentence) == row.get("text")
        for index, (sentence, row) in enumerate(zip(original_sentences, rows), 1)))
    for index, (segment, row) in enumerate(zip(segments, rows), 1):
        if (row.get("key") != timing_contract.sentence_key(segment, index)
                or row.get("text") != timing_contract.sentence_text(segment)
                or int(row.get("audio_duration_us") or 0) <= 0 or row.get("timing_certainty") != "audio"):
            raise ValueError("RECOVERY_SENTENCE_TIMING_CHANGED")
        path = Path(str(row.get("tts_path") or ""))
        if not path.is_file() or not path.stat().st_size:
            raise ValueError("RECOVERY_SENTENCE_AUDIO_REQUIRED")
        original = original_sentences[index - 1] if original_bound else None
        original = original if isinstance(original, dict) else {"text": original} if original else None
        original_tts = (original is not None and not original.get("audio")
                        and original.get("audio_mode", "tts") == "tts")
        if (segment.get("audio_mode", "tts") == "tts" and not segment.get("audio")) or original_tts:
            retained_tts.add(str(path.resolve()))
        segment.update(audio=str(path.resolve()), audio_mode="file",
                       audio_duration_us=row["audio_duration_us"], timing_certainty="audio")
    if retained_tts:
        result["retained_tts_audio"] = sorted(retained_tts)
    return result


def bind_and_record(manifest: dict, matched: dict, report_dir: Path) -> dict:
    """Caller has validated the complete match and its original input binding."""
    if not manifest.get("narration_timing_file"):
        return manifest
    bound = bind(manifest, input_manifest=matched.get("input_manifest"))
    if (bound["segments"] != matched.get("segments")
            or bound != matched.get("manifest")):
        matched["segments"] = copy.deepcopy(bound["segments"])
        matched["manifest"] = copy.deepcopy(bound)
        (report_dir / "shot_match_report.json").write_text(
            json.dumps(matched, ensure_ascii=False, indent=2), encoding="utf-8")
        (report_dir / "matched_manifest.json").write_text(
            json.dumps(bound, ensure_ascii=False, indent=2), encoding="utf-8")
    return bound
