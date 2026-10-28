"""Plan narration once before TTS; reject model changes to the supplied script."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from . import subtitle_units

PROMPT = """你是短视频口播编辑。只把原文拆成自然口播和字幕句段，不改写、不增删、不替换文字或标点。
参考节奏：一条字幕通常表达一个完整意思或一个具体卖点；包装、材质、揉搓、检测和湿水效果可以分别成句。
不要把品牌、商品名、数字单位、词语或语法结构拆开；短引导语与后文连在一起，不机械追求字数或镜头数。
同一句允许多个镜头，同一动作也可以跨连续相关句展示；不要为了镜头把必要动作剪碎。
无标点的长句也按自然意思拆，但必须能将每段依次拼回原文。每段尽量简短，最多30个单位。
例如“百分百原生木浆无荧光剂，湿水不破”可分为“百分百原生木浆”“无荧光剂，”“湿水不破”。
“谁能拒绝精致包装和好闻的香味啊”应保持为语法完整的一句，不要让“和好闻的香味啊”孤立成句。
只返回 JSON：{"segments":["原文第一段", "原文第二段"]}。不要解释。
原文：\n"""


def validate(text: str, response: dict) -> list[dict]:
    parts = response.get('segments') if isinstance(response, dict) else None
    if not isinstance(parts, list) or not parts or len(parts) > 128 or any(not isinstance(p, str) or not p.strip() for p in parts):
        raise ValueError('SENTENCE_PLAN_INVALID_SEGMENTS')
    source = re.sub(r'\s+', '', text)
    clean = [re.sub(r'\s+', '', p) for p in parts]
    if ''.join(clean) != source:
        raise ValueError('SENTENCE_PLAN_TEXT_CHANGED')
    bounds = set()
    cursor = 0
    for p in clean[:-1]:
        cursor += len(p)
        bounds.add(cursor)
    protected = list(re.finditer(r'[A-Za-z0-9]+(?:[.,:+\-][A-Za-z0-9]+)*(?:[%°℃]|倍速|倍|元|片|包|层|毫升|毫克)?', source))
    for word in subtitle_units.KEEP_WORDS:
        protected.extend(re.finditer(re.escape(word), source, flags=re.IGNORECASE))
    if any(match.start() < b < match.end() for match in protected for b in bounds):
        raise ValueError('SENTENCE_PLAN_SPLIT_TOKEN')
    if any(not re.search(r'[\w\u3400-\u9fff]', p) or subtitle_units.text_units(p) > 30 for p in clean):
        raise ValueError('SENTENCE_PLAN_UNREADABLE')
    # Reconstruct from the input, never from model spelling or whitespace.
    # Whitespace at a newly introduced boundary is trimmed; whitespace inside
    # each phrase is retained (e.g. English product names).
    positions = [i for i, ch in enumerate(text) if not ch.isspace()]
    segments = []
    start = 0
    consumed = 0
    for piece in clean:
        consumed += len(piece)
        end = positions[consumed - 1] + 1
        segments.append({'text': text[start:end].strip()})
        start = end
    return segments


def plan(manifest: dict, directory: Path, call: Callable | None = None) -> dict:
    text = str(manifest.get('full_script') or manifest.get('script') or '')
    if not text.strip():
        raise ValueError('SENTENCE_PLAN_EMPTY_SCRIPT')
    if call is None:
        from .ai_editor import _call
        call = _call
    response = call(PROMPT + text, directory / 'sentence_plan.json')
    return {**manifest, 'segments': validate(text, response), 'sentence_plan_version': 'semantic-v1'}
