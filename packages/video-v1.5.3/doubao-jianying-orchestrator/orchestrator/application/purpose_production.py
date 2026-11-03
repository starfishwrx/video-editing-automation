"""Opt-in directed selection around fixed audio; no draft or business writes."""
from __future__ import annotations

from collections import Counter
from fractions import Fraction
from dataclasses import replace
import json
from pathlib import Path
import subprocess

from .. import ai_editor_contract as contract
from ..audio.final_bgm import prepare_task_bgm
from ..domain.timeline import (Asset, AudioEvent, Canvas, EditTimeline, Sentence,
    SourceInterval, SubtitleEvent, VideoEvent)
from ..media.probe import probe_media
from ..media.video_encode import run_h264_encode
from ..media.timeline_render import render_timeline, timeline_identity
from ..quality.production_qc import check_production
from ..runtime.ready_work import iter_ready
from ..selection import ai_editor, purpose_selection, purpose_retrieval
from ..vision import purpose_review as vision
from ..vision import purpose_inventory


def _audio_binding(narration):
    return [{"index":r["index"],"text":r["text"],"path":str(Path(r["tts_path"]).resolve()),
        "duration_us":r["audio_duration_us"],"source_identity":contract.source_identity(r["tts_path"])} for r in narration["rows"]]


def _context(manifest):
    return {k:manifest.get(k) for k in ("task_id","product","category","full_script","voice")}


def _request_coverage(requests, rows):
    if (not isinstance(requests,list) or len(requests) != len(rows)
            or any(not isinstance(r,dict) or type(r.get("index")) is not int for r in requests)
            or [(r.get("index"),r.get("text")) for r in requests] != [(i,r["text"]) for i,r in enumerate(rows,1)]):
        raise ValueError("PURPOSE_REQUEST_COVERAGE")


def _binding(manifest, narration, artifact, *, rules_required=False):
    from ..selection.timing_contract import split_sentences
    sources=sorted(str(Path(p).resolve()) for p in manifest["material_sources"])
    if (artifact.get("task_id") != manifest["task_id"] or artifact.get("sources") != sources
            or artifact.get("context") != _context(manifest)
            or artifact.get("narration") != _audio_binding(narration)
            or narration.get("voice") != manifest.get("voice")
            or [s["text"] for s in split_sentences(manifest)] != [r["text"] for r in narration["rows"]]):
        raise ValueError("PURPOSE_ARTIFACT_INPUT_CHANGED")
    _request_coverage(artifact.get("requests"),narration["rows"])
    if rules_required:
        from ..selection.opening_hook import RULE,validate_completed
        if artifact.get("rules") != contract.rules_identity(manifest) or artifact.get("protocol") != vision.PROTOCOL:
            raise ValueError("PURPOSE_WEIPIN_RULE_IDENTITY_CHANGED")
        if artifact.get('opening_hook_rule')!=RULE:
            raise ValueError('PURPOSE_OPENING_HOOK_RULE_CHANGED')
        validate_completed(artifact['requests'])


def _parallel(jobs, prepare, consume, *, workers=6):
    prepared = {}
    def prep(job):
        prepared[id(job)] = prepare(job)
    def call(job):
        return consume(prepared.pop(id(job)))
    def failure(job, error):
        if not isinstance(error, (ValueError, TypeError, KeyError, OSError, RuntimeError, subprocess.SubprocessError)):
            raise error
        return {"error": str(error), "status": "EVIDENCE_UNKNOWN"}
    yield from iter_ready(jobs, prep, call, prep_workers=2, api_workers=workers, on_error=failure)


