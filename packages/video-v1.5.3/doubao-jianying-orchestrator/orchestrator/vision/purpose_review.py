"""Task-directed observations. Search hints never become acceptance evidence."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .. import ai_editor_contract as contract
from ..selection import ai_editor
from ..selection.paper_selection import validate_observation

PROTOCOL = "purpose-observation-v3-experiment-pairs"
OUTPUT_PROTOCOL = "purpose-output-review-v5-experiment-pairs"


def policy(manifest):
    text = contract.policy_prompt(manifest) + """
用途边界：普通介绍、个人使用/回购经历、修辞衔接、泛促销和囤货号召，用贴切同商品展示即可；
不要要求购买记录、多品牌对比、促销价格牌或证明一句修辞。薄软、舒适、不闷热不粘腻等体验表达
允许贴切真实产品展示，不要求模拟高温穿戴、皮肤检测或人体实验。明确成分须可读标称，
吸收/反渗等性能须真实演示和结果；明确持续时间、数值、比较不能由氛围、透光或短实验外推。
"""
    if contract.editorial_context(manifest)["category"] == "weipin":
        text += """卫品具体判据优先：不闷热/透气按本品类规则用对光透过、孔洞或网面；
倒液不能替代透气。吸水组合句完整展示倒液、吸收、纸巾按压与结果，可跨文件但须对应同一次实验；
仅吸收或仅按压先记录部分事实，再对通常相邻的素材做实验配对复核。禁止换另一轮实验的按压结果，
不能把跨文件本身判为不合格。必要的结果停留不当作无意义空镜。
"""
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
只输出JSON {"sentences":[{"index":1,"text":"原文","require_action":false,"require_result":false,
"checks":[{"id":"c1","question":"逐项检查什么","type":"display|label|action_result|unverifiable"}]}]}。
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
        checks = row.get("checks")
        if (not isinstance(checks, list) or not checks or any(
                not isinstance(c, dict) or not isinstance(c.get("id"), str) or not c["id"].strip()
                or not isinstance(c.get("question"), str) or not c["question"].strip()
                or c.get("type") not in {"display", "label", "action_result", "unverifiable"} for c in checks)
                or len({c["id"] for c in checks}) != len(checks)):
            raise ValueError("PURPOSE_REQUIREMENT_CHECKS")
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


def review(manifest, request, shot, media, directory):
    # The observer never sees target narration duration or a proposed speed.
    context = {"product": manifest["product"], "sentence": request["text"], "checks": request["checks"],
        "require_action": request["require_action"], "require_result": request["require_result"],
        "sample_times_us": [round(t*1e6) for t in media["times"]],
        "source_window_us": {"start": round(media["start"] * 1e6), "end": round(media["end"] * 1e6)}}
    prompt = policy(manifest) + """只看附图，独立检查这句实际需要的事实。口播不是答案，不能确认就uncertain。
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
    key = contract.digest([PROTOCOL, "review", context, media["before"]])[:24]
    row = ai_editor._call(prompt, Path(directory) / "review_calls" / f"{key}.json", Path(media["sheet"]))
    supported = check_results(row.get("checks"), request["checks"])
    row = validate_observation(row, shot, request)
    if row["verdict"] == "matched" and not supported:
        raise ValueError("PURPOSE_MATCH_WITHOUT_SUPPORT")
    if media["before"] != contract.source_identity(shot["video"]):
        raise ValueError("PURPOSE_SOURCE_CHANGED")
    return {**row, "supported_ids": supported, "source_identity": media["before"],
        "reviewed_window_us": context["source_window_us"], "sheet": str(media["sheet"])}


def review_output(manifest, request, shot, media, directory):
    context = {"product": manifest["product"], "sentence": request["text"],
        "check_ids": [c["id"] for c in request["checks"]]}
    prompt = policy(manifest) + """独立检查实际剪出的片段。你不知道选镜器的结论。只按图片中的可见事实逐项判断，
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
    supported = check_results(data.get("checks"), request["checks"])
    if (any(type(data.get(k)) is not bool for k in ("action_complete", "result_visible", "rhythm_ok"))
            or type(data.get("all_script_claims_checked")) is not bool
            or not isinstance(data.get("uncovered_claims"), list)
            or any(not isinstance(v, str) or not v.strip() for v in data["uncovered_claims"])
            or not isinstance(data.get("reason"), str) or not data["reason"].strip()):
        raise ValueError("PURPOSE_OUTPUT_REVIEW_INVALID")
    if media["before"] != contract.source_identity(shot["video"]):
        raise ValueError("PURPOSE_OUTPUT_CHANGED")
    ok = (len(supported) == len(request["checks"]) and data["rhythm_ok"]
        and data["all_script_claims_checked"] and not data["uncovered_claims"]
        and (not request["require_action"] or data["action_complete"])
        and (not request["require_result"] or data["result_visible"]))
    return {**data, "status": "SAMPLED_SUPPORT" if ok else "EVIDENCE_PENDING", "index": request["index"],
        "output_identity": media["before"], "window_us": {"start": round(media["start"] * 1e6), "end": round(media["end"] * 1e6)},
        "formal_delivery_allowed": False, "review_method": "16 timestamped output samples; full playback not certified"}
