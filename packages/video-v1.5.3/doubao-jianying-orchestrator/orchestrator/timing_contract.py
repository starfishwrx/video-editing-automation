"""Compatibility import for :mod:`orchestrator.selection.timing_contract`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "selection.timing_contract", globals())
