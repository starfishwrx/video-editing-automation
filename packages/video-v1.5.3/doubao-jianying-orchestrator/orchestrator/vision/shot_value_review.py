"""Review the information value of an action against the original spoken claim."""
import json
from pathlib import Path

from .. import ai_editor_contract as identity
from ..selection import ai_editor
from ..selection.shot_value import assess

PROMPT='''判断这个动作/展示是否让观众看到了本句卖点，而不是评价画面清晰度或手有没有动。
只看实际带时间的画面，不接收旧结论、置信度或应选哪个素材。原句是目标，不是画面答案。
逐项回答：动作实际做了什么；揭示/强调了什么可见特点或变化/结果；这个信息怎样具体对应原句。
普通抚摸、捏持、翻转、折拢，不因“同商品+手在动”就成为薄软、透气、不闷热或不粘腻的证据。
不要用“通常让人联想舒适”替代可见信息，也不能把原句的“不闷热/不粘腻”偷换成“柔软/亲肤”。
受力变形及恢复可表达回弹，抽出一张且未带出下一张可表达不连抽，包装清楚厚度可表达包装饱满；
背面照光到正面透光可作为薄/透结构的视觉表达，不等于测得空气透过率，更不能顺带支持不粘腻。
体验句可以用确有信息的相关特点作feature_presentation，不必虚构人体实验；明确缺少哪部分。
静态侧边/纹理/标称能揭示本句特点，也可有价值，不要求一定出现动作或状态变化。
如果换成随便别的手部摆弄也能原样套用理由，specific_link=false，记generic_motion。
通用本体展示适合介绍/促销时可记general_product，但不升级为具体性能展示。无合适支持就coverage=none。
复合句只支持一部分时coverage=partial，不能因为其中一个特点相关而把整句写full。
选镜门槛是至少一个具体卖点有信息，不要求逐个卖点全覆盖；不要因另一项未支持就否定已经可见的这一项。
检查清单不是完整性保证；必须独立读完整原句，遗漏或未支持的其他卖点在uncovered_spoken_claims原样引用原文。
all_spoken_claims_checked表示是否逐项审视了完整原句，不表示功效已全部支持。
只输出JSON {"all_spoken_claims_checked":true,"uncovered_spoken_claims":["原句中未支持的卖点片段"],
"claims":[{"check_id":"c1","claim_text":"给定原句/原卖点原样",
"role":"direct_demonstration|feature_presentation|general_product|generic_motion|unrelated|uncertain",
"coverage":"full|partial|none","adds_claim_information":false,"specific_link":false,
"action_or_presentation":"实际动作或静态展示","visible_change_or_feature":"可见信息或缺少",
"claim_link":"信息如何具体对应原句，不对应就直说","missing_information":"未支持的卖点/边界",
"evidence_intervals_us":[{"start":0,"end":1000000}]}],"limitations":"观察限制"}。
所有区间必须在本次源窗口内，不能用文案补出没拍到的结果。
'''


def review(manifest,request,shot,media,directory):
    bounds={'start':round(media['start']*1e6),'end':round(media['end']*1e6)}
    context={'product':manifest['product'],'sentence':request['text'],
        'display_goal':request.get('display_goal','visible_feature'),
        'claims':[{'check_id':c['id'],'claim_text':c.get('claim_text',request['text'])} for c in request['checks']],
        'sample_times_us':[round(t*1e6) for t in media['times']],'source_window_us':bounds}
    prompt=PROMPT+json.dumps(context,ensure_ascii=False)
    key=identity.digest(['shot-selling-point-information-v1',context,media['before'],identity.file_sha256(media['sheet'])])
    row=ai_editor._call(prompt,Path(directory)/'value_calls'/(key+'.json'),Path(media['sheet']))
    result=assess(row,request,bounds)
    if identity.source_identity(shot['video'])!=media['before']:raise ValueError('SHOT_VALUE_SOURCE_CHANGED')
    return {'observation':row,'assessment':result,'source_identity':media['before'],'reviewed_window_us':bounds}
