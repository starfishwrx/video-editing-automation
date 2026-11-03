"""Observe readability before seeing narration or a semantic match verdict."""
import json
from pathlib import Path

from .. import ai_editor_contract as identity
from ..selection import ai_editor
from ..selection.shot_usability import assess


def observe(shot,media,directory):
    context={'sample_times_us':[round(t*1e6) for t in media['times']],
        'source_window_us':{'start':round(media['start']*1e6),'end':round(media['end']*1e6)}}
    prompt='''只看这些带时间的真实画面，客观评估一段商品广告素材的可用性。
你不接收口播、商品卖点、旧标签或期望结论。不要替文案寻找理由，不猜性能。
逐段区分清楚展示、完成的演示、整理/准备，只评价可读性，不要求性能实验。
手持成品、缓慢转向镜头、轻触局部而轮廓/面层仍可读，属于正常展示；不能因为有手或无实验结果就判成准备。
稳定展示不必包含拿起和放下，demonstration_complete=false不否定stable_display。
折拢成团、摆弄边角且主体大面积被手遮住、末尾仍未恢复清楚形态，才记录handling_or_setup或演示未完成。
clear_intervals_us记录产品形态/面层/边缘或具体信息看清的区间；少量手指遮挡不影响阅读时仍可计入，
大面积遮挡、准备和收拢过程中辨认不清主体的区间不计。只描述看见的形状和变化，不推断功能。
只输出JSON {"view_kind":"stable_display|complete_demonstration|handling_or_setup|uncertain",
"subject_visibility":"clear|partial|blocked|uncertain","demonstration_complete":false,
"information_point":"实际看清了什么、没看清什么",
"clear_intervals_us":[{"start":0,"end":1000000,"observed":"这段可读信息"}],
"limitations":"遮挡、未完成或采样限制"}。时间使用源绝对微秒，不虚构图外过程。
'''+json.dumps(context,ensure_ascii=False)
    key=identity.digest(['blind-shot-readability-v2',context,media['before'],identity.file_sha256(media['sheet'])])
    row=ai_editor._call(prompt,Path(directory)/'quality_calls'/(key+'.json'),Path(media['sheet']))
    assess(row,{},context['source_window_us'],check_relation=False)
    if identity.source_identity(shot['video'])!=media['before']:raise ValueError('SHOT_QUALITY_SOURCE_CHANGED')
    return row
