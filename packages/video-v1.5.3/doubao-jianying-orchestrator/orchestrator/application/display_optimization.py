"""Bounded re-selection of one fixed display clause using actual-window evidence."""
from pathlib import Path

from .. import ai_editor_contract as identity
from ..domain.timeline import Asset
from ..media.probe import probe_media
from ..media.video_encode import run_h264_encode
from ..media.timeline_render import render_timeline
from ..quality.production_qc import check_production
from ..selection import ai_editor,purpose_selection,purpose_retrieval
from ..selection.shot_usability import is_display
from ..selection.temporal_groups import replace_group
from ..selection.timing_contract import split_sentences
from ..vision import purpose_review as vision
from ..vision.display_compare import compare
from .experiment_production import _prior_origins


def _fixed_request(manifest,timeline,sentence_id,request):
    timeline.validate()
    if (timeline.delivery_mode!='preview' or timeline.task_id!=manifest['task_id']
            or [s.text for s in timeline.sentences]!=[s['text'] for s in split_sentences(manifest)]):
        raise ValueError('DISPLAY_FIXED_TIMELINE_MISMATCH')
    selected=[(i,s) for i,s in enumerate(timeline.sentences,1) if s.sentence_id==sentence_id]
    if len(selected)!=1:raise ValueError('DISPLAY_SENTENCE_ID_REQUIRED')
    index,sentence=selected[0]
    if request.get('index')!=index or request.get('text')!=sentence.text or not is_display(request):
        raise ValueError('DISPLAY_REQUEST_IDENTITY_OR_TYPE')
    return sentence,{**request,'segment_us':sentence.duration_us}


def refine_from_index(manifest,timeline,sentence_id,request,analysis,index,prior_origins,directory,**options):
    """Find the candidates without a hand-authored shortlist; preserve fixed audio."""
    from ..vision.purpose_inventory import recall
    sentence,request=_fixed_request(manifest,timeline,sentence_id,request)
    allowed={str(Path(p).resolve()) for p in manifest['material_sources']}
    _prior_origins(timeline,prior_origins,allowed,sentence.start_us,sentence.start_us+sentence.duration_us)
    root=Path(directory).resolve();root.mkdir(parents=True,exist_ok=False)
    try:
        rankings=recall(manifest,[request],analysis['shots'],index,root/'retrieval')
        ranking=purpose_retrieval.allocate([request],rankings,
            purpose_retrieval.catalog(analysis['shots'],index['scans']))[request['index']]
        candidates=purpose_retrieval.windows(ranking,analysis['shots'])
        if not candidates:
            result={'status':'BLOCKED','reason':'NO_PURPOSE_RETRIEVAL_HINT',
                'formal_delivery_allowed':False,'not_found_means':'bounded recall; not a library-wide gap'}
            vision.save(root/'result.json',result);return result
        result=refine(manifest,timeline,sentence_id,request,candidates,prior_origins,root/'selection',refill=True,**options)
    except (ValueError,TypeError,KeyError,OSError,RuntimeError) as error:
        vision.save(root/'result.json',{'status':'BLOCKED','error':str(error),'formal_delivery_allowed':False})
        raise
    vision.save(root/'result.json',result)
    return result


