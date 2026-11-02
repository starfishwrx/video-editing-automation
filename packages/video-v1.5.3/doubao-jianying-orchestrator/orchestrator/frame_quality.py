"""Compatibility import for :mod:`orchestrator.media.frame_quality`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "media.frame_quality", globals())