def inspect(manifest, analysis, narration, directory, *, workers=6):
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    sources = {str(Path(p).resolve()) for p in manifest["material_sources"]}
    shots = analysis["shots"]
    if (not shots or len({s["shot_id"] for s in shots}) != len(shots)
            or any(str(Path(s["video"]).resolve()) not in sources for s in shots)):
        raise ValueError("PURPOSE_SOURCE_SCOPE_MISMATCH")
    from ..selection.timing_contract import split_sentences
    if ([s["text"] for s in split_sentences(manifest)] != [r["text"] for r in narration["rows"]]
            or narration.get("voice") != manifest.get("voice")):
        raise ValueError("PURPOSE_NARRATION_TEXT_MISMATCH")
    for audio in narration["rows"]:
        if audio.get("timing_certainty") != "audio" or type(audio.get("audio_duration_us")) is not int or audio["audio_duration_us"] <= 0:
            raise ValueError("PURPOSE_REAL_NARRATION_REQUIRED")
    requests = vision.requirements(manifest, narration, root)
    scans = {}
    def prepare(s):
        return ai_editor._review_media(s, root, dense=True, fit=True)
    # Prepared media is passed through a namespaced job, not a shared mutable pool.
    def prep(s):
        return (s, prepare(s))
    def call(pair):
        s, media = pair
        return vision.scan(manifest, requests, s, media, root)
    # Persist each completed observation before consuming another result.
    for shot, row in _parallel(shots, prep, call, workers=workers):
        scans[shot["shot_id"]] = row
        vision.save(root / "scan_checkpoint.json", {"completed":len(scans), "total":len(shots), "scans":scans})
    report = {"schema":"purpose-search-index-v1", "task_id":manifest["task_id"], "requests":requests,
        "scans":scans, "sources":sorted(sources), "narration":_audio_binding(narration), "context":_context(manifest),
        "scope":"task-local search hints; not evidence"}
    vision.save(root / "index.json", report)
    return report


