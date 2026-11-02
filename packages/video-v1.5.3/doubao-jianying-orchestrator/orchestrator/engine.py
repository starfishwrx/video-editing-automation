"""Compatibility import for :mod:`orchestrator.application.engine`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "application.engine", globals())
