"""Apply one visually verified experiment across fixed adjacent sentences."""
from pathlib import Path
import json

from .. import ai_editor_contract as identity
from ..domain.timeline import Asset
from ..media.probe import probe_media
from ..selection.experiment_contract import contract_for, validate_group, validate_observation
from ..selection.temporal_groups import replace_group
from ..vision.experiment_review import PROTOCOL
from ..media.temporal_samples import validate as validate_samples


def _prior_origins(timeline, prior_origins, allowed, lo, hi):
    """Keep explicit unresolved preview placeholders separate from real footage."""
    assets = timeline.asset_map()
    unresolved = {u.get('sentence_id') for u in timeline.unresolved if u.get('reason') ==
        'NO_CONFIRMED_FIT_WITHIN_SEARCH_BUDGET; black is diagnostic only'}
    diagnostic = set()
    if timeline.delivery_mode == 'preview':
        for asset_id, asset in assets.items():
            events = [v for v in timeline.videos if v.asset_id == asset_id]
            if (events and str(Path(asset.path).resolve()) not in allowed
                    and all(v.sentence_ids and set(v.sentence_ids) <= unresolved
                        and any(isinstance(e, dict) and e.get('diagnostic_placeholder') is True
                            for e in v.evidence) for v in events)):
                diagnostic.add(asset_id)
    origins, seen = {}, set()
    for p in prior_origins:
        if p['asset_id'] in seen:
            raise ValueError('EXPERIMENT_PRIOR_ORIGIN_DUPLICATE')
        seen.add(p['asset_id'])
        asset = assets[p['asset_id']]
        if (p.get('clock') != 'same_source_clock' or identity.source_identity(asset.path) != p['proxy']
                or (p['original']['path'] not in allowed and asset.asset_id not in diagnostic)
                or identity.source_identity(p['original']['path']) != p['original']):
            raise ValueError('EXPERIMENT_PRIOR_ORIGIN_BINDING_INVALID')
        if p['original']['path'] in allowed:
            origins[asset.asset_id] = p['original']['path']
        elif asset.asset_id in diagnostic:
            # This is an existing diagnostic file, never a matched task source.
            origins[asset.asset_id] = p['proxy']['path']
        else:
            raise ValueError('EXPERIMENT_PRIOR_ORIGIN_BINDING_INVALID')
    for event in timeline.videos:
        if event.target_start_us >= hi or event.target_start_us + event.duration_us <= lo:
            if event.asset_id not in origins:
                if event.asset_id not in diagnostic:
                    raise ValueError('EXPERIMENT_EXISTING_ORIGIN_UNKNOWN')
                origins[event.asset_id] = identity.source_identity(assets[event.asset_id].path)['path']
    return origins


def apply(manifest, timeline, sentence_ids, roles, evidence, *, group_id, prior_origins=(), ffprobe="ffprobe", motion=None):
    from ..vision.experiment_pair_review import PROTOCOL as PAIR_PROTOCOL
    if evidence.get("protocol")==PAIR_PROTOCOL:
        return apply_pair(manifest,timeline,sentence_ids,roles,evidence,group_id=group_id,
                          prior_origins=prior_origins,ffprobe=ffprobe,motion=motion)
    if motion is not None:raise ValueError('EXPERIMENT_MOTION_REQUIRES_PAIR_EVIDENCE')
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
        raise ValueError("EXPERIMENT_INCOMPLETE_NEEDS_MATCHING_VERIFICATION")
    selected = [s for s in timeline.sentences if s.sentence_id in sentence_ids]
    lo, hi = selected[0].start_us, selected[-1].start_us + selected[-1].duration_us
    origins = _prior_origins(timeline, prior_origins, allowed, lo, hi)
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