def select(manifest, analysis, narration, index, directory, *, workers=6, candidate_limit=3):
    root = Path(directory)
    if (root / "selection.json").exists():
        raise ValueError("PURPOSE_SELECTION_EXISTS_USE_NEW_ATTEMPT")
    _binding(manifest,narration,index)
    if not 1 <= candidate_limit <= 3:
        raise ValueError("PURPOSE_CANDIDATE_LIMIT")
    shots = {s["shot_id"]: s for s in analysis["shots"]}
    sources={str(Path(p).resolve()) for p in manifest["material_sources"]}
    if (len(shots) != len(analysis["shots"]) or any(str(Path(s["video"]).resolve()) not in sources for s in shots.values())
            or any(sid not in shots for sid in index["scans"])):
        raise ValueError("PURPOSE_SOURCE_SCOPE_MISMATCH")
    audio = {r["index"]:r for r in narration["rows"]}
    # The old corpus descriptions remain retrieval hints. Recompile only the
    # fixed script under current category rules rather than rescanning media.
    requests = [{**r, "segment_us":audio[r["index"]]["audio_duration_us"]}
        for r in vision.requirements(manifest,narration,root / "requirements")]
    recalled=purpose_inventory.recall(manifest,requests,list(shots.values()),index,root/'retrieval')
    recalled=purpose_retrieval.allocate(requests,recalled,purpose_retrieval.catalog(list(shots.values()),index['scans']),wave_size=candidate_limit)
    rankings={i:[c['shot_id'] for c in cs] for i,cs in recalled.items()}
    experiment_pairs=[]
    if contract.editorial_context(manifest)["category"]=="weipin":
        from ..selection.experiment_contract import rank_pairs,adjacency_hint
        def pool(topic):
            return sorted({shots[sid]["video"] for r in requests if r.get("topic")==topic
                for sid in rankings[r["index"]][:candidate_limit] if sid in shots})
        first=pool("absorption");verification=set(pool("rewet"))
        # A tissue check can be missed by the old semantic index. Recall the
        # next camera file anyway, inside the task whitelist, for pixel review.
        verification.update(b for a in first for b in sources if adjacency_hint(a,b)["adjacent"])
        experiment_pairs=rank_pairs(first,sorted(verification))[:8]
    jobs = []
    rows = {r["index"]:{"index":r["index"],"text":r["text"],"candidates":[]} for r in requests}
    pending={}
    for request in requests:
        if contract.editorial_context(manifest)["category"] == "weipin" and request.get("topic") in {"absorption","rewet"}:
            rows[request["index"]]["retrieved_candidates"] = 0
            rows[request["index"]]["reason"] = "SAME_EXPERIMENT_GROUP_REQUIRED"
            continue
        windows=purpose_retrieval.windows(recalled[request['index']],list(shots.values()))
        jobs.extend((request,shot) for shot in windows[:candidate_limit])
        pending[request['index']]=windows[candidate_limit:candidate_limit*2]
        rows[request['index']]['retrieved_candidates']=len(windows)
        rows[request['index']]['retrieval_hints']=recalled[request['index']]
    def prepare(job):
        request,shot = job
        return request,shot,ai_editor._review_media(shot,root,dense=True,fit=True)
    def consume(job):
        request,shot,media = job
        observation = vision.review(manifest,request,shot,media,root)
        plan = purpose_selection.fit(observation,shot,request)
        if plan.get("ok") and request["require_result"]:
            result_us = max((p["end"]-p["start"] for p in observation["phases"] if p["kind"] == "result"),default=0)
            if result_us < 200000:
                plan = {"ok":False,"reason":"RESULT_HOLD_TOO_SHORT"}
        return {"shot_id":shot["shot_id"],"source":shot["video"],"observation":observation,"plan":plan}
    for job, result in _parallel(jobs,prepare,consume,workers=workers):
        request,shot = job
        result.setdefault("shot_id",shot["shot_id"])
        result.setdefault("source",shot["video"])
        rows[request["index"]]["candidates"].append(result)
        vision.save(root / f"sentence-{request['index']:02d}.json",rows[request["index"]])
    used=[]
    # Reserve functional evidence before allocating generic display statements.
    for request in sorted(requests,key=lambda r:(not (r["require_action"] or r["require_result"]
            or any(c["type"] != "display" for c in r["checks"])),
            sum(bool(c.get("plan",{}).get("ok")) for c in rows[r["index"]]["candidates"]),r["index"])):
        row = rows[request["index"]]
        from ..selection.shot_usability import is_display
        feasible=purpose_selection.eligible(row['candidates'],used)
        # One bounded refill, only when the first wave yields no usable window.
        # Rejected candidates cannot be promoted from text metadata alone.
        if is_display(request) and not feasible and pending.get(request['index']):
            row['refill_reason']='NO_USABLE_UNREPEATED_FIRST_WAVE'
            for (_,shot),candidate in _parallel([(request,s) for s in pending[request['index']]],prepare,consume,workers=workers):
                candidate.setdefault('shot_id',shot['shot_id'])
                candidate.setdefault('source',shot['video'])
                row['candidates'].append(candidate)
                vision.save(root/f"sentence-{request['index']:02d}.json",row)
            feasible=purpose_selection.eligible(row['candidates'],used)
        if is_display(request) and len(feasible)>=2:
            from ..vision.display_compare import compare
            try:row['comparison']=compare(manifest,request,feasible,root/f"display-comparison-{request['index']:02d}")
            except (ValueError,OSError,RuntimeError) as error:
                row['comparison_error']=str(error)
                for c in feasible:c['plan']={'ok':False,'reason':'DISPLAY_COMPARISON_UNKNOWN'}
        winner = purpose_selection.choose(row["candidates"],used)
        row["selected"] = winner
        row["status"] = "SELECTED_FOR_PREVIEW" if winner else "EVIDENCE_OR_TIMING_PENDING"
        if winner:
            used.append({"source":winner["source"],"start":winner["plan"]["source_start_us"],"end":winner["plan"]["source_end_us"]})
    from ..selection.opening_hook import RULE
    result={"schema":"purpose-selection-v1","task_id":manifest["task_id"],"requests":requests,'opening_hook_rule':RULE,
        "rules":contract.rules_identity(manifest),"protocol":vision.PROTOCOL,
        "old_index_use":"retrieval descriptions only; requirements recompiled under current rules",
        "experiment_pair_candidates":experiment_pairs,
        "revision":1,
        "sources":sorted(sources),"narration":_audio_binding(narration),"context":_context(manifest),
        "rows":[rows[i] for i in sorted(rows)],"formal_delivery_allowed":False,
        "search_limit":candidate_limit,"maximum_display_reviews":candidate_limit*2,
        "retrieval_protocol":purpose_inventory.PROTOCOL,
        "not_found_means":"not confirmed within this bounded search, not a library-wide gap"}
    vision.save(root / "selection.json",result)
    return result


