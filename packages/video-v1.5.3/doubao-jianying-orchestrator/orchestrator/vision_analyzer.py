"""Compatibility import for :mod:`orchestrator.vision.vision_analyzer`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "vision.vision_analyzer", globals())
