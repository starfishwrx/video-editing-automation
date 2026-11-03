"""Bounded comparison of usable display windows using only their actual pixels."""
import json
from pathlib import Path

from .. import ai_editor_contract as identity
from ..selection import ai_editor
from .purpose_review import policy,USABILITY_PROMPT,save
from .shot_value_review import PROMPT as VALUE_PROMPT


def compare(manifest,request,candidates,directory):
    from PIL import Image,ImageDraw,ImageFont
    if not 2<=len(candidates)<=3:raise ValueError('DISPLAY_COMPARISON_CANDIDATE_LIMIT')
    root=Path(directory);root.mkdir(parents=True,exist_ok=True)
    media=[ai_editor._review_media({'video':c['source'],'source_start':c['plan']['source_start_us']/1e6,
        'source_end':c['plan']['source_end_us']/1e6},root/'media',dense=True,fit=True) for c in candidates]
    sheets=[Path(m['sheet']) for m in media]
    inputs=[{'source':identity.source_identity(c['source']),'sheet_sha256':identity.file_sha256(p),
        'window_us':{'start':c['plan']['source_start_us'],'end':c['plan']['source_end_us']}} for c,p in zip(candidates,sheets)]
    if any(i['source']!=c['observation']['source_identity'] for i,c in zip(inputs,candidates)):
        raise ValueError('DISPLAY_COMPARISON_SOURCE_CHANGED')
    canvas=Image.new('RGB',(720*len(candidates),1400),'#222');draw=ImageDraw.Draw(canvas)
    font=ImageFont.load_default(size=24)
    for index,path in enumerate(sheets):
        with Image.open(path) as source:
            im=source.convert('RGB');im.thumbnail((720,1360));canvas.paste(im,(index*720,40))
        draw.text((index*720+12,8),f'C{index}',font=font,fill='white')
    key=identity.digest([request,inputs])
    sheet=root/(key+'.jpg');canvas.save(sheet,quality=95)
    prompt=VALUE_PROMPT+policy(manifest)+USABILITY_PROMPT+'''比较图中C0/C1/C2窗口，首先按本句卖点信息价值排序。
不接收旧模型结论、置信度、文件名或应当选谁；只看实际画面与本句目标。
优先选动作或展示确有揭示本句特点/结果的片段，清楚但没卖点信息的手部动作不胜过有信息的片段。
稳定真实展示不要求刻意拿起放下；不能把普通展示提升为功能实验。必要时全部拒绝。
每个候选给usable；可用者rank从0开始连续且不重复，不可用者rank=null，所有C编号恰好一次。
输出JSON {"candidates":[{"id":"C0","usable":true,"rank":0,"reason":"实际可见优劣"}]}。
'''+json.dumps({'sentence':request['text'],'checks':request['checks'],
        'display_goal':request.get('display_goal','visible_feature'),'candidate_ids':[f'C{i}' for i in range(len(candidates))]},ensure_ascii=False)
    row=ai_editor._call(prompt,root/(key+'.json'),sheet);items=row.get('candidates')
    if (not isinstance(items,list) or len(items)!=len(candidates) or any(not isinstance(i,dict) for i in items)
            or {i.get('id') for i in items}!={f'C{i}' for i in range(len(candidates))}):
        raise ValueError('DISPLAY_COMPARISON_COVERAGE')
    ranks=[]
    for item in items:
        if type(item.get('usable'))is not bool or not isinstance(item.get('reason'),str) or not item['reason'].strip():
            raise ValueError('DISPLAY_COMPARISON_FACT_REQUIRED')
        if item['usable']:
            if type(item.get('rank'))is not int or item['rank']<0:raise ValueError('DISPLAY_COMPARISON_RANK_INVALID')
            ranks.append(item['rank'])
        elif item.get('rank')is not None:raise ValueError('DISPLAY_COMPARISON_REJECTED_WITH_RANK')
    if sorted(ranks)!=list(range(len(ranks))):raise ValueError('DISPLAY_COMPARISON_RANK_INVALID')
    for c,old in zip(candidates,inputs):
        if identity.source_identity(c['source'])!=old['source']:raise ValueError('DISPLAY_COMPARISON_SOURCE_CHANGED')
    for item in items:
        c=candidates[int(item['id'][1:])];c['comparison']=item
        if item['usable']:c['comparison_rank']=item['rank']
        else:c['plan']={'ok':False,'reason':'DISPLAY_COMPARISON_REJECTED'}
    save(root/'comparison.json',{'observation':row,'inputs':inputs,'formal_delivery_allowed':False})
    return row
