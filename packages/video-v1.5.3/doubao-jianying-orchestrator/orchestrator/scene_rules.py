"""Compatibility import for :mod:`orchestrator.policy.scene_rules`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "policy.scene_rules", globals())