def materialize(manifest, narration, selection, directory, *, ffmpeg="ffmpeg", ffprobe="ffprobe", width=540, music=None):
    """Normalize selected media explicitly, preserving its source clock in provenance."""
    root=Path(directory)
    _binding(manifest,narration,selection,rules_required=True)
    _request_coverage(selection.get("rows"),narration["rows"])
    requests={r["index"]:r for r in selection["requests"]}
    if contract.editorial_context(manifest)["category"] == "weipin":
        from ..selection.experiment_contract import contract_for
        experiment_rules = contract_for(manifest)
        topics = set(experiment_rules["allowed_roles"]) | set(experiment_rules["forbidden_roles"])
        for row in selection["rows"]:
            topic = requests[row["index"]].get("topic")
            if topic not in topics:
                raise ValueError("PURPOSE_WEIPIN_TOPIC_REQUIRED")
            if row["selected"] is not None and topic in {"absorption","rewet"}:
                raise ValueError("PURPOSE_WEIPIN_EXPERIMENT_REQUIRES_GROUP_ASSEMBLY")
    sources={str(Path(p).resolve()) for p in manifest["material_sources"]}
    occupied=[]
    for row in selection["rows"]:
        winner=row["selected"]
        if winner is None:
            continue
        observation=winner["observation"]
        if (str(Path(winner["source"]).resolve()) not in sources
                or observation.get("source_identity") != contract.source_identity(winner["source"])):
            raise ValueError("PURPOSE_SELECTION_SOURCE_CHANGED")
        bound=observation["reviewed_window_us"]
        shot={"source_start":bound["start"]/1e6,"source_end":bound["end"]/1e6}
        if purpose_selection.fit(observation,shot,requests[row["index"]]) != winner["plan"]:
            raise ValueError("PURPOSE_SELECTION_PLAN_CHANGED")
        left,right=winner["plan"]["source_start_us"],winner["plan"]["source_end_us"]
        if any(winner["source"] == old[0] and max(left,old[1]) < min(right,old[2]) for old in occupied):
            raise ValueError("PURPOSE_SELECTION_SOURCE_REPLAY")
        occupied.append((winner["source"],left,right))
    root.mkdir(parents=True,exist_ok=False)
    assets=[]; audio_events=[]; sentences=[]; subtitles=[]; videos=[]; unresolved=[]; cursor=0
    request_map={r["index"]:r for r in selection["requests"]}
    selected={r["index"]:r["selected"] for r in selection["rows"]}
    source_map={}; provenance=[]
    duration=sum(a["audio_duration_us"] for a in narration["rows"])
    if music and (music.get("task_id") != manifest["task_id"] or music.get("event_duration_us") != duration):
        raise ValueError("PURPOSE_BGM_TASK_OR_DURATION_CHANGED")
    def video_asset(source):
        if source in source_map:
            return source_map[source]
        i=len(source_map)+1
        target=root / f"normalized-{i:02d}.mp4"
        # iPhone source rotation and variable PTS are baked once into a new CFR
        # asset. Original source and its evidence remain immutable and recorded.
        original=contract.source_identity(source)
        run_h264_encode(str(ffmpeg),["-v","error","-nostdin","-threads","1","-i",str(source),"-an",
            "-vf",f"fps=60:eof_action=pass,scale={width}:{round(width*1920/1080/2)*2}:force_original_aspect_ratio=decrease,pad={width}:{round(width*1920/1080/2)*2}:(ow-iw)/2:(oh-ih)/2",
            "-r","60","-fps_mode","cfr","-video_track_timescale","60000"], target,
            trailing=["-pix_fmt","yuv420p","-metadata:s:v:0","rotate=0"],timeout=300)
        if original != contract.source_identity(source):
            raise ValueError("PURPOSE_SOURCE_CHANGED_DURING_NORMALIZATION")
        facts=probe_media(target,ffprobe=ffprobe)
        a=Asset(f"video-{i}",str(target),"video",facts["duration_us"],facts["width"],facts["height"],facts["fps_num"],facts["fps_den"])
        assets.append(a);source_map[source]=a
        provenance.append({"original":original,"normalized":str(target),"normalization":"autorotate, CFR60, same source clock, portrait proxy",
            "semantic_acceptance":"pending actual-output review"})
        return a
    black=root / "missing.mp4"
    if any(v is None for v in selected.values()):
        run_h264_encode(str(ffmpeg),["-v","error","-nostdin","-f","lavfi","-i",
            f"color=c=black:s={width}x{round(width*1920/1080/2)*2}:r=60:d={duration/1e6+1}"],black,
            trailing=["-pix_fmt","yuv420p"],timeout=90)
    for row in narration["rows"]:
        i=row["index"];sid=f"sentence-{i:04d}";d=row["audio_duration_us"];a_id=f"narration-{i}"
        facts=probe_media(row["tts_path"],ffprobe=ffprobe)
        if facts["duration_us"] != d:
            raise ValueError("PURPOSE_FINAL_NARRATION_CHANGED")
        assets.append(Asset(a_id,row["tts_path"],"audio",d))
        audio_events.append(AudioEvent(a_id,a_id,"narration",cursor,d,sentence_ids=(sid,)))
        sentences.append(Sentence(sid,row["text"],cursor,d,a_id,tuple(request_map[i]["checks"])))
        import unicodedata
        text="".join(c for c in row["text"] if not unicodedata.category(c).startswith("P"))
        subtitles.append(SubtitleEvent(f"subtitle-{i}",sid,text,cursor,d))
        winner=selected[i]
        if winner:
            asset=video_asset(winner["source"])
            parts=winner["plan"]["parts"]
            missing=[c for c in winner["observation"]["checks"] if c["status"] != "supported"]
            if missing:
                unresolved.append({"sentence_id":sid,"reason":"PARTIAL_POINT_SUPPORT","checks":missing})
        else:
            asset=video_asset(str(black))
            parts=[{"source_start_us":0,"source_end_us":d,"segment_us":d,"locked":False}]
            unresolved.append({"sentence_id":sid,"reason":"NO_CONFIRMED_FIT_WITHIN_SEARCH_BUDGET; black is diagnostic only"})
        for j,p in enumerate(parts,1):
            ratio=Fraction(p["source_end_us"]-p["source_start_us"],p["segment_us"])
            protected=()
            if winner and p["locked"]:
                protected=[SourceInterval(f"protected-{i}-{j}",p["source_start_us"],p["source_end_us"],"action",1)]
                for k,phase in enumerate(winner["observation"]["phases"]):
                    if phase["kind"] == "result" and p["source_start_us"] <= phase["start"] < phase["end"] <= p["source_end_us"]:
                        protected.append(SourceInterval(f"result-{i}-{j}-{k}",phase["start"],phase["end"],"result",6))
                protected=tuple(protected)
            videos.append(VideoEvent(f"video-{i}-{j}",asset.asset_id,(sid,),cursor,p["segment_us"],
                p["source_start_us"],p["source_end_us"]-p["source_start_us"],ratio.numerator,ratio.denominator,
                protected_intervals=protected,evidence=tuple(winner["observation"]["checks"]) if winner else ({"diagnostic_placeholder":True},)))
            cursor+=p["segment_us"]
    # An arbitrary microsecond start may lie between two normalized source
    # frames. Align ordinary displays to an actual decoder PTS at unchanged 1x;
    # physical actions/results never take this path. Recheck the changed output.
    from ..media import action_frames
    from ..media.frame_boundaries import display_start
    alignment=[];frame_ledgers={}
    asset_map={a.asset_id:a for a in assets}
    for j,event in enumerate(videos):
        request=request_map[int(event.sentence_ids[0].split("-")[-1])]
        winner=selected[request["index"]]
        physical=winner and any(p["kind"] in {"key_action","result"} for p in winner["observation"]["phases"])
        if (physical or request["require_action"] or request["require_result"] or event.speed_num != event.speed_den
                or (winner and len(winner["plan"]["parts"]) != 1)):
            continue
        asset=asset_map[event.asset_id]
        if asset.asset_id not in frame_ledgers:
            frame_ledgers[asset.asset_id]=action_frames.probe(asset.path,root / "frame_metadata",ffprobe=str(ffprobe))
        pts=[f["pts_us"] for f in frame_ledgers[asset.asset_id]["frames"]]
        start=display_start(event.source_start_us,event.duration_us,pts,asset.duration_us)
        if start != event.source_start_us:
            alignment.append({"event_id":event.event_id,"old_start_us":event.source_start_us,"start_us":start,
                "shift_us":start-event.source_start_us,"speed":1,"requires_actual_output_review":True})
            # Only ordinary displays get a new window, which is subsequently
            # observed from the actual output. No action/result protection is dropped.
            videos[j]=replace(event,source_start_us=start,protected_intervals=())
    vision.save(root / "display_frame_alignment.json",alignment)
    occupied=[]
    for event in videos:
        i=int(event.sentence_ids[0].split("-")[-1])
        if selected[i] is None:
            continue
        left,right=event.source_start_us,event.source_start_us+event.source_duration_us
        if any(event.asset_id == old[0] and max(left,old[1]) < min(right,old[2]) for old in occupied):
            raise ValueError("PURPOSE_ALIGNED_SOURCE_REPLAY")
        occupied.append((event.asset_id,left,right))
    bgm=music or prepare_task_bgm(manifest.get("bgm") or {"auto":True},script=manifest["full_script"],task_id=manifest["task_id"],
        batch_id=manifest["task_id"],state_dir=root.parent / "bgm_selection",output_dir=root / "bgm",duration_us=duration,
        ffmpeg=ffmpeg,ffprobe=ffprobe)
    vision.save(root / "music_receipt.json",{**bgm,"reused_for_fixed_audio":bool(music)})
    assets.append(Asset("bgm",bgm["path"],"audio",bgm["asset_duration_us"]))
    audio_events.append(AudioEvent("bgm","bgm","bgm",0,duration))
    timeline=EditTimeline(manifest["task_id"],selection.get("revision",1),Canvas(),duration,tuple(assets),tuple(sentences),tuple(videos),
        tuple(audio_events),tuple(subtitles),"preview",tuple(unresolved)).validate()
    timeline.save(root / "timeline.json")
    vision.save(root / "source_provenance.json",provenance)
    render_timeline(timeline,root / "render",width=width,ffmpeg=ffmpeg,ffprobe=ffprobe,allow_real_tail=True)
    check_production(timeline,root / "render",ffmpeg=ffmpeg,ffprobe=ffprobe)
    _binding(manifest,narration,selection)
    if any(w and w["observation"]["source_identity"] != contract.source_identity(w["source"]) for w in selected.values()):
        raise ValueError("PURPOSE_SOURCE_CHANGED_DURING_PRODUCTION")
    return timeline


