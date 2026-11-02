"""Compatibility import for :mod:`orchestrator.media.ffmpeg_bootstrap`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "media.ffmpeg_bootstrap", globals())
