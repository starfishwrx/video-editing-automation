"""口播长度与字幕切分。词表里的整词只算 1 个字，不把词内字符再加进去。"""
from __future__ import annotations

import re

KEEP_WORDS = (
    "HelloKitty", "hellokitty", "心相印", "七度空间", "生理盐水", "湿厕纸", "洗脸巾",
    "棉柔巾", "悬挂", "联名", "云感", "湿巾", "毛巾", "浴巾", "纸巾", "化妆巾", "卸妆棉",
    "安睡裤", "卫生巾", "抽纸", "荧光剂", "乳霜因子", "不含荧光", "超薄透气", "加大加厚",
    "不掉絮", "不反渗", "娇嫩肌肤", "普通干纸", "身体", "家庭",
    "一次性", "便携装", "独立包装", "湿敷巾", "原生木浆", "雪松绿茶",
)

# engine 硬闸：text_units > HARD 才拦。15 只是历史软参考，切句不按它硬拆。
SOFT_SUBTITLE_CHARS = 15
HARD_SUBTITLE_CHARS = 30

_MARK = "\u0001"
_SORTED = tuple(sorted(set(KEEP_WORDS), key=len, reverse=True))
_CLAUSE_PUNCT = set("，、,；;")
_TRAIL_PUNCT = set("，、,；;。！？!?")


def text_units(text: str) -> int:
    """整词替换成一个占位后再数长度。占位算 1，其余字符照旧。"""
    value = text or ""
    for word in _SORTED:
        if word:
            value = value.replace(word, _MARK)
    return len(value)


def _tokens(text: str) -> list[str]:
    """整词作原子，其余按字；句读标点粘到前一个原子上。"""
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        matched = ""
        for word in _SORTED:
            if word and text.startswith(word, i):
                matched = word
                break
        if matched:
            tokens.append(matched)
            i += len(matched)
            continue
        ch = text[i]
        if tokens and ch in _TRAIL_PUNCT:
            tokens[-1] += ch
        else:
            tokens.append(ch)
        i += 1
    return tokens


def _split_clauses(text: str) -> list[str]:
    """按文案逗号/顿号/分号切开，标点留在左段。"""
    out: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in _CLAUSE_PUNCT:
            out.append("".join(buf))
            buf = []
    if buf:
        out.append("".join(buf))
    return out or ([text] if text else [])


def _pack_to_hard(parts: list[str], *, hard: int) -> list[str]:
    """无标点兜底：只按硬上限切，不往 15 字软目标砍。"""
    out: list[str] = []
    cur = ""
    for part in parts:
        if not part:
            continue
        if not cur:
            cur = part
            continue
        trial = cur + part
        if text_units(trial) <= hard:
            cur = trial
            continue
        out.append(cur)
        cur = part
    if cur:
        out.append(cur)
    return out


def split_subtitle_units(
    text: str,
    *,
    soft: int = SOFT_SUBTITLE_CHARS,
    hard: int = HARD_SUBTITLE_CHARS,
) -> list[str]:
    """按文案逗号/顿号/分号拆开，标点留在左段。

    不缩写、不改字；不按 15 字软目标再拼或硬砍。无逗号且单段 >hard 时，
    才按整词边界拆到硬限内。``soft`` 保留参数兼容，切句逻辑不使用它。
    """
    del soft  # 不按 15 字软目标切割
    raw = re.sub(r"\s+", "", str(text or ""))
    if not raw:
        return []

    pieces: list[str] = []
    for clause in _split_clauses(raw):
        if text_units(clause) <= hard:
            pieces.append(clause)
            continue
        pieces.extend(_pack_to_hard(_tokens(clause), hard=hard))
    return pieces