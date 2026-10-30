"""Compatibility import for :mod:`orchestrator.selection.ai_editor`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "selection.ai_editor", globals())
