"""Compatibility import for :mod:`orchestrator.runtime.task_log`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "runtime.task_log", globals())
