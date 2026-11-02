"""Compatibility import for :mod:`orchestrator.runtime.file_safety`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "runtime.file_safety", globals())
