"""Normalize the operator's voice and tempo fields without losing their originals."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

from .voice_catalog import VoicePreset, resolve_voice

_SUFFIX = re.compile(r"^(?P<name>.*?)[\s·]*(?P<tempo>\d+(?:\.\d+)?)\s*倍速\s*$")


def validate_tempo(value: Any) -> float:
    """Validate and normalize a narration tempo within the established 0.5-2 range."""
    if value is None:
        return 1.0
    try:
        tempo = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"无效的配音倍速: {value!r}") from exc
    if isinstance(value, bool) or not math.isfinite(tempo) or not 0.5 <= tempo <= 2.0:
        raise ValueError(f"无效的配音倍速: {value!r}")
    return round(tempo, 3)


def select_voice_tempo(voice: Any, *, voice_tempo: Any = None,
                       category_tempo: Any = 1.0) -> tuple[float, str, float | None]:
    """Select tempo without resolving the catalog identity (useful for defaults)."""
    name_tempo = None
    if isinstance(voice, str):
        match = _SUFFIX.match(voice.strip())
        if match:
            if not match.group("name").strip():
                raise ValueError("倍速后缀缺少音色名称")
            name_tempo = validate_tempo(match.group("tempo"))
    elif isinstance(voice, dict) and voice.get("speed") not in (None, ""):
        name_tempo = validate_tempo(voice["speed"])
    if voice_tempo is not None:
        return validate_tempo(voice_tempo), "voice_tempo", name_tempo
    if name_tempo is not None:
        return name_tempo, "voice_name", name_tempo
    return validate_tempo(category_tempo), "category_default", None


@dataclass(frozen=True)
class VoiceRequest:
    original_voice: Any
    voice_name: Any
    preset: VoicePreset
    named_tempo: float | None
    tempo: float
    tempo_source: str


def resolve_voice_request(voice: Any, *, voice_tempo: Any = None,
                          category_tempo: Any = 1.0) -> VoiceRequest:
    """Resolve the voice identity first, then select one (non-multiplicative) tempo."""
    name = voice
    named_tempo = None
    if isinstance(voice, str):
        match = _SUFFIX.match(voice.strip())
        if match:
            name = match.group("name").strip()
            if not name:
                raise ValueError("倍速后缀缺少音色名称")
            named_tempo = validate_tempo(match.group("tempo"))
    elif isinstance(voice, dict):
        name = dict(voice)
        if "speed" in voice and voice["speed"] not in (None, ""):
            named_tempo = validate_tempo(voice["speed"])
        # Resolve identity independently of the tempo field.
        name.pop("speed", None)

    preset = resolve_voice(name)
    tempo, source, selected_name_tempo = select_voice_tempo(
        voice, voice_tempo=voice_tempo, category_tempo=category_tempo)
    named_tempo = selected_name_tempo
    return VoiceRequest(voice, name, preset, named_tempo, tempo, source)
