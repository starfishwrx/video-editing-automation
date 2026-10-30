"""Compatibility import for :mod:`orchestrator.runtime.resource_profile`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "runtime.resource_profile", globals())