def repair_selection(manifest, narration, previous, review, timeline, directory):
    """Try an already observed alternative for failed output; never change audio."""
    from copy import deepcopy
    directory=Path(directory)
    if directory.exists():
        raise ValueError("PURPOSE_REPAIR_OUTPUT_EXISTS")
    if review.get("protocol") != vision.OUTPUT_PROTOCOL:
        raise ValueError("PURPOSE_REPAIR_REVIEW_PROTOCOL_CHANGED")
    _binding(manifest,narration,previous,rules_required=True)
    _request_coverage(review.get("rows"),[{"text":s.text} for s in timeline.sentences])
    if review.get("timeline_identity") != timeline_identity(timeline):
        raise ValueError("PURPOSE_REPAIR_REVIEW_STALE")
    result=deepcopy(previous)
    result["revision"]=previous.get("revision",1)+1
    qc={r["index"]:r for r in review["rows"]}
    used=[{"index":r["index"],"source":r["selected"]["source"],"start":r["selected"]["plan"]["source_start_us"],
        "end":r["selected"]["plan"]["source_end_us"]} for r in previous["rows"]
        if r["selected"]]
    changed=[]
    for row in result["rows"]:
        check=qc[row["index"]]
        if check.get("status") == "SAMPLED_SUPPORT":
            continue
        # A missing claim cannot be repaired by silently selecting another shot
        # against the same incomplete checklist.
        if check.get("uncovered_claims"):
            row["repair_status"]="REQUIREMENT_GAP_REMAINS"
            continue
        rejected=set(row.get("rejected_shot_ids",[]))
        if row["selected"]:
            rejected.add(row["selected"]["shot_id"])
        remaining=[old for old in used if old["index"] != row["index"]]
        alternative=purpose_selection.choose([c for c in row["candidates"] if c["shot_id"] not in rejected],remaining)
        row["rejected_shot_ids"]=sorted(rejected)
        if alternative:
            row["selected"]=alternative;changed.append(row["index"])
            used=remaining+[ {"index":row["index"],"source":alternative["source"],"start":alternative["plan"]["source_start_us"],"end":alternative["plan"]["source_end_us"]} ]
            row["repair_status"]="ALTERNATIVE_REQUIRES_OUTPUT_REVIEW"
        else:
            row["repair_status"]="NO_BETTER_REVIEWED_FIT"
    result["changed_indices"]=changed
    result["parent_output_identity"]=review["output_identity"]
    directory.mkdir(parents=True,exist_ok=False)
    vision.save(directory / "selection.json",result)
    return result


