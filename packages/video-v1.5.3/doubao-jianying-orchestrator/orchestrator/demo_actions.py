"""Compatibility import for :mod:`orchestrator.policy.demo_actions`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "policy.demo_actions", globals())
