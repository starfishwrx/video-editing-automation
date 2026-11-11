"""Task-directed observations. Search hints never become acceptance evidence."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .. import ai_editor_contract as contract
from ..selection import ai_editor
from ..selection.paper_selection import validate_observation

PROTOCOL = "purpose-observation-v7-opening-hook-packaging"
OUTPUT_PROTOCOL = "purpose-output-review-v9-opening-hook-packaging"

USABILITY_PROMPT='''
在输出JSON顶层另加shot_usability对象报告镜头可用性，不从同商品、口播或高置信度推断画面有效。
先描述具体看清的信息点。整理护翼、翻折收拢、准备姿态、手遮住主体，不等于柔软/轻薄展示。
柔软演示应能看清受力变形与完成/恢复形态；薄度须有看清边缘/层厚的视角；
仅把产品折成一团不能证明薄软，更不能推断透气或不粘腻。体验句可用贴切物性展示，不强制人体实验。
稳定、清楚的本体或包装展示可用于普通介绍/促销；具体体验目标要能实际看见对应特点。
clear_intervals_us只记录主体和所需信息点确实可读的时间区间，手遮挡或仅整理阶段不计。
shot_usability字段：{"view_kind":"stable_display|complete_demonstration|handling_or_setup|uncertain",
"subject_visibility":"clear|partial|blocked|uncertain","relation":"visible_feature|general_product|inferred_only|unrelated|uncertain",
"demonstration_complete":false,"information_point":"实际可见信息及限制",
"clear_intervals_us":[{"start":0,"end":1000000,"observed":"这段具体看清什么"}],"limitations":"采样和看不清部分"}。
这与语义相关性分别判断，语义贴切但镜头没有展示清楚时仍记录不可用事实。
'''


def policy(manifest):
    text = contract.policy_prompt(manifest) + """
用途边界：普通介绍、个人使用/回购经历、修辞衔接、泛促销和囤货号召，用贴切同商品展示即可；
不要要求购买记录、多品牌对比、促销价格牌或证明一句修辞。具体体验表达需要有相应卖点信息的特点展示，
不要求模拟高温穿戴、皮肤检测或人体实验，但普通抚摸/折拢不自动表达薄软、不闷热或不粘腻。
不能把原句目标替换成另一个容易展示的特点；通用同商品配图与卖点表达分开记录。明确成分须可读标称，
吸收/反渗等性能须真实演示和结果；明确持续时间、数值、比较不能由氛围、透光或短实验外推。
"""
    if contract.editorial_context(manifest)["category"] == "weipin":
        text += """卫品具体判据优先：不闷热/透气按本品类规则用对光透过、孔洞或网面；
倒液不能替代透气。吸水组合句完整展示倒液、吸收、纸巾按压与结果，可跨文件但须对应同一次实验；
仅吸收或仅按压先记录部分事实，再对通常相邻的素材做实验配对复核。禁止换另一轮实验的按压结果，
不能把跨文件本身判为不合格。必要的结果停留不当作无意义空镜。
"""
    if contract.editorial_context(manifest)['category']=='zhipin':
        text+='''纸品动作事实：不掉絮须真实揉压/揉搓后展开或抖松查看，单纯拉伸不能替代揉搓。
只取一次完整演示和首次可读结果，不把之后的重复揉搓、长时间拉伸全算作必须保留的结果。
定性吸水的可读结果是挤压时清水明显涌出，不必等巾体拧到全干或最后一滴水停止。
首次关键接触、动作完成及可读结果须保护；持续重复揉搓、继续展开或挤压的中间过程可记process。
关键瞬间和结果仍标key_action/result且can_speed=false；不可把整段重复过程一概锁成关键动作。
涉及瞬吸、吸得快或具体时长的主张仍按time_sensitive保留相关过程原速。
'''
    return text


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def requirements(manifest, narration, directory):
    rows = narration["rows"]
    category = contract.editorial_context(manifest)["category"]
    topics = ""
    if category == "weipin":
        topics = """卫品每行另带topic，枚举absorption|rewet|followup|bridge|breathability|fluorescence|sku_change|promotion|other|uncertain。
吸收/锁水=absorption，反渗=rewet，同实验干爽总结=followup，不闷热/透气=breathability，
纯承接=bridge；有独立卖点不能标bridge。先按原文主题分类，不因缺素材改变分类。
"""
    data = ai_editor._call(policy(manifest) + topics + """把口播转成需要检查的画面事实。不要判断任何素材已经支持。
