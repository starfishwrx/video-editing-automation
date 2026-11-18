"""Experiment: submit one proposed ordinary display window to the existing editor."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from . import ai_editor, camera_variety, shot_notes


def apply(manifest, analysis, plan, timing, *, index: int, shot_id: str,
          start_us: int, directory: Path):
    """Return an assembled match only when actual pixels support this exact cut.

    The caller supplies the original plan with every other sentence frozen.
    The program computes the end from the existing audio and playback contract.
    """
    if type(index) is not int or not 1 <= index <= len(plan["sentences"]):
        raise ValueError("DIRECTED_DISPLAY_SENTENCE_INVALID")
    if type(start_us) is not int or start_us < 0:
        raise ValueError("DIRECTED_DISPLAY_START_INVALID")
    if set(plan.get("_frozen_segments", {})) != set(range(1, len(plan["sentences"]) + 1)) - {index}:
        raise ValueError("DIRECTED_DISPLAY_FROZEN_SCOPE_REQUIRED")
    plan = deepcopy(plan)
    before = ai_editor._assemble(manifest, analysis, plan, narration_timing=timing, verify_sources=True)
    group = next(g for g in camera_variety.groups(before["segments"], plan, manifest) if index in g["indices"])
    row = next(row for row in plan["rows"] if row["index"] == index)
    original = ai_editor.timing_contract.split_sentences(manifest)[index - 1]
    if (group["indices"] != [index] or not group["editable"] or manifest.get("shot_notes_general")
            or ai_editor._row_has_presentation(plan, row) or original.get("visual_requirements")
            or shot_notes.note_for(manifest, index, original)):
        raise ValueError("DIRECTED_DISPLAY_REQUIRES_INDEPENDENT_DISPLAY")
    shot = next((s for s in analysis["shots"] if s.get("shot_id") == shot_id), None)
    if not shot or shot.get("status") != "ready_for_matching" or shot.get("text_pass") in {"pending", "failed"}:
        raise ValueError("DIRECTED_DISPLAY_CANDIDATE_UNAVAILABLE")
    allowed = {Path(path).resolve() for path in manifest.get("material_sources", [])}
    if Path(shot["video"]).resolve() not in allowed:
        raise ValueError("DIRECTED_DISPLAY_SOURCE_OUTSIDE_TASK")
    if shot_id in ai_editor.qc_rejected_shots(manifest).get(index, set()):
        raise ValueError("DIRECTED_DISPLAY_CANDIDATE_REJECTED")
    sentence = plan["sentences"][index - 1]
    duration = before["segments"][index - 1]["audio_duration_us"]
    pad = ai_editor.timing_contract.us_of(manifest.get("tail_pad_s"), 0)
    end_us = start_us + duration + pad
    if not round(shot["source_start"] * 1e6) <= start_us < end_us <= round(shot["source_end"] * 1e6):
        raise ValueError("DIRECTED_DISPLAY_WINDOW_OUTSIDE_SOURCE")
    directory.mkdir(parents=True, exist_ok=True)
    if ai_editor._matched_parent_review(plan, sentence, shot_id) is None:
        review = ai_editor._review(manifest, sentence, shot, directory, dense=True)
        if review.get("verdict") != "matched" or review.get("same_product") is not True:
            raise ValueError("DIRECTED_DISPLAY_SOURCE_NOT_CONFIRMED")
        plan["display_reviews"].append(review)
    group.update(texts=[sentence["text"]], durations={index: duration}, pad=pad)
    result = camera_variety.replacement(manifest, analysis, plan, timing, group,
        {"shot_id": shot_id, "start": start_us, "end": end_us}, shot, directory, ai_editor)
    if result is None:
        raise ValueError("DIRECTED_DISPLAY_EXACT_CUT_NOT_CONFIRMED")
    plan["rows"], _, _ = result
    return ai_editor._assemble(manifest, analysis, plan, narration_timing=timing, verify_sources=True)
