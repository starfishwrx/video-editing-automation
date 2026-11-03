"""Bounded display-shot re-selection with fixed narration and a new preview directory."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'doubao-jianying-orchestrator'))


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('manifest','timeline','request','prior-origins','report-dir'):
        p.add_argument('--'+name,type=Path,required=True)
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--candidates',type=Path,help='At most three explicit source windows')
    mode.add_argument('--analysis',type=Path,help='Existing shot analysis; recall candidates automatically')
    p.add_argument('--index',type=Path,help='Existing purpose-search index; required with --analysis')
    p.add_argument('--sentence-id',required=True);p.add_argument('--no-render',action='store_true')
    p.add_argument('--width',type=int,default=540);p.add_argument('--ffmpeg',default='ffmpeg');p.add_argument('--ffprobe',default='ffprobe')
    a=p.parse_args(argv)
    if bool(a.analysis)!=bool(a.index):p.error('--analysis and --index must be supplied together')
    from orchestrator.application.display_optimization import refine,refine_from_index
    from orchestrator.domain.timeline import EditTimeline
    def read(path):return json.loads(path.read_text('utf-8-sig'))
    args=(read(a.manifest),EditTimeline.load(a.timeline),a.sentence_id,read(a.request))
    options=dict(render=not a.no_render,ffmpeg=a.ffmpeg,ffprobe=a.ffprobe,width=a.width)
    if a.analysis:
        result=refine_from_index(*args,read(a.analysis),read(a.index),read(a.prior_origins),a.report_dir,**options)
    else:
        result=refine(*args,read(a.candidates),read(a.prior_origins),a.report_dir,**options)
    print(json.dumps({k:v for k,v in result.items() if k not in {'candidates','selected'}},ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