def apply_pair(manifest,timeline,sentence_ids,roles,evidence,*,group_id,prior_origins=(),ffprobe="ffprobe",motion=None):
    from ..selection.timing_contract import split_sentences
    from ..selection.experiment_contract import validate_pair
    from ..vision.experiment_pair_review import PROTOCOL as PAIR_PROTOCOL
    if [s.text for s in timeline.sentences]!=[r["text"]for r in split_sentences(manifest)]:
        raise ValueError("EXPERIMENT_TIMELINE_SCRIPT_CHANGED")
    rules=contract_for(manifest);validate_group(timeline,sentence_ids,roles,rules)
    if (timeline.task_id!=manifest["task_id"] or evidence.get("task_id")!=manifest["task_id"]
        or evidence.get("protocol")!=PAIR_PROTOCOL or evidence.get("rules")!=identity.rules_identity(manifest)
        or evidence.get("context")!={"product":manifest["product"],"category":"weipin"}):
        raise ValueError("EXPERIMENT_PAIR_RULE_OR_CONTEXT_CHANGED")
    members=evidence.get("members")
    if not isinstance(members,list)or [m.get("part")for m in members]!=["A","B"]:
        raise ValueError("EXPERIMENT_PAIR_MEMBERS_INVALID")
    allowed={str(Path(p).resolve())for p in manifest["material_sources"]}
    visible={};bounds=[]
    for member in members:
        samples=validate_samples(member["samples"])
        if samples["original"]["path"]not in allowed:raise ValueError("EXPERIMENT_PAIR_OUTSIDE_TASK")
        a,b=member["window_frame_ids"]
        if type(a)is not int or type(b)is not int or not 0<=a<b<len(samples["frames"]):
            raise ValueError("EXPERIMENT_PAIR_WINDOW_INVALID")
        ids=sorted({round(a+i*(b-a)/7)for i in range(8)})
        if member["visible_frame_ids"]!=ids:raise ValueError("EXPERIMENT_PAIR_SHOWN_FRAMES_CHANGED")
        visible[member["part"]]=set(ids)
        bounds.append((samples["frames"][a]["pts_us"],
                       min(samples["source_end_us"],samples["frames"][b]["pts_us"]+16_667)))
    if identity.source_identity(evidence["sheet"]["path"])!=evidence["sheet"]:
        raise ValueError("EXPERIMENT_PAIR_SHEET_CHANGED")
    from ..vision.experiment_pair_review import input_binding
    binding=evidence.get("input_binding")or {};bound_file=binding.get("file")or {}
    try:
        binding_valid=(identity.source_identity(bound_file["path"])==bound_file
            and identity.file_sha256(bound_file["path"])==binding["sha256"]
            and json.loads(Path(bound_file["path"]).read_text("utf-8"))==input_binding(manifest,members,evidence["sheet"]["path"]))
    except (KeyError,OSError,ValueError,TypeError):binding_valid=False
    if not binding_valid:raise ValueError("EXPERIMENT_PAIR_REVIEW_INPUT_CHANGED")
    row=validate_pair(evidence["observation"],visible)
    if row["verdict"]!="matched":raise ValueError("EXPERIMENT_PAIR_NOT_CONFIRMED")
    if members[0]["samples"]["original"]==members[1]["samples"]["original"]and bounds[1][0]<bounds[0][1]:
        raise ValueError("EXPERIMENT_PAIR_SOURCE_ORDER_OR_REPLAY")
    selected=[s for s in timeline.sentences if s.sentence_id in sentence_ids]
    lo,hi=selected[0].start_us,selected[-1].start_us+selected[-1].duration_us
    minimum=[];intervals=[]
    for member,(left,right)in zip(members,bounds):
        samples=member["samples"];phases=[p for p in row["phases"]if p["part"]==member["part"]]
        phases_us=[{"start":samples["frames"][p["first_frame"]]["pts_us"],
            "end":min(samples["source_end_us"],samples["frames"][p["last_frame"]]["pts_us"]+16_667),
            "kind":"result"if p["kind"]=="result"else"action",
            "min_output_frames":rules["minimum_result_frames"]if p["kind"]=="result"else 1}for p in phases]
        for p in phases_us:
            if motion is None and p["kind"]=="result"and (p["end"]-p["start"])*timeline.canvas.fps_num<rules["minimum_result_frames"]*1_000_000*timeline.canvas.fps_den:
                raise ValueError("EXPERIMENT_RESULT_WINDOW_TOO_SHORT")
        shown=member["visible_frame_ids"]
        first=min(p["first_frame"]for p in phases);last=max(p["last_frame"]for p in phases)
        before=max((i for i in shown if i<first),default=shown[0])
        after=min((i for i in shown if i>last),default=shown[-1])
        minimum.append([max(left,samples["frames"][before]["pts_us"]),
            min(right,samples["frames"][after]["pts_us"]+16_667)])
        intervals.append(phases_us)
    required=sum(b-a for a,b in minimum);maximum=sum(b-a for a,b in bounds);duration=hi-lo
    planned=[]
    if motion is not None:
        from ..vision.experiment_motion_review import validate as validate_motion
        from ..selection.experiment_retiming import windows,fit_group
        observed=validate_motion(manifest,evidence,motion)
        planned=fit_group(windows(members,observed,rules['minimum_result_frames'],fps=timeline.canvas.fps_num/timeline.canvas.fps_den),duration,
            fps=timeline.canvas.fps_num/timeline.canvas.fps_den)
    else:
        if duration<required:raise ValueError("EXPERIMENT_DURATION_CONFLICT_WITH_COMPATIBLE_SENTENCES")
        if duration>maximum:raise ValueError("EXPERIMENT_SOURCE_TOO_SHORT_NO_FREEZE_ALLOWED")
        extra=duration-required
        for i,((left,right),(a,b),protected)in enumerate(zip(bounds,minimum,intervals)):
            take=min(extra,(a-left)+(right-b));extra-=take
            extend_left=min(a-left,take//2);a-=extend_left;b+=min(right-b,take-extend_left)
            a=max(left,b-((minimum[i][1]-minimum[i][0])+take))
            planned.append({'source_start_us':a,'source_end_us':b,'target_duration_us':b-a,
                'protected_intervals':[{'start':a,'end':b,'kind':'action','min_output_frames':1}]+protected})
    clips=[];added=[];origins=_prior_origins(timeline,prior_origins,allowed,lo,hi)
    for i,(member,plan)in enumerate(zip(members,planned)):
        samples=member["samples"];facts=probe_media(samples["source"]["path"],ffprobe=ffprobe)
        asset=Asset(f"{group_id}-{i}",samples["source"]["path"],"video",facts["duration_us"],facts["width"],facts["height"],facts["fps_num"],facts["fps_den"])
        added.append(asset);origins[asset.asset_id]=samples["original"]["path"]
        parts=plan['retiming']['parts']if motion is not None else [plan]
        for part in parts:
            a,b=part['source_start_us'],part['source_end_us']
            protected=[{**p,'start':max(a,p['start']),'end':min(b,p['end'])}
                for p in plan['protected_intervals']if max(a,p['start'])<min(b,p['end'])]
            clips.append({'asset_id':asset.asset_id,'source_start_us':a,'source_end_us':b,
                'target_duration_us':part['segment_us']if motion is not None else part['target_duration_us'],
                'protected_intervals':protected,
                "evidence":[{"protocol":PAIR_PROTOCOL,"original":samples["original"],"pair_observation":row,
                    "motion_observation":motion,"status":"OUTPUT_REVIEW_PENDING"}]
                    +([{'bounded_process_speed_cap':3}]if motion is not None and not part['locked']else [])})
    transition=(members[0]["samples"]["original"]["path"],members[1]["samples"]["original"]["path"])
    return replace_group(timeline,sentence_ids,clips,group_id=group_id,assets=added,original_paths=origins,matched_pairs=(transition,))
