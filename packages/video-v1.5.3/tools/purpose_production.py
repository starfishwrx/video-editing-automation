"""Explicit task-directed recognition and selection. No queue, draft, NAS or table writes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1] / "doubao-jianying-orchestrator"))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("stage",choices=("inspect","select","render","review","repair"))
    for name in ("manifest","analysis","narration","report-dir"):
        p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--workers",type=int,default=6,choices=range(1,9))
    p.add_argument("--candidate-limit",type=int,default=3,choices=(1,2,3))
    p.add_argument("--ffmpeg",default="ffmpeg")
    p.add_argument("--ffprobe",default="ffprobe")
    p.add_argument("--width",type=int,default=540)
    p.add_argument("--round",type=int,default=2,choices=(2,3))
    p.add_argument("--music-receipt",type=Path,help="复用同任务已准备音乐，保证对照音轨一致")
    p.add_argument("--review-dir",type=Path,help="新的独立输出复核目录")
    a=p.parse_args(argv)
    from orchestrator.application import purpose_production as app
    from orchestrator.domain.timeline import EditTimeline
    def read(path):
        return json.loads(path.read_text(encoding="utf-8-sig"))
    manifest,analysis,narration=read(a.manifest),read(a.analysis),read(a.narration)
    root=a.report_dir.resolve()
    if a.stage == "inspect":
        result=app.inspect(manifest,analysis,narration,root / "inspect",workers=a.workers)
        unknown=sum(bool(r.get("error")) for r in result["scans"].values())
        print(json.dumps({"status":"SEARCH_INDEX_PARTIAL" if unknown else "SEARCH_INDEX_READY",
            "scanned":len(result["scans"]),"unknown":unknown,"formal_delivery_allowed":False}))
    elif a.stage == "select":
        result=app.select(manifest,analysis,narration,read(root / "inspect/index.json"),root / "selection",
            workers=a.workers,candidate_limit=a.candidate_limit)
        print(json.dumps({"status":"SELECTED_FOR_REVIEW","sentences":len(result["rows"]),
            "selected":sum(r["selected"] is not None for r in result["rows"]),
            "experiment_pair_candidates":len(result.get("experiment_pair_candidates",[])),"formal_delivery_allowed":False}))
    elif a.stage == "render":
        timeline=app.materialize(manifest,narration,read(root / "selection/selection.json"),root / "production",
            ffmpeg=a.ffmpeg,ffprobe=a.ffprobe,width=a.width,music=read(a.music_receipt) if a.music_receipt else None)
        print(json.dumps({"status":"REVIEW_RENDERED","duration_us":timeline.duration_us,"formal_delivery_allowed":False}))
    elif a.stage == "review":
        result=app.review_render(manifest,EditTimeline.load(root / "production/timeline.json"),
            read(root / "selection/selection.json")["requests"],root / "production/render",a.review_dir or root / "output_review",workers=a.workers)
        print(json.dumps({"status":"SAMPLED_REVIEW_COMPLETE","counts":result["counts"],"formal_delivery_allowed":False}))
    else:
        previous_root=root if a.round == 2 else root / "repair2"
        selection_path=previous_root / ("selection/selection.json" if a.round == 2 else "selection.json")
        timeline=EditTimeline.load(previous_root / "production/timeline.json")
        review=read((a.review_dir or previous_root / "output_review") / "review.json")
        from orchestrator import ai_editor_contract as contract
        if review["output_identity"] != contract.source_identity(previous_root / "production/render/preview.mp4"):
            raise ValueError("PURPOSE_REPAIR_OUTPUT_CHANGED")
        target=root / f"repair{a.round}"
        result=app.repair_selection(manifest,narration,read(selection_path),review,timeline,target)
        if result["changed_indices"]:
            music_path=a.music_receipt or root / "production/music_receipt.json"
            if not music_path.exists():
                music_path=root / "production/bgm/bgm_receipt.json"
            timeline=app.materialize(manifest,narration,result,target / "production",ffmpeg=a.ffmpeg,ffprobe=a.ffprobe,
                width=a.width,music=read(music_path))
            app.review_render(manifest,timeline,result["requests"],target / "production/render",target / "output_review",workers=a.workers)
        print(json.dumps({"status":"REPAIR_REVIEWED" if result["changed_indices"] else "NO_REVIEWED_ALTERNATIVE",
            "changed_indices":result["changed_indices"],"formal_delivery_allowed":False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
