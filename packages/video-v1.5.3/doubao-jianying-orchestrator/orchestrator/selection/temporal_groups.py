"""Preview-only visual groups spanning fixed adjacent narration sentences."""
from dataclasses import replace
from fractions import Fraction
import os
from pathlib import Path

from ..domain.timeline import SourceInterval, VideoEvent
from .paper_retiming import plan_retiming


def _source_key(asset, by_id, original_paths=None):
    if original_paths is not None:
        if asset.asset_id not in original_paths:
            raise ValueError("TEMPORAL_GROUP_ORIGIN_UNKNOWN")
        return os.path.normcase(str(Path(original_paths[asset.asset_id]).resolve()))
    seen = set()
    while asset.origin_asset_id:
        if asset.asset_id in seen:
            raise ValueError("TEMPORAL_GROUP_ORIGIN_CYCLE")
        seen.add(asset.asset_id)
        asset = by_id[asset.origin_asset_id]
    return os.path.normcase(str(Path(asset.path).resolve()))


def replace_group(timeline, sentence_ids, clips, *, group_id, assets=(), group_kind="continuous_experiment", original_paths=None):
    """Fit already grounded source windows without moving any audio or caption.

    Clips supply explicit target budgets and protected source intervals. This is
    editorial assembly, not evidence acceptance; output review stays pending.
    """
    timeline.validate()
    if group_kind not in {"continuous_experiment", "montage"}:
        raise ValueError("TEMPORAL_GROUP_KIND_INVALID")
    if timeline.delivery_mode != "preview":
        raise ValueError("TEMPORAL_GROUP_PREVIEW_ONLY")
    ids = tuple(sentence_ids)
    selected = [s for s in timeline.sentences if s.sentence_id in ids]
    if not ids or tuple(s.sentence_id for s in selected) != ids:
        raise ValueError("TEMPORAL_GROUP_SENTENCE_ORDER")
    lo, hi = selected[0].start_us, selected[-1].start_us + selected[-1].duration_us
    overlap = [s.sentence_id for s in timeline.sentences
               if s.start_us < hi and s.start_us + s.duration_us > lo]
    if tuple(overlap) != ids:
        raise ValueError("TEMPORAL_GROUP_NOT_ADJACENT")
    kept = []
    for event in timeline.videos:
        end = event.target_start_us + event.duration_us
        if event.target_start_us < hi and end > lo:
            if event.target_start_us < lo or end > hi:
                raise ValueError("TEMPORAL_GROUP_CUTS_EXISTING_EVENT")
        else:
            kept.append(event)
    added = tuple(assets)
    all_assets = timeline.assets + added
    by_id = {a.asset_id: a for a in all_assets}
    if len(by_id) != len(all_assets):
        raise ValueError("TEMPORAL_GROUP_DUPLICATE_ASSET")
    previous_source = None
    previous_end = None
    for clip in clips:
        source = _source_key(by_id[clip["asset_id"]], by_id, original_paths)
        if group_kind == "continuous_experiment" and previous_source is not None:
            if source != previous_source:
                raise ValueError("TEMPORAL_GROUP_EXPERIMENT_DIFFERENT_ORIGINALS")
            if clip["source_start_us"] != previous_end:
                raise ValueError("TEMPORAL_GROUP_EXPERIMENT_SOURCE_GAP")
        previous_source, previous_end = source, clip["source_end_us"]
    replaced_placeholders = {sid for event in timeline.videos if lo <= event.target_start_us < hi
        and any(isinstance(e, dict) and e.get("diagnostic_placeholder") is True for e in event.evidence)
        for sid in event.sentence_ids}
    obsolete = tuple(u for u in timeline.unresolved if u.get("sentence_id") in replaced_placeholders
        and u.get("reason") == "NO_CONFIRMED_FIT_WITHIN_SEARCH_BUDGET; black is diagnostic only")
    videos, cursor, used = [], lo, []
    for i, clip in enumerate(clips):
        asset = by_id[clip["asset_id"]]
        if asset.kind != "video":
            raise ValueError("TEMPORAL_GROUP_VIDEO_REQUIRED")
        start, end = clip["source_start_us"], clip["source_end_us"]
        if not 0 <= start < end <= asset.duration_us:
            raise ValueError("TEMPORAL_GROUP_SOURCE_BOUNDS")
        # Never replay footage retained outside this group, or another clip.
        prior = [(_source_key(by_id[v.asset_id], by_id, original_paths), v.source_start_us,
                  v.source_start_us + v.source_duration_us) for v in kept] + used
        if any(path == _source_key(asset, by_id, original_paths) and max(start, left) < min(end, right)
               for path, left, right in prior):
            raise ValueError("TEMPORAL_GROUP_SOURCE_REPLAY")
        plan = plan_retiming(start, end, clip["protected_intervals"], clip["target_duration_us"],
            fps=timeline.canvas.fps_num / timeline.canvas.fps_den)
        if not plan["ok"]:
            raise ValueError("TEMPORAL_GROUP_" + plan["reason"])
        evidence = tuple(clip.get("evidence", ())) + ({"group_id": group_id,
            "status": "OUTPUT_REVIEW_PENDING", "replaced_visual_diagnostics": obsolete},)
        for j, part in enumerate(plan["parts"]):
            duration = part["segment_us"]
            pstart, pend = part["source_start_us"], part["source_end_us"]
            speed = Fraction(pend - pstart, duration)
            affected = tuple(s.sentence_id for s in selected
                if s.start_us < cursor + duration and s.start_us + s.duration_us > cursor)
            protected = tuple(SourceInterval(f"{group_id}-{i}-{j}-{k}",
                max(pstart, interval["start"]), min(pend, interval["end"]),
                interval.get("kind", "action"), interval.get("min_output_frames", 1))
                for k, interval in enumerate(clip["protected_intervals"])
                if max(pstart, interval["start"]) < min(pend, interval["end"]))
            videos.append(VideoEvent(f"{group_id}-{i}-{j}", asset.asset_id, affected,
                cursor, duration, pstart, pend - pstart, speed.numerator, speed.denominator,
                protected_intervals=protected, evidence=evidence))
            cursor += duration
        used.append((_source_key(asset, by_id, original_paths), start, end))
    if cursor != hi:
        raise ValueError("TEMPORAL_GROUP_TARGET_COVERAGE")
    # Only the exact diagnostic black-picture issue becomes obsolete. Source
    # review does not resolve audio, captions, identity or prior quality issues.
    unresolved = tuple(u for u in timeline.unresolved if u not in obsolete)
    unresolved += ({"group_id": group_id, "sentence_ids": ids,
                    "reason": "TEMPORAL_GROUP_OUTPUT_REVIEW_PENDING"},)
    result = replace(timeline, revision=timeline.revision + 1, assets=all_assets,
        videos=tuple(sorted(kept + videos, key=lambda v: v.target_start_us)), unresolved=unresolved)
    result.validate()
    return result