每句保留编号与原文，多个卖点分别列check。体验/介绍/促销允许贴切同商品展示；纯棉等成分须可读标称，
吸收/反渗等需真实动作和可见结果，透气体验可用结构/薄软等贴切展示但不能声称已物理证明，
一整天效果不能由短实验外推。修辞衔接/囤货号召/普通促销只需display，不能标为unverifiable。
不增加原文没有的物理证明要求，也不删除难以证明的内容。需要动作/结果时对应布尔值为true。
展示句另带display_goal：普通介绍/促销为general_product，薄软/舒适等具体特点为visible_feature。
每句另给has_product_selling_point布尔值：是否对本品提出具体特点/标称/性能。is_generic_hook 标明任意位置的空泛钩子。全稿中这类钩子或开头第1—3句无具体卖点时，
作为钩子用本品包装承接，display_goal=general_product、require_action/result=false、checks只用display。
“谁还在用擦脸泛红的杂牌”是痛点钩子，has_product_selling_point=false，不要求泛红或竞品实验。
“本品擦脸不泛红/不掉絮”“200抽”“联名”“悬挂式设计”均有具体卖点，必须true，保留原有检查要求。
允许展示不等于只要同商品就合格，检查项写清观众实际需看清的形态、结构或动作结果。
每项另带claim_text，必须保留该卖点的原文片段。“不闷热、不粘腻”等不同卖点分项，
不能把原句改写成“展示本体/薄软”后按新的容易条件宣布支持；薄软或结构只作为候选表达方式。
只输出JSON {"sentences":[{"index":1,"text":"原文","has_product_selling_point":false,"is_generic_hook":true,"require_action":false,"require_result":false,"display_goal":"general_product|visible_feature",
"checks":[{"id":"c1","claim_text":"对应卖点原文片段","question":"逐项检查什么","type":"display|label|action_result|unverifiable"}]}]}。
""" + json.dumps({"product": manifest["product"], "sentences": [
        {"index": i, "text": r["text"]} for i, r in enumerate(rows, 1)]}, ensure_ascii=False),
        Path(directory) / "requirements_call.json")
    result = data.get("sentences")
    if not isinstance(result, list) or len(result) != len(rows):
        raise ValueError("PURPOSE_REQUIREMENT_COVERAGE")
    for i, (row, audio) in enumerate(zip(result, rows), 1):
        if not isinstance(row,dict) or type(row.get("index")) is not int or row["index"] != i or row.get("text") != audio["text"]:
            raise ValueError("PURPOSE_REQUIREMENT_IDENTITY")
        if any(type(row.get(k)) is not bool for k in ("require_action", "require_result")):
            raise ValueError("PURPOSE_REQUIREMENT_ACTION_POLICY")
        if category == "weipin" and row.get("topic") not in {
                "absorption","rewet","followup","bridge","breathability","fluorescence",
                "sku_change","promotion","other","uncertain"}:
            raise ValueError("PURPOSE_REQUIREMENT_TOPIC_REQUIRED")
        if row.get('display_goal','visible_feature') not in {'general_product','visible_feature'}:
            raise ValueError('PURPOSE_DISPLAY_GOAL_INVALID')
        checks = row.get("checks")
        if (not isinstance(checks, list) or not checks or any(
                not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"].strip()
                or not isinstance(c.get("question"), str) or not c["question"].strip()
                or c.get("type") not in {"display", "label", "action_result", "unverifiable"} for c in checks)
                or len({c["id"] for c in checks}) != len(checks)):
            raise ValueError("PURPOSE_REQUIREMENT_CHECKS")
        if any('claim_text' in c and (not isinstance(c['claim_text'],str) or not c['claim_text'].strip()
                or c['claim_text'] not in row['text']) for c in checks):
            raise ValueError('PURPOSE_REQUIREMENT_CLAIM_SUBSTITUTED')
    from ..selection.opening_hook import normalize,RULE
    result=normalize(result)
    data={**data,'sentences':result,'opening_hook_rule':RULE}
    save(Path(directory) / "requirements.json", data)
    return result


def check_results(checks, expected):
    if not isinstance(checks, list) or len(checks) != len(expected) or any(not isinstance(c,dict) for c in checks):
        raise ValueError("PURPOSE_CHECK_COVERAGE")
    by_id = {c["id"]: c for c in expected}
    if len({c.get("id") for c in checks}) != len(checks) or set(by_id) != {c.get("id") for c in checks}:
        raise ValueError("PURPOSE_CHECK_IDENTITY")
    for c in checks:
        if (c.get("status") not in {"supported", "unsupported", "uncertain"}
                or not isinstance(c.get("observed"), str) or not c["observed"].strip()):
            raise ValueError("PURPOSE_CHECK_FACT_REQUIRED")
        if by_id[c["id"]]["type"] == "unverifiable" and c["status"] == "supported":
            raise ValueError("PURPOSE_UNVERIFIABLE_PROMOTED")
    return [c["id"] for c in checks if c["status"] == "supported"]


def scan(manifest, requests, shot, media, directory):
    context = {"product": manifest["product"], "sentences": requests,
        "source_window_us": {"start": round(media["start"] * 1e6), "end": round(media["end"] * 1e6)}}
    prompt = policy(manifest) + """先客观描述附图中实际出现的对象、动作及结果，不从文件名或口播补全事实。
