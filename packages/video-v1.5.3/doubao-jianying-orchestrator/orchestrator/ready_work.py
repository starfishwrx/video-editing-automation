"""Compatibility import for :mod:`orchestrator.runtime.ready_work`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "runtime.ready_work", globals())
