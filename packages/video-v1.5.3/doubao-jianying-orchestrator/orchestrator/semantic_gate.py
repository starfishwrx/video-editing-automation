"""Compatibility import for :mod:`orchestrator.policy.semantic_gate`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "policy.semantic_gate", globals())
