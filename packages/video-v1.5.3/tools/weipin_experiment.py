"""Explicit same-experiment validation across source files; no business writes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "doubao-jianying-orchestrator"))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage",choices=("roles","observe","pair","motion","phase-template","apply","optimize","detail"))
    p.add_argument("--manifest",type=Path,required=True)
    for name in ("timeline","source","report-dir","output","roles","evidence","motion","phase-spec","first-evidence","second-evidence","prior-origins","render-dir","font"):
        p.add_argument("--"+name,type=Path)
    p.add_argument("--first-window",type=int,nargs=2);p.add_argument("--second-window",type=int,nargs=2)
    p.add_argument("--ffmpeg",default="ffmpeg");p.add_argument("--ffprobe",default="ffprobe")
    p.add_argument('--sentence-id',action='append');p.add_argument('--width',type=int,default=540)
    p.add_argument('--no-render',action='store_true')
    a=p.parse_args(argv)
    required={"roles":("timeline","output"),"observe":("source","report_dir","font"),
              "apply":("timeline","roles","evidence","prior_origins","output"),
              "pair":("first_evidence","second_evidence","first_window","second_window","output","font"),
              "motion":("evidence","output"),
              "optimize":("timeline","roles","evidence","prior_origins","report_dir"),
              "phase-template":("evidence","output"),
              "detail":("evidence","output","font")}[a.stage]
    if any(getattr(a,k) is None for k in required):p.error("missing required stage arguments: "+", ".join(required))
    if a.stage=='optimize'and (a.motion is None)==(a.phase_spec is None):
        p.error('optimize requires exactly one of --motion or --phase-spec')
    from orchestrator import ai_editor_contract as identity
    from orchestrator.domain.timeline import EditTimeline
    from orchestrator.media.temporal_samples import prepare,guard_paths
    from orchestrator.vision.experiment_review import classify_sentences,observe
    from orchestrator.selection.experiment_contract import candidate_groups,contract_for
    from orchestrator.vision.purpose_review import save
    def read(path):return json.loads(path.read_text(encoding="utf-8-sig"))
    manifest=read(a.manifest)
    inputs=[v for k,v in vars(a).items() if isinstance(v,Path)and k in {"manifest","timeline","source","roles","evidence","motion","phase_spec","first_evidence","second_evidence","prior_origins","font"}]
    if a.output:
        guard_paths(inputs,a.output,(".prompt.txt",".request.json",".evidence.json",".detail.json",".pair.json",".jpg",".binding.json",".motion.json"))
        if a.stage=='motion':
            for part in ('A','B'):
                guard_paths(inputs,a.output.with_name(a.output.stem+'-'+part+'.json'),
                    ('.input.json','.call.json','.video','.window.mp4','.candidate.json'))
        if a.output.exists():raise ValueError("EXPERIMENT_OUTPUT_ALREADY_EXISTS")
    if a.stage=='phase-template':
        from orchestrator.application.experiment_optimization import phase_spec_template
        save(a.output,phase_spec_template(manifest,read(a.evidence),read(a.motion)if a.motion else None))
        result={'status':'PHASE_REVIEW_TEMPLATE_CREATED','output':str(a.output)}
    elif a.stage=='optimize':
        from orchestrator.application.experiment_optimization import optimize
        result=optimize(manifest,EditTimeline.load(a.timeline),read(a.roles),read(a.evidence),
            read(a.prior_origins),a.report_dir,phase_spec=read(a.phase_spec)if a.phase_spec else None,
            motion=read(a.motion)if a.motion else None,sentence_ids=a.sentence_id,
            render=not a.no_render,ffmpeg=a.ffmpeg,ffprobe=a.ffprobe,width=a.width)
    elif a.stage=="roles":
        timeline=EditTimeline.load(a.timeline)
        if timeline.task_id!=manifest["task_id"]:raise ValueError("EXPERIMENT_TASK_MISMATCH")
        rows=classify_sentences(manifest,timeline,a.output)
        artifact={"task_id":manifest["task_id"],"rules":identity.rules_identity(manifest),"sentences":rows}
        save(a.output,artifact)
        result={"status":"ROLES_CLASSIFIED","groups":candidate_groups(timeline,rows,contract_for(manifest))}
    elif a.stage=="observe":
        # Scope rejection happens before normalization or any paid request.
        if str(a.source.resolve())not in {str(Path(s).resolve())for s in manifest["material_sources"]}:
            raise ValueError("EXPERIMENT_SOURCE_OUTSIDE_TASK")
        a.report_dir.mkdir(parents=True,exist_ok=False)
        samples=prepare(a.source,a.report_dir/"samples",ffmpeg=a.ffmpeg,ffprobe=a.ffprobe,font_path=a.font)
        evidence=observe(manifest,a.source,samples,a.report_dir/"observation.json")
        result={"status":"OBSERVED_FOR_REVIEW","verdict":evidence["observation"]["verdict"],"missing_phases":evidence["missing_phases"]}
    elif a.stage=="pair":
        from orchestrator.vision.experiment_pair_review import review as review_pair
        first,second=read(a.first_evidence),read(a.second_evidence)
        if any(e["task_id"]!=manifest["task_id"]for e in(first,second)):
            raise ValueError("EXPERIMENT_TASK_MISMATCH")
        paired=review_pair(manifest,first["samples"],second["samples"],a.first_window,a.second_window,a.output,
            ffmpeg=a.ffmpeg,ffprobe=a.ffprobe,font_path=a.font)
        result={"status":"EXPERIMENT_PAIR_REVIEWED","verdict":paired["observation"]["verdict"],
                "adjacency_is_acceptance":False}
    elif a.stage=="motion":
        from orchestrator.vision.experiment_motion_review import review
        result={"status":"EXPERIMENT_PROCESS_OBSERVED","protocol":review(manifest,read(a.evidence),a.output,
            ffmpeg=a.ffmpeg,ffprobe=a.ffprobe)['protocol']}
    elif a.stage=="detail":
        from orchestrator.vision.experiment_detail import review
        evidence=read(a.evidence)
        if evidence["task_id"]!=manifest["task_id"]:raise ValueError("EXPERIMENT_TASK_MISMATCH")
        review(evidence["samples"],a.output,ffmpeg=a.ffmpeg,ffprobe=a.ffprobe,font_path=a.font)
        result={"status":"PARTIAL_FACTS_REOBSERVED","complete_experiment_accepted":False}
    else:
        from orchestrator.application.experiment_production import apply
        roles=read(a.roles)
        if roles.get("task_id")!=manifest["task_id"]or roles.get("rules")!=identity.rules_identity(manifest):
            raise ValueError("EXPERIMENT_ROLE_RULE_IDENTITY_CHANGED")
        timeline=EditTimeline.load(a.timeline);groups=candidate_groups(timeline,roles["sentences"],contract_for(manifest))
        if len(groups)!=1:raise ValueError("EXPERIMENT_REQUIRES_ONE_UNAMBIGUOUS_GROUP")
        updated=apply(manifest,timeline,groups[0],roles["sentences"],read(a.evidence),group_id="same-experiment",
                      prior_origins=read(a.prior_origins),ffprobe=a.ffprobe,motion=read(a.motion)if a.motion else None)
        updated.save(a.output)
        if a.render_dir:
            from orchestrator.media.timeline_render import render_timeline
            from orchestrator.quality.production_qc import check_production
            render_timeline(updated,a.render_dir,ffmpeg=a.ffmpeg,ffprobe=a.ffprobe,width=540,allow_real_tail=True)
            check_production(updated,a.render_dir,ffmpeg=a.ffmpeg,ffprobe=a.ffprobe)
        result={"status":"ASSEMBLED_OUTPUT_REVIEW_PENDING","revision":updated.revision}
    print(json.dumps({**result,"formal_delivery_allowed":False},ensure_ascii=False))
    return 0


if __name__=="__main__":raise SystemExit(main())
