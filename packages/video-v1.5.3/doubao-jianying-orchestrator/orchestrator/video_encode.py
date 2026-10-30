"""Compatibility import for :mod:`orchestrator.media.video_encode`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "media.video_encode", globals())
