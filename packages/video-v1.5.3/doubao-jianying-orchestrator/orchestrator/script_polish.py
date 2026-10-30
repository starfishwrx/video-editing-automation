"""Compatibility import for :mod:`orchestrator.policy.script_polish`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "policy.script_polish", globals())
