"""Compatibility import for :mod:`orchestrator.selection.shot_notes`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "selection.shot_notes", globals())
