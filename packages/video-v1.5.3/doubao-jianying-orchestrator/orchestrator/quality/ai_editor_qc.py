"""Blind review prompts and checks against the cut actually written to disk."""
from __future__ import annotations

import math
import re
from .. import ai_editor_contract as contract
from ..policy.continuity import tissue_press_missing
from .. import visual_qc_contract

RESPONSE_VERSION = visual_qc_contract.AI_RESPONSE_VERSION
REPORT_VERSION = visual_qc_contract.AI_REPORT_VERSION


def blind_frame_label(local_index, frame_number, item):
    """Hide selector judgments in the sheet, retaining internal sampling types."""
    part = re.match(r'p(\d+)',str(item.get('suffix') or ''))
    part_label = f' P{part[1]}' if part else ''
    return f"S{local_index:02d}{part_label} F{frame_number:02d} @{item['timestamp']:.3f}s"


def _action_phases(claim):
    item=claim.get('ai_segment') or {}
    parts=item.get('span_parts') if item.get('multi_shot') else [item]
    phases=[p.get('ai_selection',{}).get('presentation_observation') for p in parts or []]
    phases+=((item.get('continuity_group') or {}).get('input') or {}).get('presentation_observations',[])
    return [p for p in phases if isinstance(p,dict)]


def apply_presentation_gate(row, claim):
    """A generic product match cannot waive an explicitly required action."""
    phases=_action_phases(claim)
    if ((not phases and not contract.presentation.promotion_motion(claim.get('claim')))
            or row.get('match_level') not in {'DIRECT','ACCEPTABLE_DEGRADED'}):
        return row
    required=visual_qc_contract.presentation_expected_actors(phases)
    revised={**row,'presentation_check_required':True,'presentation_expected_actors':required}
    if not visual_qc_contract.presentation_check_errors(revised):
        return revised
    revised.update(match_level='INSUFFICIENT_EVIDENCE',visual_ok=False)
    revised['reason']=str(row.get('reason') or '').strip()+' 待核验商品动作及可辨认落点必须有独立实际观察，普通静态展示不能替代。'
    return revised


def source_matches_receipt(actual_path: str, expected: dict, prepared_sources=None) -> bool:
    """Accept a staged copy only after proving byte identity and stable stats."""
    try:
        actual_before = contract.source_identity(actual_path)
        if actual_before == expected:
            return True
        if prepared_sources:
            from ..media.source_normalization import matches_prepared_source
            if matches_prepared_source(actual_path, expected, prepared_sources):
                return True
        if not isinstance(expected, dict) or actual_before["size"] != expected.get("size"):
            return False
        original_path = expected.get("path")
        if not original_path or contract.source_identity(original_path) != expected:
            return False
        same = contract.file_sha256(actual_path) == contract.file_sha256(original_path)
        return (same and contract.source_identity(actual_path) == actual_before
                and contract.source_identity(original_path) == expected)
    except (OSError, TypeError, ValueError, KeyError):
        return False


