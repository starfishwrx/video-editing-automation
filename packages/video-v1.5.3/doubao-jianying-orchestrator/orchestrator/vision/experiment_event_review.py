"""Observe event facts in numbered video, then refine boundaries against actual source frames."""
import json
from pathlib import Path

from .. import ai_editor_contract as identity
from ..media import action_frames
from ..media.temporal_samples import guard_outputs,guard_paths,validate as validate_samples
from ..selection import ai_editor
from ..selection.experiment_events import compile_events
from .experiment_motion_review import PROTOCOL,prepare_review,window_video
from .purpose_review import policy,save
from . import temporal_video


def _frame_sheet(member, ids, target, *, ffmpeg, ffprobe, font_path):
    from PIL import Image, ImageDraw, ImageFont
    metadata=action_frames.probe(member['samples']['source']['path'],target.parent,ffprobe=ffprobe)
    frames=action_frames.extract(member['samples']['source']['path'],metadata,
        [member['samples']['frames'][i]['source_frame'] for i in ids],target.parent,ffmpeg=ffmpeg,width=270)
    canvas=Image.new('RGB',(1620,((len(ids)+5)//6)*510),'#222');draw=ImageDraw.Draw(canvas)
    font=ImageFont.truetype(str(font_path),20)
    for n,(index,frame) in enumerate(zip(ids,frames)):
        im=Image.open(frame['path']).convert('RGB');im.thumbnail((270,480));x=n%6*270;y=n//6*510
        canvas.paste(im,(x,y+30));draw.text((x+5,y+4),f'{member["part"]} frame {index}',font=font,fill='white')
    sheet=target.with_suffix('.jpg');canvas.save(sheet,quality=95)
    return sheet


def review(manifest,pair,output,*,focus='complete_pour',ffmpeg='ffmpeg',ffprobe='ffprobe',font_path,
           frames_only=False):
    if focus not in {'complete_pour','absorption_focus'}:raise ValueError('EXPERIMENT_EVENTS_FOCUS_INVALID')
    output=Path(output);binding=prepare_review(manifest,pair,output)
    for part in ('A','B'):
        guard_paths([pair['sheet']['path'],pair['input_binding']['file']['path']],
            output.with_name(output.stem+'-'+part+'-detail.json'),
            ('.jpg','.prompt.txt','.request.json','.facts.json','.candidate.json'))
    # Validate every new destination against both members before making media.
    for samples in [m['samples'] for m in pair['members']]:
        for part in ('A','B'):
            guard_outputs(samples,output.with_name(output.stem+'-'+part+'-detail.json'),
                ('.jpg','.prompt.txt','.request.json','.facts.json','.candidate.json'))
    observations=[];facts=[]
    for member in pair['members']:
        target=output.with_name(output.stem+'-'+member['part']+'.json')
        window=None if frames_only else window_video(member,target,ffmpeg=ffmpeg,ffprobe=ffprobe)
        coverage=focus if member['part']=='A' else 'complete_verification'
        prompt=policy(manifest)+'''只报告实际视频中的事件事实和编号边界，不判断倍速，不接收目标句长。
A按顺序分为pour（液体实际流入）和absorb（倒液结束后吸收过程及最终可见状态）。
B按顺序分为press（一次完整验证中必要的按压）、lift（提起和翻转）、result（纸巾已可读）。
B必须另外明确contact_first_frame（纸巾第一次实际接触巾体，不是手靠近）、release_first_frame
（最后一次按压结束开始提起，不是等纸巾完全拿走）、result_first_frame（翻转后已可读）。
这三个编号必须分别等于press、lift、result的首帧。若输入开始有静置或手靠近，先标context再press，
不能把没有纸巾接触的准备画面塞入press。context只允许B的首段，可在有效窗口前直接去掉。
每段首尾为包含的整数frame编号，下段首帧必须等于上段尾帧加1；不能臆造不可见的液体消失。
complete_pour须保留倒液的开始和完成；absorption_focus允许从实际仍在倒液的尾段开始，
优先保留液体流入、随后吸收的状态变化和结果，不要求杯子进入和完整倒液全程。
该取法只用于定性吸收卖点，不能声称完整倒液或真实吸收耗时。不要选只有静态蓝色的窗口。
两种A覆盖都需实际pour和absorb。B保留纸巾接触、完成按压、提起翻转及清楚结果，
不强保留多余准备；result从翻转完成可读处开始，通常保留0.5到1.1秒。
不把蓝色巾体或白色纸巾外观外推绝对吸干或完全没有水。看不清则uncertain。
只输出JSON {"verdict":"observed|uncertain","coverage":"指定覆盖类型",
"effective_window_frame_ids":[0,1],"contact_first_frame":0,"release_first_frame":1,"result_first_frame":2,
"events":[{"kind":"pour|absorb|context|press|lift|result",
"first_frame":0,"last_frame":1,"observed":"实际可见事实"}],"limitations":"观察边界"}。
'''+json.dumps({'part':member['part'],'coverage':coverage,'window_frame_ids':member['window_frame_ids'],
            'input_binding_sha256':identity.digest(binding)},ensure_ascii=False)
        def validate(row):
            save(target.with_suffix('.candidate.json'),row)
            if row.get('coverage')!=coverage:raise ValueError('EXPERIMENT_EVENTS_COVERAGE_CHANGED')
            return bool(compile_events(row,member))
        if frames_only:
            left,right=member['window_frame_ids']
            anchors={round(left+i*(right-left)/15) for i in range(16)}
            shown=sorted({max(left,min(right,i+d)) for i in anchors for d in (-1,0,1)})
            sheet=_frame_sheet(member,shown,target,ffmpeg=ffmpeg,ffprobe=ffprobe,font_path=font_path)
            row=ai_editor._call(prompt+'\n本次为实际顺序源帧，未提供连续视频；边界只使用已展示编号。'+
                json.dumps({'shown_frame_ids':shown}),target,sheet)
            validate(row)
            if any(e[k] not in shown for e in row['events'] for k in ('first_frame','last_frame')):
                raise ValueError('EXPERIMENT_EVENTS_BOUNDARY_NOT_SHOWN')
        else:
            row=temporal_video.review(window,prompt,target,fps=10,validator=validate)
        # Only uncertain boundaries and a few intervening context frames need
        # dense source-image refinement; the video supplies the motion context.
        left,right=member['window_frame_ids'];ids=set()
        for event in row['events']:
            for at in (event['first_frame'],event['last_frame']):
                ids.update(range(max(left,at-2),min(right,at+2)+1))
        a,b=row['effective_window_frame_ids'];ids.update(round(a+i*(b-a)/7) for i in range(8))
        if b-a<=40:ids.update(range(a,b+1))
        ids=sorted(ids)
        target=output.with_name(output.stem+'-'+member['part']+'-detail.json')
        sheet=_frame_sheet(member,ids,target,ffmpeg=ffmpeg,ffprobe=ffprobe,font_path=font_path)
        detail_prompt=prompt+'\n依据本次实际编号帧校正以下视频事件边界。边界必须使用本图实际展示的编号；中间状态不能从旧描述推断。'+json.dumps(
            {'video_observation':row,'shown_frame_ids':ids},ensure_ascii=False)
        refined=ai_editor._call(detail_prompt,target,sheet)
        validate(refined)
        if any(e[k] not in ids for e in refined['events'] for k in ('first_frame','last_frame')):
            raise ValueError('EXPERIMENT_EVENTS_BOUNDARY_NOT_SHOWN')
        validate_samples(member['samples']);observations.append(compile_events(refined,member))
        facts.append({'video':row,'frame_refinement':refined,'shown_frame_ids':ids,'sheet':identity.source_identity(sheet)})
    artifact={'protocol':PROTOCOL,'task_id':manifest['task_id'],'rules':identity.rules_identity(manifest),
        'pair_input_sha256':identity.digest(binding),'method':('actual_frame_events_with_dense_refinement' if frames_only
            else 'qwen_video_events_with_actual_frame_refinement'),
        'observations':observations,'event_observations':facts,'formal_delivery_allowed':False}
    save(output.with_suffix('.motion.json'),artifact)
    return artifact
