"""Compatibility import for :mod:`orchestrator.media.stability`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "media.stability", globals())
