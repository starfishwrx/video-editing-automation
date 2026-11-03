"""Compatibility import for :mod:`orchestrator.quality.draft_safety`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "quality.draft_safety", globals())
