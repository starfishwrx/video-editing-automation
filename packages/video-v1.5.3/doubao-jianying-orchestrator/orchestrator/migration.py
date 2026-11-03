"""Compatibility import for :mod:`orchestrator.delivery.migration`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "delivery.migration", globals())
