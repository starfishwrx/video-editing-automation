"""Recheck actual runtime cuts and editable draft parameters for sentence groups."""
from __future__ import annotations

import filecmp
from pathlib import Path

from ..policy import continuity


def runtime_errors(clips, segments):
    errors = continuity.sequence_errors(segments)
    if errors:
        return errors
    grouped = {i: s for i, s in enumerate(segments) if s.get("continuity_group")}
    for index, segment in grouped.items():
        actual = [c for c in clips if c.raw_index == index]
        group = segment["continuity_group"]
        if continuity.is_experiment(group):
            member = next(m for m in group["input"]["members"] if m["index"] == index + 1)
            expected = member["parts"]
            if len(actual) != len(expected):
                errors.append("CONTINUITY_RUNTIME_MEMBER_MISSING")
                continue
            from ..ai_editor_contract import source_identity
            for clip, cut in zip(actual, expected):
                try:
                    valid_source = source_identity(clip.video) == cut["source_identity"]
                except OSError:
                    valid_source = False
                if (not valid_source or clip.continuity_group != group or clip.text != member["text"]
                        or round(clip.source_start * 1e6) != cut["start_us"]
                        or round(clip.duration * 1e6) != cut["target_us"]
                        or abs(clip.video_speed - (cut["end_us"] - cut["start_us"]) / cut["target_us"]) > 1e-8
                        or clip.transition or clip.transition_in_handle_us or clip.transition_out_handle_us):
                    errors.append("CONTINUITY_RUNTIME_CHANGED")
            if abs(sum(c.runtime_audio_us for c in actual) - member["audio_duration_us"]) > len(actual):
                errors.append("CONTINUITY_RUNTIME_CHANGED")
            continue
        if len(actual) != 1:
            errors.append("CONTINUITY_RUNTIME_MEMBER_MISSING")
            continue
        clip = actual[0]
        group = segment["continuity_group"]
        from ..ai_editor_contract import source_identity
        try:
            if source_identity(clip.video) != group["input"]["source_identity"]:
                errors.append("CONTINUITY_SOURCE_CHANGED")
        except OSError:
            errors.append("CONTINUITY_SOURCE_CHANGED")
        member = next(m for m in group["input"]["members"] if m["index"] == index + 1)
        if (clip.continuity_group != group or clip.text != member["text"]
                or Path(clip.video).resolve() != Path(group["input"]["source_identity"]["path"]).resolve()
                or round(clip.source_start * 1e6) != member["start_us"]
                or round(clip.duration * 1e6) != member["target_us"] or clip.video_speed != 1
                or clip.runtime_audio_us != member["audio_duration_us"] or clip.transition
                or clip.transition_in_handle_us or clip.transition_out_handle_us):
            errors.append("CONTINUITY_RUNTIME_CHANGED")
        if index > group["input"]["members"][0]["index"] - 1:
            prev = next((c for c in clips if c.raw_index == index - 1), None)
            if prev is None or clip.start_us != prev.start_us + round(prev.duration * 1e6) or clip.transition:
                errors.append("CONTINUITY_RUNTIME_DISCONTINUOUS")
    if any(c.continuity_group and c.raw_index not in grouped for c in clips):
        errors.append("CONTINUITY_RUNTIME_ORPHAN")
    for group in {s["continuity_group"]["input"]["members"][0]["index"]: s["continuity_group"]
                  for s in grouped.values()}.values():
        if not continuity.is_experiment(group):
            continue
        indices = {m["index"] - 1 for m in group["input"]["members"]}
        actual = [c for c in clips if c.raw_index in indices]
        if any(b.start_us != a.start_us + round(a.duration * 1e6) for a, b in zip(actual, actual[1:])):
            errors.append("CONTINUITY_RUNTIME_DISCONTINUOUS")
    return list(dict.fromkeys(errors))


def draft_errors(draft_dir, clips, *, source_overrides=None):
    """Read actual active native/plain document, not the plan passed to a writer."""
    grouped = [c for c in clips if c.continuity_group]
    if not grouped:
        return []
    from ..runtime.draft_documents import documents
    try:
        payload = documents(draft_dir)
        draft = payload["timeline"]
        videos = [s for t in draft.get("tracks", []) if t.get("type") == "video" for s in t.get("segments", [])]
        materials = {m["id"]: m for rows in draft["materials"].values() if isinstance(rows, list)
                     for m in rows if isinstance(m, dict) and "id" in m}
        checked = set()
        for clip in grouped:
            matching = [s for s in videos if s.get("target_timerange", {}).get("start") == clip.start_us]
            if len(matching) != 1:
                raise ValueError("CONTINUITY_DRAFT_MEMBER_MISSING")
            segment = matching[0]
            duration = round(clip.duration * 1e6)
            source_duration = round(duration * clip.video_speed)
            if (segment.get("source_timerange") != {"start": round(clip.source_start * 1e6), "duration": source_duration}
                    or segment.get("target_timerange") != {"start": clip.start_us, "duration": duration}
                    or abs(segment.get("speed", 0) - clip.video_speed) > 1e-8):
                raise ValueError("CONTINUITY_DRAFT_CUT_CHANGED")
            refs = [materials.get(r, {}) for r in segment.get("extra_material_refs", [])]
            if any(r.get("type") == "speed" and (abs(r.get("speed", 0) - clip.video_speed) > 1e-8
                                                  or r.get("curve_speed")) for r in refs):
                raise ValueError("CONTINUITY_DRAFT_SPEED_CHANGED")
            if any(r.get("type") == "transition" for r in refs):
                raise ValueError("CONTINUITY_DRAFT_TRANSITION_CHANGED")
            path = Path(materials[segment["material_id"]]["path"])
            if not path.is_absolute():
                path = Path(payload["reference_root"]) / path
            expected = Path((source_overrides or {}).get(str(Path(clip.video).resolve()), clip.video))
            pair = (str(path.resolve()), str(expected.resolve()))
            if pair not in checked:
                # Copies in editable drafts are expected; a different source is not.
                if not path.is_file() or not expected.is_file() or (path.resolve() != expected.resolve()
                        and not filecmp.cmp(path, expected, shallow=False)):
                    raise ValueError("CONTINUITY_DRAFT_SOURCE_CHANGED")
                checked.add(pair)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        return [str(exc) if str(exc).startswith("CONTINUITY_") else "CONTINUITY_DRAFT_UNREADABLE"]
    return []
