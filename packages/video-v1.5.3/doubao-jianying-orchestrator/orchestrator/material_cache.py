"""Compatibility import for :mod:`orchestrator.vision.material_cache`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "vision.material_cache", globals())
