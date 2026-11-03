"""Compatibility import for :mod:`orchestrator.runtime.proc_slots`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "runtime.proc_slots", globals())
