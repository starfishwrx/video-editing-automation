"""Compatibility import for :mod:`orchestrator.policy.feedback_policy`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "policy.feedback_policy", globals())
