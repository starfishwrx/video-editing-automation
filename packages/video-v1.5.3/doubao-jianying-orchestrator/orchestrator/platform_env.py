"""Compatibility import for :mod:`orchestrator.runtime.platform_env`.

New implementation belongs to the feature package.
"""
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "orchestrator"
from ._compat import alias_module

alias_module(__name__, "runtime.platform_env", globals())
