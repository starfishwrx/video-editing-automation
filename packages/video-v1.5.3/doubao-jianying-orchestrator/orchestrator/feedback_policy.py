"""Classify feedback into review candidates, never executable new rules.

This module is intentionally deterministic. It never decides that a task is
successful; it only labels the feedback scope and the reusable rule candidate.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

_CATEGORY_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("audio_visual_match", ("音画", "口播", "脚本", "画面", "卖点", "匹配", "提数", "柔软", "湿水", "吸水", "干爽", "侧漏", "荧光", "漏选")),
    ("playback_rhythm", ("倍速", "加速", "速度太快", "50倍", "５０倍")),
    ("shot_quality", ("粗剪", "废料", "动作", "镜头", "裁切", "掐头", "去尾", "重复分镜", "重复镜头")),
    ("subtitle_style", ("字幕", "字号", "字体", "加粗", "标点", "x轴", "y轴", "位置")),
    ("voice_mapping", ("配音", "音色", "speaker", "真人播客女", "音频名称")),
    ("duplicate_shots", ("重复分镜", "重复镜头", "同一镜头", "画面重复", "同源", "连续实验")),
)
_GENERIC_HINTS = (
    "通用", "统一", "全局", "默认", "以后", "后续", "每条", "每个", "所有", "都要",
    "整体", "普遍", "规则", "规范", "长期", "同类", "一律", "一直", "多条",
)
_SPECIFIC_HINTS = (
    r"第\s*[0-9一二三四五六七八九十]+\s*(个|条|段|镜头)",
    r"镜头\s*[0-9]+", r"片段\s*[0-9]+", r"本条", r"这条", r"当前视频", r"这个视频",
    r"某一镜", r"某个镜头",
)

_CATEGORY_GUIDANCE = {
    "audio_visual_match": "每句口播卖点必须绑定可审计画面证据；稳定裁切后仍须覆盖证据区间，否则阻断生成。",
    "shot_quality": "粗剪必须避开首尾废料并保留完整动作；动作未完成或废料超阈值时阻断。",
    "subtitle_style": "字幕样式按当前生效的机器规则和 RULES.md 核对，并以渲染帧验收。",
    "voice_mapping": "配音按剪映官方显示名精确映射 speaker_id；无匹配或多匹配时阻断，不自动猜测或回退。",
    "duplicate_shots": "分别核对无意义重复与承载同一实验的连续同源窗口；分类标签不决定是否允许复用。",
    "playback_rhythm": "核对实际源窗口、播放倍速和动作/结果可辨性；上限不等于目标倍速。",
    "generic_feedback": "将经验证的通用运营反馈写入规则真源，并按版本号发布，禁止无版本静默修改。",
}


def _guidance(category: str) -> str:
    guidance = _CATEGORY_GUIDANCE.get(category, _CATEGORY_GUIDANCE["generic_feedback"])
    if category == "subtitle_style":
        policy = Path(__file__).resolve().parents[2] / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json"
        try:
            subtitle = json.loads(policy.read_text(encoding="utf-8-sig"))["subtitle"]
            guidance += " 当前机器规则：" + json.dumps(subtitle, ensure_ascii=False, sort_keys=True)
        except (OSError, ValueError, KeyError, TypeError):
            guidance += " 当前机器规则未读取，须人工核实，不能采用旧模板值。"
    return guidance


def _directions(text: str) -> tuple[dict[str, str], list[str]]:
    # Only explicit opposing phrases are annotated. Ambiguous prose remains
    # unresolved rather than being converted into a confident rule proposal.
    patterns = {
        "subtitle_weight": (r"不要加粗|不加粗|非粗体|取消加粗", r"(?<!不要)(?<!不)(?<!取消)加粗|使用粗体"),
        "same_source_continuity": (r"不允许同源|禁止同源|不要连续|不能连续", r"允许同源|保留同源|保持连续|连续实验|连续同源"),
        "meaningless_repeat": (r"避免重复|不要重复|减少重复|重复.*太多|重复镜头太多", r"允许重复|可以重复"),
    }
    directions, conflicts = {}, []
    for key, (negative, positive) in patterns.items():
        no = bool(re.search(negative, text))
        positive_text = re.sub(negative, "", text)
        yes = bool(re.search(positive, positive_text))
        directions[key] = "conflicting" if no and yes else "opposed" if no else "requested" if yes else "unresolved"
        if no and yes:
            conflicts.append(key)
    return directions, conflicts


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def classify_feedback(text: str) -> dict[str, Any]:
    raw_text = str(text or "")
    original = _clean(raw_text)
    categories: list[str] = []
    for category, terms in _CATEGORY_PATTERNS:
        if any(term.lower() in original.lower() for term in terms):
            categories.append(category)
    # duplicate_shots is a specialization of shot quality; preserve both tags
    # but make the more specific rule the primary candidate.
    if "duplicate_shots" in categories:
        primary = "duplicate_shots"
    elif categories:
        primary = categories[0]
    else:
        primary = "generic_feedback"
    explicit_generic = any(hint in original for hint in _GENERIC_HINTS)
    specific = any(re.search(pattern, original, re.IGNORECASE) for pattern in _SPECIFIC_HINTS)
    scope = "generic" if (explicit_generic or (bool(categories) and not specific)) else "task_specific"
    reason = "explicit_generic_phrase" if explicit_generic else ("category_without_shot_scope" if scope == "generic" else "shot_or_task_specific")
    directions, conflicts = _directions(original)
    return {
        "scope": scope,
        "category": primary,
        "categories": categories,
        "rule_key": f"feedback.{primary}",
        # Compatibility field consumed by old pollers: intentionally empty.
        # Category guidance must never masquerade as a newly approved rule.
        "rule_text": "",
        "category_guidance": _guidance(primary),
        "proposal_status": "needs_review",
        "directions": directions,
        "conflicts": conflicts,
        "topics": [term for term in ("荧光", "倍速", "同源", "重复", "字幕") if term in original],
        "reason": reason,
        "original_text": raw_text,
    }


def rule_text(category: str) -> str:
    """Legacy accessor for category guidance; this is not a rule proposal."""
    return _guidance(category)
