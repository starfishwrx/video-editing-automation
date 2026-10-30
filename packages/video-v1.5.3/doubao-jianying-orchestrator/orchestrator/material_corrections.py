"""Compatibility import for :mod:`orchestrator.vision.material_corrections`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "vision.material_corrections", globals())
