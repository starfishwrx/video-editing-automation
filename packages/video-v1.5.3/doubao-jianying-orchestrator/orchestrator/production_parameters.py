"""Compatibility import for :mod:`orchestrator.policy.production_parameters`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "policy.production_parameters", globals())
