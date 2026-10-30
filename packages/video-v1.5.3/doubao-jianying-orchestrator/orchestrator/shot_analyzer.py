"""Compatibility import for :mod:`orchestrator.selection.shot_analyzer`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "selection.shot_analyzer", globals())