再给可能相关的口播编号与源时间线索。只是候选召回，不作支持或通过判断；没有对应内容就空hints。
输出JSON {"description":"完整可见事实与限制","hints":[{"index":1,"start_us":0,"end_us":1000000,"reason":"相关线索"}]}。
时间为图片所标源绝对微秒。多个替代动作可以给多条线索。不要把长源整段都当必要动作。
""" + json.dumps(context, ensure_ascii=False)
    key = contract.digest([PROTOCOL, "scan", context, media["before"]])[:24]
    data = ai_editor._call(prompt, Path(directory) / "scan_calls" / f"{key}.json", Path(media["sheet"]))
    if not isinstance(data.get("description"), str) or not data["description"].strip() or not isinstance(data.get("hints"), list):
        raise ValueError("PURPOSE_SCAN_INVALID")
    known = {r["index"] for r in requests}
    lo, hi = context["source_window_us"].values()
    valid_hints,invalid_hints=[],[]
    for h in data["hints"]:
        if (not isinstance(h,dict) or type(h.get("index")) is not int or h["index"] not in known
                or any(type(h.get(k)) is not int for k in ("start_us", "end_us"))
                or not lo <= h["start_us"] < h["end_us"] <= hi):
            invalid_hints.append(h)
        else:
            valid_hints.append(h)
    data={**data,"hints":valid_hints,"discarded_hints":invalid_hints}
    if media["before"] != contract.source_identity(shot["video"]):
        raise ValueError("PURPOSE_SOURCE_CHANGED")
    return {**data, "shot_id": shot["shot_id"], "source_identity": media["before"]}


def _phase_observation(prompt, path, sheet, shot, request):
    row = ai_editor._call(prompt, path, sheet)
    try:
        return validate_observation(row, shot, request)
    except ValueError as error:
        if str(error) != 'PAPER_REVIEW_PROTECTED_PHASE_ACCELERATED':
            raise
    # A contradictory permission is a malformed response, not a bad picture.
    # Re-observe once; never silently flip permissions or promote an old result.
    retry = Path(path).with_name(Path(path).stem + '-phase-schema-retry.json')
    correction = ('\n上次响应有格式矛盾：key_action/result 的 can_speed 被标为true。'
        'can_speed表示是否允许后续加速，不是对画面当前速度的描述。请重新看同一附图，'
        '重新输出完整观察；关键动作和结果必须false，不能为通过格式而把它们改成process。'
        '可以重新判uncertain/mismatch，不得为了通过而补出未见的事实。')
    row = validate_observation(ai_editor._call(prompt + correction, retry, sheet), shot, request)
    return {**row, 'schema_recovery': {'attempts': 1,
        'first_error': 'PAPER_REVIEW_PROTECTED_PHASE_ACCELERATED', 'retry_call': str(retry)}}


def review(manifest, request, shot, media, directory):
    from ..selection.shot_usability import is_display,assess
    value=None
    if is_display(request):
        from .shot_value_review import review as review_value
        value=review_value(manifest,request,shot,media,directory)
        if value['assessment']['status']=='NO_SELLING_POINT_INFORMATION':
            return {'verdict':'mismatch','reason':'NO_SELLING_POINT_INFORMATION','selling_point_value':value,
                'observation':'The action does not convey the requested selling point.','supported_ids':[],
                'source_identity':media['before'],'reviewed_window_us':value['reviewed_window_us'],'sheet':str(media['sheet'])}
    quality=None
    if is_display(request):
        from .shot_quality_review import observe
        quality=observe(shot,media,directory)
        gate=assess(quality,request,{'start':round(media['start']*1e6),'end':round(media['end']*1e6)},check_relation=False)
        if gate['status']=='REJECTED':
            return {'verdict':'mismatch','reason':'SHOT_NOT_USABLE_BEFORE_SEMANTIC_MATCH',
                'observation':quality['information_point'],'shot_usability':{**quality,'relation':'uncertain'},
                'usability':gate,'supported_ids':[],'source_identity':media['before'],
                'reviewed_window_us':{'start':round(media['start']*1e6),'end':round(media['end']*1e6)},'sheet':str(media['sheet'])}
    # The observer never sees target narration duration or a proposed speed.
    context = {"product": manifest["product"], "sentence": request["text"], "checks": request["checks"],
        'display_goal':request.get('display_goal','visible_feature'),
        "require_action": request["require_action"], "require_result": request["require_result"],
        "sample_times_us": [round(t*1e6) for t in media["times"]],
        "source_window_us": {"start": round(media["start"] * 1e6), "end": round(media["end"] * 1e6)}}
    if request.get('visual_purpose')=='opening_hook_packaging':
        context['visual_purpose']='opening_hook_packaging'
    prompt = policy(manifest) + USABILITY_PROMPT + """只看附图，独立检查这句实际需要的事实。口播不是答案，不能确认就uncertain。
