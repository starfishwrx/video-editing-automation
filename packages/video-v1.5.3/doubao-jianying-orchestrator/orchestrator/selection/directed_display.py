"""Experiment: submit one proposed ordinary display window to the existing editor."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from . import ai_editor, camera_variety, experiment_groups, shot_notes


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


def _recorded_audio(manifest, timing, index, sentence):
    from ..audio.narration_probe import probe_audio
    from ..runtime.platform_env import find_ffmpeg

    rows = (timing or {}).get("rows") or []
    row = rows[index - 1] if 0 < index <= len(rows) else {}
    path = Path(row.get("tts_path") or "")
    declared = sentence.get("audio") if isinstance(sentence, dict) else None
    if declared and path.resolve() != Path(declared).resolve():
        prepared = (manifest.get("_paper_fixed_narrations") or {}).get(str(Path(declared).resolve())) or {}
        if not prepared.get("path") or Path(prepared["path"]).resolve() != path.resolve():
            raise ValueError("DIRECTED_WINDOW_NARRATION_CHANGED")
    if (row.get("timing_certainty") != "audio" or row.get("estimated") is True
            or row.get("key") != ai_editor.timing_contract.sentence_key(sentence, index)
            or row.get("text") != ai_editor.timing_contract.sentence_text(sentence)
            or not path.is_file()):
        raise ValueError("DIRECTED_WINDOW_REAL_NARRATION_REQUIRED")
    measured = probe_audio(path, ffprobe=str(find_ffmpeg()[1]))["duration_us"]
    if measured <= 0 or measured != row.get("audio_duration_us"):
        raise ValueError("DIRECTED_WINDOW_NARRATION_CHANGED")
    return measured


def review_window(manifest, analysis, plan, timing, *, index, shot_id, start_us, directory, inventory_dir):
    """Review a proposed ordinary cut on its own pixels and return a verified row.

    A failed first draft may have a usable sentence inventory without accepted
    neighbours. This operation leaves other rows and the frozen audio alone.
    """
    if plan.get("identity") != ai_editor.plan_identity(manifest, analysis, timing):
        raise ValueError("DIRECTED_WINDOW_INPUT_CHANGED")
    sentences = plan["sentences"]
    source_sentences = ai_editor.timing_contract.split_sentences(manifest)
    if (type(index) is not int or not 1 <= index <= len(sentences)
            or [s["text"] for s in sentences] != [ai_editor.timing_contract.sentence_text(s) for s in source_sentences]):
        raise ValueError("DIRECTED_WINDOW_SENTENCE_CHANGED")
    intake = _read_intake(manifest, timing, plan, inventory_dir)
    if _sentence_contract(sentences) != _sentence_contract(intake["sentences"]):
        raise ValueError("DIRECTED_WINDOW_SENTENCE_CONTRACT_CHANGED")
    selected = [r for r in plan["rows"] if r["index"] == index]
    if len(selected) != 1:
        raise ValueError("DIRECTED_WINDOW_SENTENCE_CHANGED")
    if (index in plan.get("_frozen_segments", {}) or selected[0].get("continuity_key")
            or any(index == s["index"] for group in experiment_groups.groups(sentences) for s in group)):
        raise ValueError("DIRECTED_WINDOW_GROUP_OR_FROZEN")
    if shot_notes.note_for(manifest, index, source_sentences[index - 1]) or manifest.get("shot_notes_general"):
        raise ValueError("DIRECTED_WINDOW_USE_NOTED_SELECTION")
    shot = next((s for s in analysis["shots"] if s["shot_id"] == shot_id), None)
    if (not shot or shot.get("status") != "ready_for_matching" or shot.get("excluded_from_pool")
            or shot.get("text_pass") in {"pending", "failed"} or shot_id in ai_editor.qc_rejected_shots(manifest).get(index, set())
            or Path(shot["video"]) not in {Path(path) for path in manifest.get("material_sources", [])}):
        raise ValueError("DIRECTED_WINDOW_SOURCE_UNAVAILABLE")
    duration = _recorded_audio(manifest, timing, index, source_sentences[index - 1])
    if (type(start_us) is not int or not round(shot["source_start"] * 1e6) <= start_us
            or start_us + duration > round(shot["source_end"] * 1e6)):
        raise ValueError("DIRECTED_WINDOW_OUTSIDE_SOURCE")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = ai_editor._review_proposed_window(manifest, plan, directory, sentences[index - 1], shot,
                                            (start_us, start_us + duration), require_current_sentence=True)
    if not key:
        raise ValueError("DIRECTED_WINDOW_NOT_SUPPORTED")
    observation = plan["window_reviews"][key]
    parents = (plan["reviews"].setdefault(str(index), []) if sentences[index - 1]["kind"] == "selling_point"
               else plan["display_reviews"])
    if not any(r.get("shot_id") == shot_id and r.get("verdict") == "matched" for r in parents):
        parents.append(observation)
    return {"index": index, "parts": [{"shot_id": shot_id, "source_start_us": start_us,
            "audio_duration_us": duration, "window_review_key": key,
            "directed_window_us": {"start": start_us, "end": start_us + duration}}]}


def _intake_audio(manifest, timing):
    from ..runtime.file_safety import content_identity

    sentences = ai_editor.timing_contract.split_sentences(manifest)
    if len((timing or {}).get("rows", [])) != len(sentences):
        raise ValueError("DIRECTED_WINDOW_REAL_NARRATION_REQUIRED")
    return [content_identity(row["tts_path"]) for row in timing["rows"]]


def _read_intake(manifest, timing, plan, directory):
    path = Path(directory) / "directed_intake.json"
    if not path.is_file():
        raise ValueError("DIRECTED_INTAKE_REQUIRED")
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved.get("identity") != plan["identity"] or saved.get("audio") != _intake_audio(manifest, timing):
        raise ValueError("DIRECTED_INTAKE_CHANGED")
    return saved


def _sentence_contract(sentences):
    fields = ("index", "text", "kind", "scene_ids", "experiment_role", "hook_neighbor")
    result = []
    for sentence in sentences:
        points = sentence.get("selling_points", [])
        ids = [point.get("id") for point in points]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise ValueError("DIRECTED_WINDOW_SENTENCE_CONTRACT_CHANGED")
        # NOTE: A sole claim can only bind to itself. Multiple claims use their
        # original IDs so a receipt for one cannot be reassigned to another.
        claims = [{k: v for k, v in points[0].items() if k != "id"}] if len(points) == 1 else points
        result.append({**{k: sentence.get(k) for k in fields}, "claims": claims})
    return result


def freeze_intake(manifest, analysis, timing, directory):
    """Obtain the editor's inventory once; bind semantic requirements and final audio."""
    return _freeze_intake(manifest, analysis, timing, directory)


