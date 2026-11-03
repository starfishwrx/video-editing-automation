"""Compatibility import for :mod:`orchestrator.audio.audio_captions`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "audio.audio_captions", globals())
