"""Compatibility import for :mod:`orchestrator.audio.voice_tts`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "audio.voice_tts", globals())