逐项记录supported/unsupported/uncertain及观察；短实验不能证明一整天或绝对功效。
只定位一次紧凑完整的真实演示，去掉重复演示。动作开端、关键接触、完成与可见结果不能省略；
关键动作、结果保持原速，只有明确无关紧要的过程允许加速。结果需有可看清的停留，不按目标句长裁证据。
普通产品展示/标称句，只检查实际给出的源窗是否可读和贴切，不要求把拿起和放下也判作完整演示。
涉及吸得快、干得快或明确时长的结论，time_sensitive必须为true，相关整个过程保留原速。
输出JSON {"verdict":"matched|mismatch|uncertain","same_product":true,"supports_requirement":true,
"confidence":0.8,"observation":"可见事实","reason":"对应理由与缺失项","time_sensitive":false,
"checks":[{"id":"c1","status":"supported|unsupported|uncertain","observed":"逐项事实"}],
"window_us":{"start":0,"end":2000000},"evidence_intervals_us":[{"start":0,"end":2000000}],
"phases":[{"start":0,"end":2000000,"kind":"setup|process|key_action|result|display","can_speed":false,"reason":"依据"}]}。
window是足以表达这次演示的连续源窗，phases按顺序无缝覆盖；key_action/result的can_speed必须false。
至少一项实际支持才可matched，其他缺失仍逐项保留；没有任何支持就uncertain或mismatch。
""" + json.dumps(context, ensure_ascii=False)
    if is_display(request) and round((media['end']-media['start'])*1e6)==request.get('segment_us'):
        prompt+='\n本次展示输入已经是拟剪出的完整窗口，window_us必须等于source_window_us，phases无缝覆盖全部输入。输入将原速播放，本次所有phases的can_speed均须false，key_action/result更不能标true。若只有一小段可用，在shot_usability明确记录，不通过缩小window隐去问题。'
    key = contract.digest([PROTOCOL, "review", context, media["before"]])[:24]
    row = _phase_observation(prompt, Path(directory) / "review_calls" / f"{key}.json",
                             Path(media["sheet"]), shot, request)
    if quality is not None and row.get('verdict')=='matched':
        from ..selection.shot_usability import intersect_observations
        row['shot_usability']=intersect_observations(quality,row.get('shot_usability'),request,context['source_window_us'])
    supported = check_results(row.get("checks"), request["checks"])
    if value is not None:supported=[i for i in supported if i in value['assessment']['supported_ids']]
    row = validate_observation(row, shot, request)
    if row["verdict"] == "matched" and not supported:
        raise ValueError("PURPOSE_MATCH_WITHOUT_SUPPORT")
    if media["before"] != contract.source_identity(shot["video"]):
        raise ValueError("PURPOSE_SOURCE_CHANGED")
    return {**row, 'selling_point_value':value,"supported_ids": supported, "source_identity": media["before"],
        "reviewed_window_us": context["source_window_us"], "sheet": str(media["sheet"])}


def review_output(manifest, request, shot, media, directory):
    from ..selection.shot_usability import is_display
    value=None
    if is_display(request):
        from .shot_value_review import review as review_value
        value=review_value(manifest,request,shot,media,directory)
    quality=None
    if is_display(request):
        from .shot_quality_review import observe
        quality=observe(shot,media,directory)
    context = {"product": manifest["product"], "sentence": request["text"],
        "check_ids": [c["id"] for c in request["checks"]]}
    from ..selection.opening_hook import is_packaging_hook
    if is_packaging_hook(request):
        context['visual_purpose']='opening_hook_packaging'
        context['visual_task']='只核对本品包装可辨并自然承接开头钩子；不要求痛点/竞品/功效画面，原文断言不获验证。'
    prompt = policy(manifest) + USABILITY_PROMPT + """独立检查实际剪出的片段。你不知道选镜器的结论。只按图片中的可见事实逐项判断，
