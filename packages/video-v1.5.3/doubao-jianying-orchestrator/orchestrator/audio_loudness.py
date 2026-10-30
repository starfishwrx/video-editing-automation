"""Compatibility import for :mod:`orchestrator.audio.audio_loudness`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "audio.audio_loudness", globals())
