"""Compatibility import for :mod:`orchestrator.selection.sentence_planner`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "selection.sentence_planner", globals())