不能从口播/字幕推导画面已经证实，也不能把场景联想当成功效。标称仅支持标称，短实验不支持一整天。
检查动作是否完整，结果是否看清，明显超速是否损害理解。采样没拍到的动作不能猜测发生过。
输出JSON {"checks":[{"id":"c1","status":"supported|unsupported|uncertain","observed":"事实"}],
"action_complete":true,"result_visible":true,"rhythm_ok":true,"reason":"可见事实和限制",
"all_script_claims_checked":true,"uncovered_claims":["原文中检查清单遗漏的卖点或无法支持的其他断言"]}。
清单不是完整性保证，必须独立读原文。发现清单漏掉卖点时写uncovered_claims，不能仅按给出的check打分。
check_ids只提供编号，没有选镜器的解释或分类。按文案卖点出现顺序对应编号，独立判断需要何种画面。
明确的持续时间与绝对效果须保留未核实范围，不能仅因为产品展示贴切就声称已验证整段时长或绝对功效。
""" + json.dumps(context, ensure_ascii=False)
    key = contract.digest([PROTOCOL, "output", context, media["before"], media["start"], media["end"]])[:24]
    data = ai_editor._call(prompt, Path(directory) / "output_calls" / f"{key}.json", Path(media["sheet"]))
    if quality is not None:
        from ..selection.shot_usability import intersect_observations
        data['shot_usability']=intersect_observations(quality,data.get('shot_usability'),request,
            {'start':round(media['start']*1e6),'end':round(media['end']*1e6)})
    supported = check_results(data.get("checks"), request["checks"])
    if (any(type(data.get(k)) is not bool for k in ("action_complete", "result_visible", "rhythm_ok"))
            or type(data.get("all_script_claims_checked")) is not bool
            or not isinstance(data.get("uncovered_claims"), list)
            or any(not isinstance(v, str) or not v.strip() for v in data["uncovered_claims"])
            or not isinstance(data.get("reason"), str) or not data["reason"].strip()):
        raise ValueError("PURPOSE_OUTPUT_REVIEW_INVALID")
    if media["before"] != contract.source_identity(shot["video"]):
        raise ValueError("PURPOSE_OUTPUT_CHANGED")
    from ..selection.shot_usability import is_display,assess
    usability=assess(data.get('shot_usability'),request,{'start':round(media['start']*1e6),'end':round(media['end']*1e6)}) if is_display(request) else None
    coverage_ok=(bool(set(supported)&set(value['assessment']['supported_ids'])) if value is not None else
        len(supported)==len(request['checks']) and data['all_script_claims_checked'] and not data['uncovered_claims'])
    ok = (coverage_ok and (usability is None or usability['status']=='USABLE_FOR_COMPARISON')
        and data["rhythm_ok"]
        and (not request["require_action"] or data["action_complete"])
        and (not request["require_result"] or data["result_visible"]))
    return {**data, 'selling_point_value':value,
        'selling_point_coverage':value['assessment']['status'] if value is not None else None,
        'selection_policy':value['assessment'].get('selection_policy','at_least_one_concrete_selling_point') if value is not None else 'required_checks',
        'visual_requirement_supported_ids':value['assessment'].get('visual_requirement_supported_ids',value['assessment'].get('supported_ids',[])) if value is not None else supported,
        'claim_truth_supported_ids':value['assessment'].get('claim_truth_supported_ids') if value is not None else None,
        'usability':usability,"status": "SAMPLED_SUPPORT" if ok else "EVIDENCE_PENDING", "index": request["index"],
        "output_identity": media["before"], "window_us": {"start": round(media["start"] * 1e6), "end": round(media["end"] * 1e6)},
        "formal_delivery_allowed": False, "review_method": "16 timestamped output samples; full playback not certified"}