def prompt(claims: list[dict], frame_count: int) -> str:
    # Purpose is an instruction to inspect, never the selector's pass verdict.
    mapping = "\n".join(f"S{i:02d}: 商品={c.get('product', '')}；口播={c['claim']}；建议用途={c.get('sentence_kind', '')}；物理片段数={c.get('slots', 1)}"
                        for i, c in enumerate(claims, 1))
    for i, claim in enumerate(claims, 1):
        phases = _action_phases(claim)
        goals = list(dict.fromkeys(p['action'] for p in phases if isinstance(p, dict) and p.get('action')))
        intended = {
            'enter':'商品自身相对背景运动进入画面', 'orientation':'商品自身被转动至展示正面',
            'reveal':'商品和遮挡物相对位置发生变化，原本被挡住的商品完整露出且可辨认', 'arrange':'商品被实际摆放形成数量展示',
            'extract':'商品从原处被实际取出并展示'}
        goals = list(dict.fromkeys([intended[c['kind']] for p in phases if isinstance(p,dict)
            for c in [p.get('change') or {}] if c.get('kind') in intended] + goals))
        if contract.presentation.promotion_motion(claim.get('claim')):
            goals.append('促销或数量展示需有商品自身或遮挡物的实际变化，固定背景对照确认；单独扫过陈列不算。')
        if goals:
            mapping += f"\nS{i:02d} 待核验动作意图（不是已发生事实）=" + '；'.join(goals)
    contexts = {contract.digest(contract.rules_identity({"category": c.get("category"), "product": c.get("product")})):
        {"category": c.get("category"), "product": c.get("product")} for c in claims}
    policy = "\n".join(contract.policy_prompt(manifest, include_presentation=False) for manifest in contexts.values())
    local = {c["index"]: i for i, c in enumerate(claims, 1) if "index" in c}
    groups = set()
    for claim in claims:
        group = (claim.get("ai_segment") or {}).get("continuity_group")
        if group:
            indices = tuple(m["index"] for m in group["input"]["members"])
            if not all(i in local for i in indices):
                raise ValueError("CONTINUITY_QC_GROUP_INCOMPLETE")
            groups.add(tuple(local[i] for i in indices))
    if groups:
        mapping += "\n待独立检查的连续画面句组：" + str(sorted(groups))
        mapping += ("。这只是时间线分组，不是通过结论。联合查看本批所有实际切片，独立确认同商品、"
                    "同次演示、同一展示目的或具体卖点、动作和结果完整且可读；仍逐句检查其口播与画面阶段、节奏是否吻合。"
                    "无需每句重播完整实验，但不能借无关句，也不能把后句结果说成前句已出现。"
                    "组内任一必要阶段缺失、不连贯或无法确认时，受影响各句标为INSUFFICIENT_EVIDENCE，不得整体放行。")
    return policy + f"""
你是独立成片审稿人，这是全新调用，不知道选镜器的结论。仅根据最终切片中新抽出的实际画面判断。
每个物理片段依时间顺序提供开始、过程、结束帧，并补充关键动作/结果帧，总共{frame_count}张。
{mapping}
独立检查每句实际内容，不盲信建议用途：含卖点的钩子仍须对应卖点，多个卖点至少有一个具体点被画面实际支持。
reason明确列出实际支持的具体点及未确认的其他点；部分呈现不等于整句全部功效已证实。没有任何具体点成立不得通过。
DIRECT表示本句按其用途正常匹配：贴切的轻薄/柔软展示和普通介绍/钩子的同商品展示均可正常通过，
不要求它们成为物理实验。明确性能结果、数量和数值比较仍须对应画面。包装淋水不能证明巾体干爽。
卫品口播里的反渗、回流、干爽、不黏或黏腻，按压验证只认纸巾：必须看见纸巾接触巾体并提起。手掌、手指、空手离开、按完后手掌干净都不是按压验证。没看见纸巾时这句标INSUFFICIENT_EVIDENCE，不得标DIRECT或ACCEPTABLE_DEGRADED。
价格/优惠/划算等普通促销口播，可用同商品包装或陈列承接；明确价格数值或价格比较仍须对应可读证据。
包装标称只支持包装上的说法，不证明实际性能；EDI纯水等原料标称不要求另拍制水过程。
口播里的层数或结构名称如果说的是瞬吸、吸收、下渗，看得见倒液之后的吸收过程或结果就是这句证据，不要求切开看到内部层数。没有吸收过程的包装和静态陈列仍然不能证明。
防侧漏看见立体护边、侧翼或侧边结构就是直接匹配，不要求倒液防漏实验。只拉伸腰围、没有护边不算。
干净卫生看见同商品外包装或未拆封包装就是直接匹配，标DIRECT，不得标ACCEPTABLE_DEGRADED，不要求撕开实验。
画面反证或对象错误为MISMATCH；动作/结果未拍到、遮挡或无法确认则INSUFFICIENT_EVIDENCE。
如实际切片主要是空等、调机位、拿错重拍或遮挡，说明在哪个物理片段影响了本句，按实际可见证据判定；稳定展示结果本身有信息。
不要因凑满视频而放行，不得把结尾静态结果当作已看见完整过程。
商品展示有动作时，独立看实际裁切是否保留有效变化、第一次展示到位和足够辨认；找东西、重复摆弄和复位不替代有效展示。静态展示可按实际用途判断，不凭选镜器的阶段标签认定看过动作。
即使没有性能卖点，也不能把空景、仅画面边缘的一点包装或大量无信息准备当作完整商品展示；需要在实际有用的商品变化处切入，并呈现可辨认落点。
用固定背景对照判断运动主体。若商品和窗帘、桌边等背景同步移动，而商品彼此关系与形状保持不变，这是相机运动；画内可见商品逐渐变多不能直接推断为手在摆出商品。若待核验目标是商品摆出、举入或转正，却只有相机扫到既有陈列，标INSUFFICIENT_EVIDENCE。
待核验动作意图仅说明这段剪辑要完成什么，不是观察证据；必须逐项在附图核实其变化和落点。若意图包含开盖露出内部商品，只有搬箱、闭箱正面或刚开始掀盖而商品仍被挡住，不得按普通包装展示放过，标INSUFFICIENT_EVIDENCE。其他动作同理，不能偷换成更弱的动作后通过。
输出唯一JSON对象：{{"response_version":"{RESPONSE_VERSION}","segments":[{{"index":1,
"match_level":"DIRECT|ACCEPTABLE_DEGRADED|MISMATCH|INSUFFICIENT_EVIDENCE","visual_ok":true,
"observed":"可见事实","reason":"逐项支持或不支持原因","confidence":0.8}}],"overall":"PASS|PASS_WITH_DEGRADED|FAIL|UNCERTIFIED"}}。
每句恰好一条，index使用本批次从1开始编号。字段必填、描述非空、置信有限且0到1。
正面判断置信至少0.6；DIRECT/ACCEPTABLE_DEGRADED的visual_ok为true，其余为false。
凡有待核验动作意图的句子，另必填action_check={{"actor":"product|occluder|product_and_occluder|camera|hand_only|none|uncertain","change_visible":true,"result_visibility":"clear|partial|occluded|unseen|uncertain","observation":"仅描述实际看见的动作主体、变化和结果遮挡情况"}}。
actor填实际发生所需状态变化的对象，不是施力者，也不照抄意图。手拿起、转正、放下商品，只要商品自身相对背景移动/转动，就填product，不能因手在操作而填hand_only。手打开箱盖而内部商品保持原位，填occluder；两者都发生所需变化填product_and_occluder。纯相机扫动填camera；只有手指指点或抚过已摆好的商品、商品本身不动才填hand_only；没看见动作填none/uncertain。result_visibility只能在必要结果完整露出且足以辨认时填clear，仍有箱盖挡住必要内容时填partial/occluded。
连续句组按整组核查该动作，各成员都报告同一组实际核查结果；不要求每个切片重播动作。change_visible=false、actor不支持所需动作、结果不清楚时，即使普通促销静态匹配也不得DIRECT/ACCEPTABLE_DEGRADED。
"""


