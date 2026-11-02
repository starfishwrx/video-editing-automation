"""Compatibility import for :mod:`orchestrator.quality.diagnostics`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "quality.diagnostics", globals())