def guard_placeholder(timeline, row):
    sid=timeline.sentences[row["index"]-1].sentence_id
    placeholder=any(v.sentence_ids and sid in v.sentence_ids and any(e.get("diagnostic_placeholder") is True
        for e in v.evidence if isinstance(e,dict)) for v in timeline.videos)
    placeholder=placeholder or any(u.get("sentence_id") == sid and "black is diagnostic only" in u.get("reason","")
        for u in timeline.unresolved if isinstance(u,dict))
    if placeholder:
        return {**row,"raw_model_status":row.get("status"),"status":"EVIDENCE_PENDING",
            "structural_blockers":["DIAGNOSTIC_PLACEHOLDER_CANNOT_PROVE_CLAIM"]}
    return row


def picture_window(timeline, sentence):
    from ..domain.timeline import frame_at_us,frame_time_us
    c=timeline.canvas
    first=frame_at_us(sentence.start_us,c.fps_num,c.fps_den)
    end_us=sentence.start_us+sentence.duration_us
    last=timeline.frame_count if end_us == timeline.duration_us else frame_at_us(end_us,c.fps_num,c.fps_den)
    if last <= first:
        raise ValueError("PURPOSE_OUTPUT_SENTENCE_BELOW_FRAME")
    return frame_time_us(first,c.fps_num,c.fps_den),frame_time_us(last,c.fps_num,c.fps_den)