def apply_tissue_press_gate(row: dict, *, category: str) -> dict:
    """Downgrade a rewet judgment without observed tissue contact and lifting."""
    if category != "weipin" or row.get("match_level") not in {"DIRECT", "ACCEPTABLE_DEGRADED"}:
        return row
    if not tissue_press_missing([row.get("claim")], [row.get("observed")]):
        return row
    revised = dict(row)
    revised["match_level"] = "INSUFFICIENT_EVIDENCE"
    revised["visual_ok"] = False
    revised["tissue_press_required"] = True
    note = "按压验证只认纸巾，手掌或手指接触不能代替。"
    revised["reason"] = (str(revised.get("reason") or "").strip() + " " + note).strip()
    return revised


def claim_batches(claims, size):
    """Keep a reviewed group together, without sending selector judgments to QC."""
    batches, batch, cursor = [], [], 0
    while cursor < len(claims):
        group = (claims[cursor].get("ai_segment") or {}).get("continuity_group")
        count = len(group["input"]["members"]) if group else 1
        unit = claims[cursor:cursor + count]
        if group and ([c["index"] for c in unit] != [m["index"] for m in group["input"]["members"]]
                      or any((c.get("ai_segment") or {}).get("continuity_group") != group for c in unit)):
            raise ValueError("CONTINUITY_QC_GROUP_INCOMPLETE")
        # An unrelated next shot must not lend its revealed product/result to
        # this action. A certified continuous group is still one visual unit.
        item = claims[cursor].get('ai_segment') or {}
        parts = item.get('span_parts') if item.get('multi_shot') else [item]
        action_unit = (bool(((group or {}).get('input') or {}).get('presentation_observations'))
                       or any((p.get('ai_selection') or {}).get('presentation_observation') for p in parts or []))
        if action_unit or any(contract.presentation.promotion_motion(c.get("claim")) for c in unit):
            if batch:
                batches.append(batch)
                batch = []
            batches.append(unit)
            cursor += count
            continue
        if batch and len(batch) + count > size:
            batches.append(batch)
            batch = []
        batch.extend(unit)
        cursor += count
    if batch:
        batches.append(batch)
    return batches


