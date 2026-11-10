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


def prepare_for_selection(manifest: dict, timing: dict, report_dir: Path, *, source_dir: Path | None = None) -> dict:
    """Finalize cached paper TTS before rematching, never after selection."""
    from .. import rulepack
    from ..audio import narration_preparation
    from ..audio.narration_probe import probe_audio
    from ..runtime.platform_env import find_ffmpeg

    policy = rulepack.for_manifest(manifest)
    if policy.get("category") != "zhipin":
        return timing
    ffmpeg, ffprobe = find_ffmpeg()[:2]
    result = copy.deepcopy(timing)
    fixed = dict(manifest.get("_paper_fixed_narrations") or {})
    sentences = timing_contract.split_sentences(manifest)
    rows = result.get("rows", [])
    if len(sentences) != len(rows):
        raise ValueError("RECOVERY_SENTENCE_TIMING_CHANGED")
    retained = {str(Path(p).resolve()) for p in manifest.get("retained_tts_audio", [])}
    for sentence, row in zip(sentences, rows):
        segment = sentence if isinstance(sentence, dict) else {}
        source_path = Path(row["tts_path"] )
        if not source_path.is_absolute() and source_dir is not None:
            source_path = source_dir / source_path
        source = str(source_path.resolve())
        if (segment.get("audio") or segment.get("audio_mode", "tts") == "file") and source not in retained:
            declared = Path(str(segment.get("audio") or source))
            if not declared.is_absolute() and source_dir is not None:
                declared = source_dir / declared
            if declared.resolve() != Path(source):
                raise ValueError("RECOVERY_SENTENCE_AUDIO_CHANGED")
            facts = probe_audio(source, ffprobe=str(ffprobe))
            row.update(tts_path=source, audio_duration_us=round(float(facts["duration"]) * 1_000_000),
                       timing_certainty="audio", estimated=False, probe_error="")
            continue
        record = narration_preparation.frozen(source, loudness=manifest.get("narration_loudness"))
        if record is None:
            record = narration_preparation.prepare(source, report_dir / "recovery-narration",
                loudness=manifest.get("narration_loudness"),
                edge_trim=bool(manifest.get("voice_edge_trim", True)),
                ffmpeg=str(ffmpeg), ffprobe=str(ffprobe))
        fixed[source] = record
        row.update(tts_path=record["path"], audio_duration_us=record["duration_us"],
                   timing_certainty="audio", estimated=False, probe_error="")
    narration_preparation.preflight(result, report_dir, fps=float(manifest.get("fps") or 30),
        max_frames=int(policy["voice"]["acceptance_max_gap_frames"]), ffmpeg=str(ffmpeg))
    target = report_dir / "recovery_narration_timing.json"
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest.update(narration_timing_file=str(target.resolve()), _paper_fixed_narrations=fixed)
    return result