def review_render(manifest, timeline, requests, render_dir, directory, *, workers=6):
    root=Path(directory);path=Path(render_dir) / "preview.mp4"
    if timeline.task_id != manifest["task_id"]:
        raise ValueError("PURPOSE_OUTPUT_TASK_MISMATCH")
    _request_coverage(requests,[{"text":s.text} for s in timeline.sentences])
    if check_production(timeline,render_dir)["structure_media"]["status"] != "PASS":
        raise ValueError("PURPOSE_OUTPUT_RENDER_IDENTITY_MISMATCH")
    before=contract.source_identity(path)
    mapping={i:s for i,s in enumerate(timeline.sentences,1)}
    def prep(request):
        s=mapping[request["index"]]
        first,last=picture_window(timeline,s)
        shot={"shot_id":s.sentence_id,"video":str(path),"source_start":first/1e6,"source_end":last/1e6}
        return request,shot,ai_editor._review_media(shot,root,dense=True,fit=True)
    def call(job):
        request,shot,media=job
        return vision.review_output(manifest,request,shot,media,root)
    rows=[]
    for request,row in _parallel(requests,prep,call,workers=workers):
        row.setdefault("index",request["index"]);row["text"]=request["text"]
        rows.append(guard_placeholder(timeline,row))
        vision.save(root / "checkpoint.json",rows)
    result={"protocol":vision.OUTPUT_PROTOCOL,"timeline_identity":timeline_identity(timeline),"output_identity":contract.source_identity(path),
        "rows":sorted(rows,key=lambda r:r["index"]),"formal_delivery_allowed":False,
        "counts":dict(Counter(r.get("status","EVIDENCE_UNKNOWN") for r in rows))}
    if before != result["output_identity"] or any(r.get("output_identity",before) != before for r in rows):
        raise ValueError("PURPOSE_OUTPUT_CHANGED")
    vision.save(root / "review.json",result)
    return result
