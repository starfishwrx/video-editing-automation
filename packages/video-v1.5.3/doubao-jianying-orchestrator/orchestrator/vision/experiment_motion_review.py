"""Observe process speedability in actual numbered videos, without a target duration."""
import json
from pathlib import Path

from .. import ai_editor_contract as identity
from ..media.temporal_samples import validate as validate_samples,guard_outputs
from ..media.video_encode import run_h264_encode
from ..media.action_frames import probe
from ..selection.experiment_retiming import validate_motion
from .experiment_pair_review import input_binding,PROTOCOL as PAIR_PROTOCOL
from .purpose_review import policy,save
from . import temporal_video

PROTOCOL='weipin-experiment-local-speed-v1'


def review(manifest,pair,output,*,ffmpeg='ffmpeg',ffprobe='ffprobe'):
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
    # Check every destination against both sources before creating any media.
    for member in pair['members']:
        validate_samples(member['samples'])
        guard_outputs(member['samples'],output,('.motion.json',))
        for target in targets:
            guard_outputs(member['samples'],target,('.input.json','.call.json','.video','.window.mp4','.candidate.json'))
    observations=[]
    for member in pair['members']:
        samples=validate_samples(member['samples']);target=output.with_name(output.stem+'-'+member['part']+'.json')
        guard_outputs(samples,target,('.input.json','.call.json','.video','.window.mp4','.candidate.json'))
        guard_outputs(samples,output,('.motion.json',))
        # Submit only the already paired window, retaining its printed global
        # frame IDs. Long unrelated tails obscure these short action boundaries.
        a,b=member['window_frame_ids'];window=target.with_suffix('.window.mp4')
        target.parent.mkdir(parents=True,exist_ok=True)
        run_h264_encode(ffmpeg,['-v','error','-nostdin','-i',samples['video']['path'],'-an','-vf',
            f'trim=start_frame={a}:end_frame={b+1},setpts=PTS-STARTPTS','-fps_mode','passthrough'],window,
            trailing=['-pix_fmt','yuv420p'],timeout=120)
        if len(probe(window,target.parent,ffprobe=ffprobe)['frames'])!=b-a+1:
            raise ValueError('EXPERIMENT_MOTION_WINDOW_FRAME_COUNT_CHANGED')
        prompt=policy(manifest)+'''请完整观察带frame N编号的真实视频，只分析指定源窗口中的这一轮实验，不接收剪辑目标时长。
A是倒液吸收，B是对应的纸巾按压验证。本次允许过程局部加速，优先不超过2倍、最多3倍。
先找能完整表达该文件阶段的一次紧凑有效窗口：保留动作开始、完成与可读结果，去掉多余等待或重复。
不得为了凑时长裁必要动作；不要把已有液体推断成新倒液。仅根据实际看到的frame编号定位。
把有效窗口无缝分段，first_frame/last_frame均为包含的整数编号，下段从上段last_frame+1开始。
kind只用pour/absorb/press/lift/result/context。pour或press可分开始接触、重复中间过程、完成几个段；
开始接触与完成段can_speed=false，明确可压缩的中间过程才true；lift、result始终false。
特别注意：pour、press各自首段和末段都必须false，不能把整个press一段标成true。
若中间可压缩，则同一kind至少分开始false/中间true/结束false；不能确定分界则全部false。
有效窗口不必等于输入窗口：清楚结果展示后无变化的长尾应去掉；B结果通常保留0.5–0.8秒，
须从纸巾完成翻转后清楚可读时开始，不把准备翻转当结果。A的吸收结果保留看清状态所需区间。
吸收前后可见状态保留原速，不改变真实计时或用加速冒充瞬吸。没有计时定量主张时，可压缩重复倒液中间过程。
A必须有pour和absorb；B必须有press、lift和result。纸巾结果只描述是否有明显湿痕，不外推绝对无水。
只输出JSON {"verdict":"observed|uncertain","effective_window_frame_ids":[0,1],
"segments":[{"first_frame":0,"last_frame":1,"kind":"pour","can_speed":false,"observed":"实际动作及为何可压缩或须原速"}],
"limitations":"信息不足或帧边界不确定性"}。
'''+json.dumps({'part':member['part'],'window_frame_ids':member['window_frame_ids'],
            'input_binding_sha256':identity.digest(binding)},ensure_ascii=False)
        def validate_row(row):
            save(target.with_suffix('.candidate.json'),row)
            return bool(validate_motion(row,member))
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
