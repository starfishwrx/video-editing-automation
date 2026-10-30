"""从模型回复里容错提取 JSON。

兼容 ```json 围栏、裸数组/对象、JSON 后面跟中文说明、前面带一句开场白等格式。
空回复单独抛 ``EmptyResponseError``，调用方据此按"可重试/待定"处理而不是判通过。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterator, Optional

_FENCE = re.compile(r"```[ \t]*(?:json|JSON|javascript|js)?[ \t]*\r?\n?(.*?)```", re.S)
_DECODER = json.JSONDecoder()


class EmptyResponseError(ValueError):
    """模型返回了空内容（0 字节或只有空白）。"""


def is_blank(text: Any) -> bool:
    return not str(text or "").replace("\ufeff", "").strip()


def _scan(text: str) -> list[Any]:
    values: list[Any] = []
    cursor = 0
    while cursor < len(text):
        positions = [p for p in (text.find("{", cursor), text.find("[", cursor)) if p >= 0]
        if not positions:
            break
        start = min(positions)
        try:
            value, end = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        values.append(value)
        cursor = end
    return values


def iter_json_values(text: Any) -> Iterator[Any]:
    """依次产出文本里每个完整的顶层 JSON 对象/数组；有围栏时只看围栏内。"""
    raw = str(text or "").replace("\ufeff", "").strip()
    fenced: list[Any] = []
    for block in _FENCE.findall(raw):
        fenced.extend(_scan(block))
    if fenced:
        yield from fenced
        return
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    yield from _scan(raw)


def _structured(value: Any) -> bool:
    return isinstance(value, dict) or (
        isinstance(value, list) and all(isinstance(item, (dict, list)) for item in value))


def extract_json(text: Any, *, accept: Optional[Callable[[Any], bool]] = None) -> Any:
    """返回第一个满足 ``accept`` 的 JSON 值；默认要求对象或"元素都是对象/数组"的数组。"""
    if is_blank(text):
        raise EmptyResponseError("vision model returned an empty response")
    check = accept or _structured
    for value in iter_json_values(text):
        if check(value):
            return value
    raise ValueError("vision model response did not contain valid JSON")


def try_extract_json(text: Any, default: Any = None, **kwargs: Any) -> Any:
    try:
        return extract_json(text, **kwargs)
    except ValueError:
        return default
