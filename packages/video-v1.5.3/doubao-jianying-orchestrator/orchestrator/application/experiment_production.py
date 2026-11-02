"""Apply a grounded single-original experiment to fixed adjacent sentences."""
from pathlib import Path

from .. import ai_editor_contract as identity
from ..domain.timeline import Asset
from ..media.probe import probe_media
from ..selection.experiment_contract import contract_for, validate_group, validate_observation
from ..selection.temporal_groups import replace_group
from ..vision.experiment_review import PROTOCOL
from ..media.temporal_samples import validate as validate_samples


def apply(manifest, timeline, sentence_ids, roles, evidence, *, group_id, prior_origins=(), ffprobe="ffprobe"):
    from ..selection.timing_contract import split_sentences
    if [s.text for s in timeline.sentences] != [r["text"] for r in split_sentences(manifest)]:
        raise ValueError("EXPERIMENT_TIMELINE_SCRIPT_CHANGED")
    rules = contract_for(manifest)
    validate_group(timeline, sentence_ids, roles, rules)
    if (timeline.task_id != manifest["task_id"] or evidence.get("task_id") != manifest["task_id"]
            or evidence.get("protocol") != PROTOCOL
            or evidence.get("rules") != identity.rules_identity(manifest)
            or evidence.get("context") != {"product":manifest["product"],"category":identity.rules_identity(manifest)["category"]}):
        raise ValueError("EXPERIMENT_EVIDENCE_RULE_OR_TASK_CHANGED")
    original = evidence["original"]
    allowed = {str(Path(p).resolve()) for p in manifest["material_sources"]}
    if original["path"] not in allowed or identity.source_identity(original["path"]) != original:
        raise ValueError("EXPERIMENT_ORIGINAL_CHANGED_OR_OUTSIDE_TASK")
    samples = evidence["samples"]
    validate_samples(samples)
    if samples["original"] != original:
        raise ValueError("EXPERIMENT_ORIGINAL_PROXY_MISMATCH")
    frames = samples["frames"]
    row = validate_observation(evidence["observation"], frames, rules["phases"])
    if row["verdict"] != "complete":
        raise ValueError("EXPERIMENT_NOT_COMPLETE_SAME_ORIGINAL")
    selected = [s for s in timeline.sentences if s.sentence_id in sentence_ids]
    lo, hi = selected[0].start_us, selected[-1].start_us + selected[-1].duration_us
    origins = {}
    assets = timeline.asset_map()
    for p in prior_origins:
        a = assets[p["asset_id"]]
        if (p.get("clock") != "same_source_clock" or identity.source_identity(a.path) != p["proxy"]
                or p["original"]["path"] not in allowed
                or identity.source_identity(p["original"]["path"]) != p["original"]):
            raise ValueError("EXPERIMENT_PRIOR_ORIGIN_BINDING_INVALID")
        origins[a.asset_id] = p["original"]["path"]
    for event in timeline.videos:
        if event.target_start_us >= hi or event.target_start_us+event.duration_us <= lo:
            if event.asset_id not in origins:
                raise ValueError("EXPERIMENT_EXISTING_ORIGIN_UNKNOWN")
    duration = hi - lo
    intervals = [{"start": frames[p["first_frame"]]["pts_us"],
                  "end": min(samples["source_end_us"], frames[p["last_frame"]]["pts_us"]+16_667),
                  "kind": "result" if p["kind"] == "result" else "action",
                  "min_output_frames": rules["minimum_result_frames"] if p["kind"] == "result" else 1}
                 for p in row["phases"]]
    minimum_result_us = (rules["minimum_result_frames"]*1_000_000*timeline.canvas.fps_den
                         + timeline.canvas.fps_num-1)//timeline.canvas.fps_num
    if any(p["kind"]=="result" and p["end"]-p["start"]<minimum_result_us for p in intervals):
        raise ValueError("EXPERIMENT_RESULT_WINDOW_TOO_SHORT")
    start, end = min(p["start"] for p in intervals), max(p["end"] for p in intervals)
    # Native-video milestones are sampled, not exact boundaries. Keep 100ms
    # real context on each side and do not speed unobserved or required actions.
    start, end = max(0,start-100_000), min(samples["source_end_us"],end+100_000)
    if end-start > duration:
        raise ValueError("EXPERIMENT_DURATION_CONFLICT_WITH_COMPATIBLE_SENTENCES")
    if end-start < duration:
        start = max(0,start-(duration-(end-start))//2)
        end = min(samples["source_end_us"],start+duration)
        start = max(0,end-duration)
    if end-start != duration:
        raise ValueError("EXPERIMENT_SOURCE_TOO_SHORT_NO_FREEZE_ALLOWED")
    facts = probe_media(samples["source"]["path"], ffprobe=ffprobe)
    asset = Asset(group_id+"-source", samples["source"]["path"], "video", facts["duration_us"],
                  facts["width"], facts["height"], facts["fps_num"], facts["fps_den"])
    # Protect the entire source span at 1x; retain individual result constraints
    # as well, so merging action protection never loses readability checking.
    protected = [{"start":start,"end":end,"kind":"action","min_output_frames":1}] + intervals
    clip = {"asset_id":asset.asset_id,"source_start_us":start,"source_end_us":end,
            "target_duration_us":duration,"protected_intervals":protected,
            "evidence":[{"protocol":PROTOCOL,"original":original,"observation":row,
                         "rules":evidence["rules"],"status":"OUTPUT_REVIEW_PENDING"}]}
    origins[asset.asset_id] = original["path"]
    return replace_group(timeline, sentence_ids, [clip], group_id=group_id, assets=(asset,), original_paths=origins)
