"""Compatibility import for :mod:`orchestrator.quality.visual_match`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "quality.visual_match", globals())
