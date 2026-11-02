"""Compatibility import for :mod:`orchestrator.runtime.json_extract`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "runtime.json_extract", globals())
