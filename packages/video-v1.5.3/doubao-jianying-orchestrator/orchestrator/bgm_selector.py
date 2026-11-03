"""Compatibility import for :mod:`orchestrator.audio.bgm_selector`.

New implementation belongs to the feature package.
"""
from ._compat import alias_module

alias_module(__name__, "audio.bgm_selector", globals())
