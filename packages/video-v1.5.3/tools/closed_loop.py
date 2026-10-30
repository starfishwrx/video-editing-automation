#!/usr/bin/env python3
"""Compatibility entrypoint for tools.workflow.closed_loop.

The maintained implementation owns Eval/Mac gates and environment-bound LARK_BASE_TOKEN.
"""
from __future__ import annotations

from pathlib import Path
import sys

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(_PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_ROOT))

from tools.workflow import closed_loop as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
elif __name__ in sys.modules:
    # One module object keeps imported entrypoints and monkeypatches consistent.
    sys.modules[__name__] = _implementation
else:
    # An unregistered importlib spec deliberately creates an isolated namespace.
    # Execute the single maintained source there so its globals remain patchable.
    __file__ = _implementation.__file__
    exec(compile(Path(__file__).read_text(encoding="utf-8"), __file__, "exec"), globals())
