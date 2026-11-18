"""Observe process speedability in actual numbered videos, without a target duration."""
import json
from pathlib import Path

from .. import ai_editor_contract as identity
from ..media.temporal_samples import validate as validate_samples,guard_outputs,guard_paths
from ..media.video_encode import run_h264_encode
from ..media.action_frames import probe
from ..selection.experiment_retiming import validate_motion
from .experiment_pair_review import input_binding,PROTOCOL as PAIR_PROTOCOL
from .purpose_review import policy,save
from . import temporal_video

PROTOCOL='weipin-experiment-local-speed-v1'


def prepare_review(manifest,pair,output):
    output=Path(output)
    if (pair.get('rules')!=identity.rules_identity(manifest)or pair.get('task_id')!=manifest['task_id']
            or pair.get('protocol')!=PAIR_PROTOCOL or pair.get('observation',{}).get('verdict')!='matched'
            or pair.get('context')!={'product':manifest['product'],'category':'weipin'}
            or [m.get('part')for m in pair.get('members',[])]!=['A','B']):
        raise ValueError('EXPERIMENT_MOTION_PAIR_INPUT_CHANGED')
    allowed={str(Path(p).resolve())for p in manifest['material_sources']}
    if any(m['samples']['original']['path']not in allowed for m in pair['members']):
        raise ValueError('EXPERIMENT_MOTION_SOURCE_OUTSIDE_TASK')
    binding=input_binding(manifest,pair['members'],pair['sheet']['path'])
    bound_file=pair['input_binding']['file']
    if (identity.source_identity(bound_file['path'])!=bound_file
            or identity.file_sha256(bound_file['path'])!=pair['input_binding']['sha256']
            or json.loads(Path(bound_file['path']).read_text('utf-8'))!=binding):
        raise ValueError('EXPERIMENT_MOTION_PAIR_INPUT_CHANGED')
    targets=[output.with_name(output.stem+'-'+m['part']+'.json')for m in pair['members']]
    evidence_inputs=[pair['sheet']['path'],bound_file['path']]
    guard_paths(evidence_inputs,output,('.motion.json',))
    for target in targets:
        guard_paths(evidence_inputs,target,('.input.json','.call.json','.video','.window.mp4','.candidate.json'))
    # Check every destination against both sources before creating any media.
    for member in pair['members']:
        validate_samples(member['samples'])
        guard_outputs(member['samples'],output,('.motion.json',))
        for target in targets:
            guard_outputs(member['samples'],target,('.input.json','.call.json','.video','.window.mp4','.candidate.json'))
    return binding


def window_video(member,target,*,ffmpeg='ffmpeg',ffprobe='ffprobe'):
    samples=validate_samples(member['samples']);target=Path(target)
    a,b=member['window_frame_ids'];window=target.with_suffix('.window.mp4')
    target.parent.mkdir(parents=True,exist_ok=True)
    run_h264_encode(ffmpeg,['-v','error','-nostdin','-i',samples['video']['path'],'-an','-vf',
        f'trim=start_frame={a}:end_frame={b+1},setpts=PTS-STARTPTS','-fps_mode','passthrough'],window,
        trailing=['-pix_fmt','yuv420p'],timeout=120)
    if len(probe(window,target.parent,ffprobe=ffprobe)['frames'])!=b-a+1:
        raise ValueError('EXPERIMENT_MOTION_WINDOW_FRAME_COUNT_CHANGED')
    return window


def review(manifest,pair,output,*,ffmpeg='ffmpeg',ffprobe='ffprobe'):
    output=Path(output);binding=prepare_review(manifest,pair,output);observations=[]
    for member in pair['members']:
        samples=validate_samples(member['samples']);target=output.with_name(output.stem+'-'+member['part']+'.json')
        guard_outputs(samples,target,('.input.json','.call.json','.video','.window.mp4','.candidate.json'))
        guard_outputs(samples,output,('.motion.json',))
        # Submit only the already paired window, retaining its printed global
        # frame IDs. Long unrelated tails obscure these short action boundaries.
        window=window_video(member,target,ffmpeg=ffmpeg,ffprobe=ffprobe)
        prompt=policy(manifest)+'''请完整观察带frame N编号的真实视频，只分析指定源窗口中的这一轮实验，不接收剪辑目标时长。
A是倒液吸收，B是对应的纸巾按压验证。请标记各事件的源帧事实；成片由三阶段变速规则独立分配时长。
先找能完整表达该文件阶段的一次紧凑有效窗口：保留动作开始、完成与可读结果，去掉多余等待或重复。
不得为了凑时长裁必要动作；不要把已有液体推断成新倒液。仅根据实际看到的frame编号定位。
把有效窗口无缝分段，first_frame/last_frame均为包含的整数编号，下段从上段last_frame+1开始。
kind只用pour/absorb/press/lift/result/context。pour或press可分开始接触、重复中间过程、完成几个段；
can_speed标识单调重复过程。观察记录各事件的起止、事实和疑点，原片发生的动作要完整覆盖。
成片可对三个阶段分别加速。报告纸巾接触、提起、结果首次可辨处；最终吸收状态与纸巾结果须有足够实际帧。
加速后的画面只能说明发生了什么。报告原片计时需要可读计时依据。
A必须有pour和absorb；B必须有press、lift和result。纸巾结果只描述是否有明显湿痕，不外推绝对无水。
只输出JSON {"verdict":"observed|uncertain","effective_window_frame_ids":[0,1],
"segments":[{"first_frame":0,"last_frame":1,"kind":"pour","can_speed":false,"observed":"实际动作及为何可压缩或须原速"}],
"limitations":"信息不足或帧边界不确定性"}。
'''+json.dumps({'part':member['part'],'window_frame_ids':member['window_frame_ids'],
            'input_binding_sha256':identity.digest(binding)},ensure_ascii=False)
        def validate_row(row):
            save(target.with_suffix('.candidate.json'),row)
            return bool(validate_motion(row,member,allow_phase_speedup=True))
        row=temporal_video.review(window,prompt,target,fps=10,validator=validate_row)
        validate_samples(samples);observations.append(row)
    artifact={'protocol':PROTOCOL,'task_id':manifest['task_id'],'rules':identity.rules_identity(manifest),
        'method':'qwen_numbered_video_observation',
        'pair_input_sha256':identity.digest(binding),'observations':observations,'formal_delivery_allowed':False}
    save(output.with_suffix('.motion.json'),artifact)
    return artifact


def validate(manifest,pair,motion):
    if (motion.get('protocol')!=PROTOCOL or motion.get('task_id')!=manifest['task_id']
            or motion.get('rules')!=identity.rules_identity(manifest)
            or motion.get('pair_input_sha256')!=identity.digest(input_binding(manifest,pair['members'],pair['sheet']['path']))):
        raise ValueError('EXPERIMENT_MOTION_INPUT_CHANGED')
    return motion['observations']
