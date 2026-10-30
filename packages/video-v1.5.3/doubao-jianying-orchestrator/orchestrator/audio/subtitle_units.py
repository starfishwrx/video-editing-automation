"""口播长度。词表里的整词只算 1 个字，不把词内字符再加进去。"""
from __future__ import annotations

# Shared by the pre-TTS sentence planner and the engine's final length gate.
MAX_SUBTITLE_CHARS = 15
SUBTITLE_HARD_CHARS = 30

KEEP_WORDS = (
    "HelloKitty", "hellokitty", "心相印", "七度空间", "生理盐水", "湿厕纸", "洗脸巾",
    "棉柔巾", "悬挂", "联名", "云感", "湿巾", "毛巾", "浴巾", "纸巾", "化妆巾", "卸妆棉",
    "安睡裤", "卫生巾", "抽纸", "荧光剂", "乳霜因子", "不含荧光", "超薄透气", "加大加厚",
    "不掉絮", "不反渗", "娇嫩肌肤", "普通干纸", "身体", "家庭",
)

_MARK = "\u0001"
_SORTED = tuple(sorted(set(KEEP_WORDS), key=len, reverse=True))


def text_units(text: str) -> int:
    """整词替换成一个占位后再数长度。占位算 1，其余字符照旧。"""
    value = text or ""
    for word in _SORTED:
        if word:
            value = value.replace(word, _MARK)
    return len(value)
