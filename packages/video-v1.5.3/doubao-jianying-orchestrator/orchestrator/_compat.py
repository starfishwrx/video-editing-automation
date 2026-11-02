"""Keep legacy imports bound to the actual implementation and its module state."""
from __future__ import annotations

import importlib
import runpy
import sys


def alias_module(name: str, target: str, namespace: dict) -> None:
    qualified = "orchestrator." + target
    if name == "__main__":
        # Keep each executable module's original __main__ behavior (some print
        # a report rather than returning an exit code from main()).
        runpy.run_module(qualified, run_name="__main__")
        return
    implementation = importlib.import_module(qualified)
    sys.modules[name] = implementation
    namespace.update(implementation.__dict__)
