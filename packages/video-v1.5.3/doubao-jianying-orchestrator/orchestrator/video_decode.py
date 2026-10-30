"""Compatibility import for :mod:`orchestrator.media.video_decode`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "media.video_decode", globals())