def refine(manifest,timeline,sentence_id,request,candidates,prior_origins,directory,*,
           render=True,ffmpeg='ffmpeg',ffprobe='ffprobe',width=540,refill=False):
    sentence,request=_fixed_request(manifest,timeline,sentence_id,request)
    allowed={str(Path(p).resolve()) for p in manifest['material_sources']}
    if (not isinstance(candidates,list) or not 1<=len(candidates)<=(6 if refill else 3)
            or len({c['shot_id'] for c in candidates})!=len(candidates)
            or any(str(Path(c['video']).resolve()) not in allowed for c in candidates)):
        raise ValueError('DISPLAY_CANDIDATE_SCOPE')
    lo,hi=sentence.start_us,sentence.start_us+sentence.duration_us
    origins=_prior_origins(timeline,prior_origins,allowed,lo,hi)
    used=[{'source':origins[v.asset_id],'start':v.source_start_us,'end':v.source_start_us+v.source_duration_us}
        for v in timeline.videos if v.target_start_us>=hi or v.target_start_us+v.duration_us<=lo]
    root=Path(directory).resolve();root.mkdir(parents=True,exist_ok=False)
    result={'status':'BLOCKED','formal_delivery_allowed':False,'task_id':manifest['task_id'],'sentence_id':sentence_id}
    try:
        rows=[]
        for number,shot in enumerate(candidates):
            if number==3:
                if purpose_selection.eligible(rows,used):break
                result['refill_reason']='NO_USABLE_UNREPEATED_FIRST_WAVE'
            item={'shot_id':shot['shot_id'],'source':str(Path(shot['video']).resolve()),
                'retrieval_hint':shot.get('retrieval_hint')}
            start,end=round(shot['source_start']*1e6),round(shot['source_end']*1e6)
            if end-start<=sentence.duration_us and any(item['source']==u['source']
                    and max(start,u['start'])<min(end,u['end']) for u in used):
                item['plan']={'ok':False,'reason':'SOURCE_REPLAY_WITH_RETAINED_SENTENCE'}
                rows.append(item);vision.save(root/'candidates.json',rows);continue
            try:
                media=ai_editor._review_media(shot,root/'media',dense=True,fit=True)
                observed=vision.review(manifest,request,shot,media,root/'observations')
                plan=purpose_selection.fit(observed,shot,request)
                item.update(observation=observed,plan=plan)
            except (ValueError,TypeError,KeyError,OSError,RuntimeError) as error:
                item.update(error=str(error),plan={'ok':False,'reason':'DISPLAY_REVIEW_UNKNOWN'})
            rows.append(item);vision.save(root/'candidates.json',rows)
        available=purpose_selection.eligible(rows,used)
        if len(available)>=2:result['comparison']=compare(manifest,request,available,root/'comparison')
        winner=purpose_selection.choose(rows,used);vision.save(root/'candidates.json',rows)
        if winner is None:
            result.update(reason='NO_USABLE_UNREPEATED_DISPLAY_WINDOW',candidates=rows)
            vision.save(root/'result.json',result);return result
        source=winner['source'];before=identity.source_identity(source)
        if before!=winner['observation']['source_identity']:raise ValueError('DISPLAY_REVIEW_SOURCE_CHANGED')
        proxy=root/'selected-normalized.mp4'
        height=round(width*timeline.canvas.height/timeline.canvas.width/2)*2
        run_h264_encode(ffmpeg,['-v','error','-nostdin','-i',source,'-an','-vf',
            f'fps=60:eof_action=pass,scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2',
            '-r','60','-fps_mode','cfr','-video_track_timescale','60000'],proxy,
            trailing=['-pix_fmt','yuv420p','-metadata:s:v:0','rotate=0'],timeout=300)
        if identity.source_identity(source)!=before:raise ValueError('DISPLAY_SOURCE_CHANGED')
        revision=timeline.revision+1;group_id=f'display-optimization-r{revision}'
        while any(a.asset_id==group_id+'-source' for a in timeline.assets):
            revision+=1;group_id=f'display-optimization-r{revision}'
        facts=probe_media(proxy,ffprobe=ffprobe)
        asset=Asset(group_id+'-source',str(proxy),'video',facts['duration_us'],facts['width'],facts['height'],facts['fps_num'],facts['fps_den'])
        origins[asset.asset_id]=source;clips=[]
        for part in winner['plan']['parts']:
            a,b=part['source_start_us'],part['source_end_us']
            protected=[{'start':a,'end':b,'kind':'action','min_output_frames':1}] if part['locked'] else []
            clips.append({'asset_id':asset.asset_id,'source_start_us':a,'source_end_us':b,
                'target_duration_us':part['segment_us'],'protected_intervals':protected,
                'evidence':[{'protocol':vision.PROTOCOL,'original':before,'observation':winner['observation'],
                    'usability':winner['plan']['usability'],'status':'OUTPUT_REVIEW_PENDING'}]
                    +([] if part['locked'] else [{'bounded_process_speed_cap':3}])})
        updated=replace_group(timeline,(sentence_id,),clips,group_id=group_id,assets=(asset,),
            original_paths=origins,group_kind='montage')
        value=winner['plan'].get('selling_point_value') or {}
        outside=lambda t:tuple(v for v in t.videos if v.target_start_us>=hi or v.target_start_us+v.duration_us<=lo)
        preserved={'sentences':updated.sentences==timeline.sentences,'audio':updated.audios==timeline.audios,
            'subtitles':updated.subtitles==timeline.subtitles,'outside_sentence_video':outside(updated)==outside(timeline)}
        if not all(preserved.values()):raise ValueError('DISPLAY_FIXED_INPUT_CHANGED')
        updated.save(root/'timeline.json')
        result.update(status='PLANNED_OUTPUT_REVIEW_PENDING',request=request,selected=winner,preserved=preserved,
            selling_point_value=value,
            timeline=str(root/'timeline.json'),candidates=rows)
        if render:
            receipt=render_timeline(updated,root/'render',ffmpeg=ffmpeg,ffprobe=ffprobe,width=width,allow_real_tail=True)
            qc=check_production(updated,root/'render',ffmpeg=ffmpeg,ffprobe=ffprobe)
            if qc['structure_media']['status']!='PASS':raise ValueError('DISPLAY_RENDER_STRUCTURE_FAILED')
            result.update(status='RENDERED_OUTPUT_REVIEW_PENDING',video=receipt['video'],qc=qc)
        vision.save(root/'result.json',result);return result
    except (ValueError,TypeError,KeyError,OSError,RuntimeError) as error:
        vision.save(root/'result.json',{**result,'status':'BLOCKED','error':str(error)});raise