def start_plan(manifest, analysis, timing, directory):
    """Start a directed edit from the same classified intake as the normal editor."""
    saved = freeze_intake(manifest, analysis, timing, directory)
    return {**ai_editor.empty_plan(), "identity": saved["identity"], "sentences": saved["sentences"],
            "rows": [{"index": s["index"], "parts": []} for s in saved["sentences"]]}


def _freeze_intake(manifest, analysis, timing, directory, *, classified=None):
    from ..runtime.file_safety import file_lock
    from ..vision.vision_analyzer import needs_vision
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "directed_intake.json"
    current = ai_editor.plan_identity(manifest, analysis, timing)
    audio = _intake_audio(manifest, timing)
    with file_lock(path):
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved["identity"] != current or saved["audio"] != audio:
                raise ValueError("DIRECTED_INTAKE_CHANGED")
            return saved
        for i, sentence in enumerate(ai_editor.timing_contract.split_sentences(manifest), 1):
            _recorded_audio(manifest, timing, i, sentence)
        shots = [s for s in analysis["shots"] if not s.get("excluded_from_pool") and not needs_vision(s)]
        sentences = classified if classified is not None else ai_editor._inventory(manifest, shots, directory)
        saved = {"identity": current, "audio": audio, "sentences": sentences}
        ai_editor._save(path, saved)
        return saved


def match_reviewed(manifest, analysis, plan, timing, *, inventory_dir):
    """Revalidate every row and group with the normal matcher before writing."""
    if plan.get("identity") != ai_editor.plan_identity(manifest, analysis, timing):
        raise ValueError("DIRECTED_WINDOW_INPUT_CHANGED")
    if plan.get("schema") != ai_editor.contract.PLAN_SCHEMA:
        raise ValueError("AI_EDITOR_PLAN_NOT_CERTIFIED")
    saved = _read_intake(manifest, timing, plan, inventory_dir)
    if _sentence_contract(plan["sentences"]) != _sentence_contract(saved["sentences"]):
        raise ValueError("DIRECTED_WINDOW_SENTENCE_CONTRACT_CHANGED")
    experiment_groups.validate_holds(manifest, plan["sentences"])
    matched = ai_editor._assemble(manifest, analysis, plan, narration_timing=timing, verify_sources=True)
    if ai_editor.contract.rules_identity(manifest)["category"] == "weipin":
        experiment_groups.require_complete(plan["sentences"], matched["segments"])
    allowed = {Path(path) for path in manifest.get("material_sources", [])}
    if not allowed or any(Path(cut["video"]) not in allowed for segment in matched["segments"]
                          for cut in segment.get("span_parts") or [segment]):
        raise ValueError("DIRECTED_MATCH_SOURCE_OUTSIDE_TASK")
    for row in plan["rows"]:
        parts = row.get("parts", [])
        if not any(p.get("directed_window_us") for p in parts):
            continue
        actual = matched["segments"][row["index"] - 1]
        placed = actual.get("span_parts") or [actual]
        if (len(parts) != len(placed) or any(
                wanted.get("directed_window_us") and (
                    wanted["shot_id"] != (cut.get("shot_id") or cut.get("temporary_shot_id"))
                    or wanted["directed_window_us"] != cut.get("ai_selection", {}).get("window_us"))
                for wanted, cut in zip(parts, placed))):
            raise ValueError(f"DIRECTED_WINDOW_CHANGED:{row['index']}")
    matched["playback_policy_version"] = ai_editor.contract.playback.VERSION
    return matched
