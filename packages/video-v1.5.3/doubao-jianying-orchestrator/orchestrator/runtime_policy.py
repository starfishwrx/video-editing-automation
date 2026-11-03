"""Compatibility import for :mod:`orchestrator.runtime.runtime_policy`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "runtime.runtime_policy", globals())
