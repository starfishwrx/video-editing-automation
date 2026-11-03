"""Purpose-specific recall over cached descriptions, followed by mandatory pixel review."""
import json
from pathlib import Path

from .. import ai_editor_contract as identity
from ..selection import ai_editor, purpose_retrieval
from .purpose_review import save

PROTOCOL = 'purpose-information-recall-v3-opening-hook'
PROMPT = '''从缓存描述中为原句找有展示目的的候选；这是文字召回，绝不是画面已通过。
优先考虑：具体动作/展示 -> 可见特点或结果 -> 本句至少一个具体卖点。
不要求整句所有卖点都匹配，不要把“不闷热不粘腻”改成另一句更容易匹配的话。
普通抚摸/捏持/折拢不能因手在动或同商品就排在有实际特点/结果的镜头前。
具体性能或体验句保留自己的候选排序，不能替换为通用包装、本体展示池。
静态侧边厚度、纹理、标称也可以提供信息；无需每个镜头都有动作。
相关特点允许部分支持体验句，但明确限制；透光能表达薄透结构，不证明空气透过率或不粘腻。
注意否定句：“未见照光/没有完成拉伸”等不能当作正面线索；不要靠关键词相同推断。
不要受ID大小或文件顺序影响。优先不同的有效展示方式，避免前几个都是同一种无意义动作。
每句最多6个不同shot_id，按针对性排序；没有描述支持的候选可以少给或为空，不用通用动作填满。
visual_purpose=opening_hook_packaging时只找同商品包装展示来承接钩子，不找泛红/竞品等痛点实验；
此时claim_text仍原样保留，mode=general_product，information_link明确仅包装承接，不证明原句功效。
每个候选用description_excerpt原样引用对应描述的一段，说明信息如何对应原句claim_text，不能捏造。
claim_id与claim_text必须来自本次checks（无claim_text则用原句）。
start_us/end_us是待复核源窗，不是已证实的动作边界。结合描述时间和旧hints定位最有信息的部分；
旧hints可能过宽或错位，不能机械复制起点。窗口必须在该镜头start_us/end_us内。
展示句(require_action=false且require_result=false)只需给start_us，程序计算end_us=start_us+segment_us，
start_us必须预留整段时长。不能从普通准备动作开始，优先把有信息的部分放进固定时长。不足整段时长的源不要选。
动作/结果句保留完成动作与结果所需源窗，后续程序另求局部变速，不能为凑句长截断。
只输出JSON {"sentences":[{"index":1,"candidates":[{"shot_id":"catalog中的完整ID",
"claim_id":"c1","claim_text":"原样给定原句卖点",
"mode":"direct_demonstration|feature_presentation|general_product|uncertain",
"description_excerpt":"原样描述片段","information_link":"具体联系及限制",
"start_us":0,"end_us":2000000}]}]}。
'''


def recall(manifest, requests, shots, index, directory):
    sources = {str(Path(p).resolve()) for p in manifest['material_sources']}
    context = {k: manifest.get(k) for k in ('task_id', 'product', 'category', 'full_script', 'voice')}
    if (index.get('task_id') != manifest['task_id'] or index.get('context') != context
            or set(index.get('sources', [])) != sources
            or any(str(Path(s['video']).resolve()) not in sources for s in shots)):
        raise ValueError('PURPOSE_RECALL_INDEX_SCOPE')
    entries = purpose_retrieval.catalog(shots, index['scans'])
    # Changed/missing caches stop explicitly; never rescan the library here.
    known = {s['shot_id']: s for s in shots}
    before = {}
    for e in entries:
        sid = e['shot_id']
        before[sid] = identity.source_identity(known[sid]['video'])
        if index['scans'][sid].get('source_identity') != before[sid]:
            raise ValueError('PURPOSE_RECALL_INDEX_SOURCE_CHANGED')
    from ..selection.opening_hook import is_packaging_hook
    hooks=[r for r in requests if is_packaging_hook(r)]
    directed=[r for r in requests if not is_packaging_hook(r)]
    payload = {'product': manifest.get('product'), 'requests': directed, 'catalog': entries}
    key = identity.digest([PROTOCOL, payload, before])
    root = Path(directory)
    # Hook packaging needs no invented selling-point text plan. A deterministic
    # pool is only recall; actual dominant same-product packaging is mandatory.
    hook_rankings=purpose_retrieval.allocate(hooks,{r['index']:[]for r in hooks},entries) if hooks else {}
    raw = ai_editor._call(PROMPT + json.dumps(payload, ensure_ascii=False), root / (key + '.json')) if directed else {'sentences':[]}
    rejected = []
    result = purpose_retrieval.validate(raw, directed, entries, rejected=rejected)
    result.update(hook_rankings)
    for sid in {c['shot_id'] for cs in result.values() for c in cs}:
        if identity.source_identity(known[sid]['video']) != before[sid]:
            raise ValueError('PURPOSE_RECALL_SOURCE_CHANGED')
    save(root / 'recall.json', {'protocol': PROTOCOL, 'requests': requests, 'rankings': result,
        'catalog_size': len(entries), 'rejected_hints': rejected,
        'hook_retrieval':'deterministic_pool_then_actual_packaging_review' if hooks else None,
        'scope': 'text retrieval hints only; mandatory actual-window review',
        'maximum_pixel_reviews_per_sentence': 6, 'formal_delivery_allowed': False})
    return result