def validate_cut(claim: dict, rows: list[dict], prepared_sources=None) -> list[str]:
    item = claim["ai_segment"]
    context = {"category": claim.get("category"), "product": claim.get("product")}
    if claim.get("max_absorb_demo_speed") is not None:
        context["max_absorb_demo_speed"] = claim["max_absorb_demo_speed"]
    if not (claim.get("category") or claim.get("product") or "max_absorb_demo_speed" in context
            or (item.get("ai_selection") or {}).get("task_absorb_speed_cap") is not None):
        context = None
    check = contract.validate_segment_receipt(item, index=claim["index"], manifest=context)
    issues = list(check["reasons"])
    expected = item.get("span_parts") if item.get("multi_shot") else [item]
    if len(expected) != len(rows):
        return issues + ["AI_QC_PART_COUNT_CHANGED"]
    for part, actual in zip(expected, rows):
        receipt = part.get("ai_selection") or {}
        if not source_matches_receipt(actual["source"], receipt.get("source_identity"), prepared_sources):
            issues.append("AI_QC_SOURCE_CONTENT_CHANGED")
        start = round(actual["source_start"] * 1e6)
        end = round((actual["source_start"] + actual["source_duration"]) * 1e6)
        planned = receipt.get("window_us") or {}
        issues.extend(contract.presentation.cut_errors(receipt.get("presentation_observation"), start, end))
        expected_target = (part.get("segment_us") if contract.continuity.is_experiment(item.get("continuity_group"))
                           else end - start)
        if item.get("continuity_group") and (start != planned.get("start") or end != planned.get("end")
                or round(actual.get("target_duration", 0) * 1e6) != expected_target):
            issues.append("CONTINUITY_QC_ALLOCATION_CHANGED")
        # Reopening a draft can move a cut by a few frames. Within 100ms the
        # window is unchanged, and evidence clipped only by that shift is not a cut.
        if abs(start - planned.get("start", -1000000)) > 100000 or abs(end - planned.get("end", -1000000)) > 100000:
            issues.append("AI_QC_FINAL_WINDOW_CHANGED")
        if "target_duration" in actual:
            target = actual["target_duration"]
            speed = receipt.get("video_speed", 0)
            if (not isinstance(target, (int, float)) or not math.isfinite(target) or target <= 0
                    or not isinstance(speed, (int, float)) or speed <= 0
                    or abs(actual["source_duration"] / speed - target) > .04):
                issues.append("AI_QC_FINAL_SPEED_CHANGED")
        try:
            recorded_cap = contract.playback.task_absorb_speed_cap(
                {"max_absorb_demo_speed": receipt.get("task_absorb_speed_cap")})
            current_cap = contract.playback.task_absorb_speed_cap(context)
            cap = min(recorded_cap, current_cap) if recorded_cap is not None and current_cap is not None else (
                recorded_cap if recorded_cap is not None else current_cap)
            if cap is not None:
                target_us = round(float(actual["target_duration"]) * 1e6)
                segment = actual.get("segment") or {}
                speed = segment.get("speed")
                problems = contract.playback.conflicts(start_us=start, end_us=end, target_us=target_us,
                    speed=speed, time_sensitive=receipt.get("time_sensitive"),
                    observation=receipt.get("playback_observation"), task_speed_cap=cap, index=claim["index"])
                if problems:
                    issues.append("AI_QC_FINAL_PLAYBACK_POLICY_CONFLICT")
                if (type(speed) not in (int, float) or not math.isfinite(speed) or target_us <= 0
                        or abs(speed - (end - start) / target_us) > max(1e-8, 2 / target_us)):
                    issues.append("AI_QC_FINAL_SPEED_CHANGED")
                materials = actual.get("speed_materials") or []
                if not isinstance(materials, list) or len(materials) != 1:
                    issues.append("AI_QC_FINAL_SPEED_MATERIAL_MISSING")
                    materials = []
                for material in materials:
                    value = material.get("speed") if isinstance(material, dict) else None
                    if (not isinstance(material, dict) or material.get("type") != "speed"
                            or (material.get("mode") is not None and
                                (type(material["mode"]) is not int or material["mode"] != 0))
                            or material.get("curve_speed")
                            or type(value) not in (int, float) or not math.isfinite(value)
                            or type(speed) not in (int, float) or not math.isfinite(speed)
                            or abs(value - speed) > max(1e-8, 2 / target_us)):
                        issues.append("AI_QC_FINAL_SPEED_MATERIAL_CHANGED")
        except (KeyError, ValueError, TypeError, OverflowError, ZeroDivisionError):
            issues.append("AI_QC_FINAL_SPEED_UNVERIFIABLE")
        for ev in receipt.get("evidence_intervals_us", []):
            if (ev["end"] <= start or ev["start"] >= end
                    or ev["start"] < start - 100000 or ev["end"] > end + 100000):
                issues.append("AI_QC_EVIDENCE_CUT_AWAY")
    return sorted(set(issues))


