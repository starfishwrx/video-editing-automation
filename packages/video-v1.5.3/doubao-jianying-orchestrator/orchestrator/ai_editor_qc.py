"""Compatibility import for :mod:`orchestrator.quality.ai_editor_qc`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "quality.ai_editor_qc", globals())
