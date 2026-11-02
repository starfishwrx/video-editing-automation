"""Compatibility import for :mod:`orchestrator.vision.phase_timing`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "vision.phase_timing", globals())
