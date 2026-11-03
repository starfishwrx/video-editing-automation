"""Compatibility import for :mod:`orchestrator.audio.subtitle_units`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "audio.subtitle_units", globals())
