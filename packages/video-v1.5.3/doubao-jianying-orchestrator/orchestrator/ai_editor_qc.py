"""Blind review prompts and checks against the cut actually written to disk."""
from __future__ import annotations

import math
from . import ai_editor_contract as contract

RESPONSE_VERSION = "chatcut-visual-qc-response-v4"
REPORT_VERSION = "chatcut-visual-qc-v4"


def prompt(claims: list[dict], frame_count: int) -> str:
    # Purpose is an instruction to inspect, never the selector's pass verdict.
    mapping = "\n".join(f"S{i:02d}: 商品={c.get('product', '')}；口播={c['claim']}；建议用途={c.get('sentence_kind', '')}；物理片段数={c.get('slots', 1)}"
                        for i, c in enumerate(claims, 1))
    return contract.POLICY + f"""
你是独立成片审稿人，这是全新调用，不知道选镜器的结论。仅根据最终切片中新抽出的实际画面判断。
每个物理片段依时间顺序提供开始、过程、结束帧，并补充关键动作/结果帧，总共{frame_count}张。
{mapping}
独立检查每句实际内容，不盲信建议用途：含卖点的钩子仍须对应卖点，多个卖点必须全部覆盖。
DIRECT表示本句按其用途正常匹配：贴切的轻薄/柔软展示和普通介绍/钩子的同商品展示均可正常通过，
不要求它们成为物理实验。明确性能结果、数量和数值比较仍须对应画面。包装淋水不能证明巾体干爽。
价格/优惠/划算/「才这个价」类口播：素材通常无价签；同商品包装、盒规数量或陈列可见即判DIRECT，
不要因缺价签给INSUFFICIENT_EVIDENCE或overall=FAIL。
画面反证或对象错误为MISMATCH；动作/结果未拍到、遮挡或无法确认则INSUFFICIENT_EVIDENCE。
商品名「常规波点湿厕纸XSK080」就是心相印 Hello Kitty 波点湿厕纸联名款；画面出现
Hello Kitty / 心相印 / 波点湿厕纸包装或巾体，属于同申报商品，不得判“对象错误/换品”。
不要因凑满视频而放行，不得把结尾静态结果当作已看见完整过程。
输出唯一JSON对象：{{"response_version":"{RESPONSE_VERSION}","segments":[{{"index":1,
"match_level":"DIRECT|ACCEPTABLE_DEGRADED|MISMATCH|INSUFFICIENT_EVIDENCE","visual_ok":true,
"observed":"可见事实","reason":"逐项支持或不支持原因","confidence":0.8}}],"overall":"PASS|PASS_WITH_DEGRADED|FAIL|UNCERTIFIED"}}。
每句恰好一条，index使用本批次从1开始编号。字段必填、描述非空、置信有限且0到1。
正面判断置信至少0.6；DIRECT/ACCEPTABLE_DEGRADED的visual_ok为true，其余为false。
overall须与各段一致：无MISMATCH时不得FAIL；仅有ACCEPTABLE_DEGRADED用PASS_WITH_DEGRADED。
"""


def validate_cut(claim: dict, rows: list[dict]) -> list[str]:
    item = claim["ai_segment"]
    check = contract.validate_segment_receipt(item, index=claim["index"])
    issues = list(check["reasons"])
    expected = item.get("span_parts") if item.get("multi_shot") else [item]
    if len(expected) != len(rows):
        return issues + ["AI_QC_PART_COUNT_CHANGED"]
    for part, actual in zip(expected, rows):
        receipt = part.get("ai_selection") or {}
        source_hash = contract.file_sha256(actual["source"])
        if source_hash != receipt.get("source_sha256"):
            issues.append("AI_QC_SOURCE_CONTENT_CHANGED")
        start = round(actual["source_start"] * 1e6)
        end = round((actual["source_start"] + actual["source_duration"]) * 1e6)
        planned = receipt.get("window_us") or {}
        # One output frame of rounding is allowed; evidence itself cannot be cut.
        if abs(start - planned.get("start", -1000000)) > 40000 or abs(end - planned.get("end", -1000000)) > 40000:
            issues.append("AI_QC_FINAL_WINDOW_CHANGED")
        if "target_duration" in actual:
            target = actual["target_duration"]
            speed = receipt.get("video_speed", 0)
            if (not isinstance(target, (int, float)) or not math.isfinite(target) or target <= 0
                    or not isinstance(speed, (int, float)) or speed <= 0
                    or abs(actual["source_duration"] / speed - target) > .04):
                issues.append("AI_QC_FINAL_SPEED_CHANGED")
        for ev in receipt.get("evidence_intervals_us", []):
            if ev["start"] < start - 1000 or ev["end"] > end + 1000:
                issues.append("AI_QC_EVIDENCE_CUT_AWAY")
    return sorted(set(issues))


def frame_plan(claim: dict, rows: list[dict]) -> list[tuple]:
    plan = []
    expected = claim["ai_segment"].get("span_parts") if claim["ai_segment"].get("multi_shot") else [claim["ai_segment"]]
    for n, (row, part) in enumerate(zip(rows, expected), 1):
        for j, fraction in enumerate((0, .2, .4, .6, .8, .98)):
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
