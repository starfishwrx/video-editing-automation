"""画面备注：把业务表里「文案片段：画面要求」形式的备注挂到对应句子上（v1.5.2）。

备注只是给选镜的提示，存在段落的 ``shot_note`` 字段里，不写进 ``visual_requirements`` ——
后者会进语义硬闸，自由文本放进去会把整句拦成缺素材。
"""
from __future__ import annotations

import os
import re
from typing import Any

from . import timing_contract

REMARK_FIELD_ENV = "JY_REMARK_FIELD"
DEFAULT_REMARK_FIELD = "画面备注"
_SPLIT = re.compile(r"[\r\n]+|[；;]")
_INDEX_PREFIX = re.compile(r"^\s*(?:第\s*(\d+)\s*句|(\d+)\s*[.、．)）])\s*[:：]?\s*")
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:“”\"'‘’（）()【】\[\]…—\-]+")


def remark_field() -> str:
    return (os.environ.get(REMARK_FIELD_ENV) or DEFAULT_REMARK_FIELD).strip()


def _norm(text: Any) -> str:
    return _PUNCT.sub("", str(text or ""))


def _lines(notes: Any) -> list[str]:
    if isinstance(notes, (list, tuple)):
        out: list[str] = []
        for item in notes:
            if isinstance(item, dict):
                fragment = str(item.get("text") or item.get("fragment") or "").strip()
                note = str(item.get("note") or item.get("requirement") or "").strip()
                if note:
                    out.append(f"{fragment}：{note}" if fragment else note)
            elif str(item or "").strip():
                out.extend(_SPLIT.split(str(item)))
        return [line.strip() for line in out if line and line.strip()]
    return [line.strip() for line in _SPLIT.split(str(notes or "")) if line.strip()]


def parse(notes: Any, sentences: list[str]) -> dict[str, Any]:
    """返回 ``{"by_index": {1: "..."}, "general": [...], "unmatched": [...]}``（句号从 1 开始）。"""
    normalized = [_norm(s) for s in sentences]
    by_index: dict[int, list[str]] = {}
    general: list[str] = []
    unmatched: list[str] = []
    for line in _lines(notes):
        index = None
        match = _INDEX_PREFIX.match(line)
        body = line
        if match:
            index = int(match.group(1) or match.group(2))
            body = line[match.end():].strip()
            if 1 <= index <= len(sentences) and body:
                by_index.setdefault(index, []).append(body)
            else:
                unmatched.append(line)
            continue
        if "：" in body or ":" in body:
            fragment, requirement = re.split(r"[:：]", body, maxsplit=1)
            key = _norm(fragment)
            requirement = requirement.strip()
            if not requirement:
                continue
            hit = next((i for i, s in enumerate(normalized, 1) if key and key in s), None)
            if hit is None:
                unmatched.append(line)
            else:
                by_index.setdefault(hit, []).append(requirement)
            continue
        general.append(body)
    return {"by_index": {i: "；".join(v) for i, v in by_index.items()},
            "general": general, "unmatched": unmatched}


def _notes_of(manifest: dict[str, Any]) -> Any:
    for key in ("shot_notes", remark_field()):
        value = manifest.get(key)
        if value:
            return value
    return None


def apply(manifest: dict[str, Any]) -> dict[str, Any]:
    notes = _notes_of(manifest)
    if not notes:
        return manifest
    out = dict(manifest)
    segments = out.get("segments")
    if isinstance(segments, list) and segments:
        sentences = [timing_contract.sentence_text(s) for s in segments]
    else:
        sentences = [timing_contract.sentence_text(s) for s in timing_contract.split_sentences(out)]
    parsed = parse(notes, sentences)
    if isinstance(segments, list) and segments:
        new_segments = []
        for i, seg in enumerate(segments, 1):
            note = parsed["by_index"].get(i)
            if note and isinstance(seg, dict):
                seg = {**seg, "shot_note": note}
            new_segments.append(seg)
        out["segments"] = new_segments
    out["shot_notes_by_index"] = {str(i): v for i, v in parsed["by_index"].items()}
    out["shot_notes_general"] = parsed["general"]
    out["shot_notes_unmatched"] = parsed["unmatched"]
    return out


def note_for(manifest: dict[str, Any], index: int, segment: Any = None) -> str:
    if isinstance(segment, dict) and segment.get("shot_note"):
        return str(segment["shot_note"])
    return str((manifest.get("shot_notes_by_index") or {}).get(str(index)) or "")
