"""Blind review prompts and checks against the cut actually written to disk."""
from __future__ import annotations

import math
from .. import ai_editor_contract as contract
from ..policy.continuity import tissue_press_missing
from .. import visual_qc_contract

RESPONSE_VERSION = visual_qc_contract.AI_RESPONSE_VERSION
REPORT_VERSION = visual_qc_contract.AI_REPORT_VERSION


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
    contexts = {contract.digest(contract.rules_identity({"category": c.get("category"), "product": c.get("product")})):
        {"category": c.get("category"), "product": c.get("product")} for c in claims}
    policy = "\n".join(contract.policy_prompt(manifest) for manifest in contexts.values())
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
                    "同次演示、同一具体卖点、动作和结果完整且可读；仍逐句检查其口播与画面阶段、节奏是否吻合。"
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
不要因凑满视频而放行，不得把结尾静态结果当作已看见完整过程。
输出唯一JSON对象：{{"response_version":"{RESPONSE_VERSION}","segments":[{{"index":1,
"match_level":"DIRECT|ACCEPTABLE_DEGRADED|MISMATCH|INSUFFICIENT_EVIDENCE","visual_ok":true,
"observed":"可见事实","reason":"逐项支持或不支持原因","confidence":0.8}}],"overall":"PASS|PASS_WITH_DEGRADED|FAIL|UNCERTIFIED"}}。
每句恰好一条，index使用本批次从1开始编号。字段必填、描述非空、置信有限且0到1。
正面判断置信至少0.6；DIRECT/ACCEPTABLE_DEGRADED的visual_ok为true，其余为false。
"""


def apply_tissue_press_gate(row: dict, *, category: str) -> dict:
    """Downgrade a passing rewet judgment that never reports tissue."""
    if category != "weipin" or row.get("match_level") not in {"DIRECT", "ACCEPTABLE_DEGRADED"}:
        return row
    if not tissue_press_missing([row.get("claim")], [row.get("observed"), row.get("reason")]):
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
    positions = {id(row): index for index, row in enumerate(rows)}
    ordered, seen = [], set()
    for row, label, fraction in sorted(plan, key=lambda p: (positions[id(p[0])], p[2])):
        key = (positions[id(row)], round(fraction, 9))
        if key not in seen:
            seen.add(key)
            ordered.append((row, label, fraction))
    return ordered
