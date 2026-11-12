"""Action hints from the visible phase of a solved source window."""
from __future__ import annotations

import math


def _action_name(label):
    if not isinstance(label, str):
        return ""
    label = label.strip()
    if label in {"举起", "举起展示", "托举", "托举展示", "手持展示", "拿起展示", "拿起并展示"}:
        return "手持举起展示"
    if label in {"倾倒", "倒液", "倒入液体"}:
        return "倒液"
    if label in {"无动作", "展示", "无", "未知", "不确定", "空镜", "等待", "调整机位", "对焦", "重拍"}:
        return ""
    return label


def window_action(shot, start, end):
    """Only compare actions when the annotated phase covers the whole cut."""
    segments = shot.get("action_segments") or []
    if not segments:
        return _action_name(shot.get("action"))
    spans = []
    for part in segments:
        try:
            lo, hi = float(part["start"]), float(part["end"])
            if not math.isfinite(lo + hi) or hi <= lo:
                continue
            if lo < end and hi > start:
                spans.append((max(start, lo), min(end, hi), _action_name(part.get("action"))))
        except (KeyError, ValueError, TypeError):
            continue
    actions = {name for _, _, name in spans}
    if len(actions) != 1 or "" in actions:
        return ""
    cursor = start
    for lo, hi, _ in sorted(spans):
        if lo > cursor:
            return ""
        cursor = max(cursor, hi)
    return actions.pop() if cursor >= end else ""
