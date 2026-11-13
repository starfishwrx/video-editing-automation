"""Check feature boundaries offline, without importing the video runtime."""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

from tools import check_dynamic_loading

ROOT = Path(__file__).resolve().parents[1] / "doubao-jianying-orchestrator" / "orchestrator"
RUNNABLE = {"cli", "draft_visual_qc", "legacy_cache_import", "platform_env"}
ROOT_MODULES = {"__init__", "_compat", "paths"}


def imports(tree: ast.AST, package: str):
    """Include local imports inside functions; lazy edges still cross boundaries."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level:
                name = importlib.util.resolve_name("." * node.level + name, package)
            yield name
            # `from orchestrator import cli` is also an edge to cli.
            yield from (name + "." + alias.name for alias in node.names)


def _wrapper(name: str, target: str) -> str:
    bootstrap = ""
    if name in RUNNABLE:
        bootstrap = ('if __package__ in (None, ""):\n'
                     '    import sys\n    from pathlib import Path\n'
                     '    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n'
                     '    __package__ = "orchestrator"\n')
    return bootstrap + f'from ._compat import alias_module\nalias_module(__name__, "{target}", globals())\n'


def _tool_boundaries(root: Path, legacy_modules: set[str]) -> list[str]:
    issues = []
    for feature in ("queueing", "acceptance", "workflow"):
        for path in sorted((root / feature).rglob("*.py")):
            relative = path.relative_to(root)
            if "__pycache__" in relative.parts:
                continue
            package = "tools." + ".".join(relative.parts[:-1])
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for target in imports(tree, package):
                parts = target.split(".")
                worker = parts[0] == "batch_worker"
                tool = parts[1] if parts[0] == "tools" and len(parts) > 1 else None
                application = target == "orchestrator.application" or target.startswith("orchestrator.application.")
                if parts[0] == "orchestrator" and len(parts) > 1:
                    application |= parts[1] == "OrchestrationEngine" or parts[1] in legacy_modules
                forbidden = application
                if feature == "queueing":
                    forbidden |= worker or (tool is not None and tool != "queueing") or parts[0] == "orchestrator"
                elif feature == "acceptance":
                    forbidden |= worker or tool in {"workflow", "batch_dispatch", "closed_loop"}
                if forbidden:
                    issues.append(f"TOOLS_DEPENDENCY:{relative.as_posix()}:{target}")
    return issues


def check(root: Path = ROOT, *, tools_root: Path | None = None) -> list[str]:
    layout = json.loads((root / "module_map.json").read_text(encoding="utf-8"))
    mapping = {name: feature + "." + name for feature, names in layout["features"].items() for name in names}
    issues = []
    allowed_root = ROOT_MODULES | set(mapping) | set(layout["anchored"])
    for path in sorted(root.glob("*.py")):
        if path.stem not in allowed_root:
            issues.append(f"ROOT_IMPLEMENTATION_FORBIDDEN:{path.name}")
    for name, target in mapping.items():
        facade = root / (name + ".py")
        if not facade.is_file() or not (root / (target.replace(".", "/") + ".py")).is_file():
            issues.append(f"MODULE_MISSING:{name}")
            continue
        tree = ast.parse(facade.read_text(encoding="utf-8"))
        # Documentation is free to change; executable facade shape is fixed.
        if ast.get_docstring(tree) is not None:
            tree.body.pop(0)
        if ast.dump(tree) != ast.dump(ast.parse(_wrapper(name, target))):
            issues.append(f"LEGACY_ENTRYPOINT_HAS_LOGIC:{name}")
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts:
            continue
        anchored = len(relative.parts) == 1
        if anchored and path.stem not in layout["anchored"]:
            continue
        feature = "contract" if anchored else relative.parts[0]
        package = "orchestrator" + ("." + ".".join(relative.parts[:-1]) if not anchored else "")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for target in imports(tree, package):
            parts = target.split(".")
            if (anchored and path.stem != "ship_distribute"
                    and parts[0] in {"tools", "batch_worker"}):
                issues.append(f"PUBLIC_CONTRACT_DEPENDENCY:{relative.as_posix()}:{target}")
            if parts[0] != "orchestrator" or len(parts) < 2:
                continue
            dependency = parts[1]
            if dependency in mapping:
                issues.append(f"LEGACY_IMPORT:{relative.as_posix()}:{target}")
            if feature != "application" and dependency in {"application", "OrchestrationEngine"}:
                issues.append(f"APPLICATION_DEPENDENCY:{relative.as_posix()}:{target}")
            if feature in {"runtime", "media", "domain"} and dependency in {
                "selection", "vision", "policy", "quality", "audio", "delivery",
            }:
                issues.append(f"FOUNDATION_DEPENDENCY:{relative.as_posix()}:{target}")
            if feature == "domain" and dependency != "domain":
                issues.append(f"DOMAIN_DEPENDENCY:{relative.as_posix()}:{target}")
    issues.extend(_tool_boundaries(
        tools_root if tools_root is not None else root.parent.parent / "tools", set(mapping)))
    if root.resolve() == ROOT.resolve():
        issues.extend(check_dynamic_loading.check())
    return sorted(set(issues))


def main() -> int:
    issues = check()
    print(json.dumps({"status": "FAIL" if issues else "PASS", "issues": issues}, ensure_ascii=False))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