def frame_plan(claim: dict, rows: list[dict]) -> list[tuple]:
    plan = []
    expected = claim["ai_segment"].get("span_parts") if claim["ai_segment"].get("multi_shot") else [claim["ai_segment"]]
    for n, (row, part) in enumerate(zip(rows, expected), 1):
        for j, fraction in enumerate((0, .14, .28, .42, .56, .70, .84, .98)):
            plan.append((row, f"p{n}_t{j}", fraction))
        for k, ev in enumerate((part.get("ai_selection") or {}).get("evidence_intervals_us", [])):
            for label, timestamp in (("begin", ev["start"]), ("result", max(ev["start"], ev["end"] - 40000))):
                fraction = (timestamp / 1e6 - row["source_start"]) / row["source_duration"]
                if 0 <= fraction < 1:
                    plan.append((row, f"p{n}_ev{k}_{label}", fraction))
        phase = contract.presentation.get((part.get("ai_selection") or {}).get("presentation_observation"))
        phases = [phase] if phase else ((part.get("continuity_group") or {}).get("input") or {}).get("presentation_observations", [])
        for k, phase in enumerate(phases):
            for label, timestamp in (("onset", phase["start_us"]), ("reveal", phase["reveal_us"]),
                                     ("readable", max(phase["reveal_us"], phase["readable_end_us"] - 40000))):
                fraction = (timestamp / 1e6 - row["source_start"]) / row["source_duration"]
                if 0 <= fraction < 1:
                    plan.append((row, f"p{n}_phase{k}_{label}", fraction))
    positions = {id(row): index for index, row in enumerate(rows)}
    ordered, seen = [], set()
    for row, label, fraction in sorted(plan, key=lambda p: (
            positions[id(p[0])], round(p[2],9), bool(re.fullmatch(r'p\d+_t\d+',p[1])),p[2])):
        key = (positions[id(row)], round(fraction, 9))
        if key not in seen:
            seen.add(key)
            ordered.append((row, label, fraction))
    return ordered
