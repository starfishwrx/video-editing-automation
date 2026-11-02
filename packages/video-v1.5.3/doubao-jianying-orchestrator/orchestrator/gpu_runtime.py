"""Compatibility import for :mod:`orchestrator.media.gpu_runtime`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "media.gpu_runtime", globals())
