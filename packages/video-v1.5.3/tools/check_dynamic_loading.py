"""Inventory known dynamic-loading adapters without importing production code.

This is an architecture drift check, not a Python sandbox or target-path proof.
"""
from __future__ import annotations

import ast
from collections import Counter
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INVENTORY = Path(__file__).with_name("dynamic_loading_adapters.json")
EXCLUDED = {"tests", "vendor", "__pycache__", ".venv", ".git", ".uv-cache"}
APIS = {
    "__import__", "builtins.__import__", "exec", "builtins.exec", "eval", "builtins.eval", "importlib.import_module",
    "importlib.util.spec_from_file_location", "importlib.util.spec_from_loader",
    "importlib.machinery.SourceFileLoader", "importlib.machinery.SourcelessFileLoader",
    "importlib.machinery.ExtensionFileLoader", "runpy.run_module", "runpy.run_path",
}


def calls(source: str) -> Counter:
    tree = ast.parse(source)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases.setdefault(alias.asname or alias.name.split('.')[0], set()).add(alias.name if alias.asname else alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            for alias in node.names:
                aliases.setdefault(alias.asname or alias.name, set()).add((node.module or '') + '.' + alias.name)

    def qualified(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, {node.id})
        if isinstance(node, ast.Attribute):
            prefix = qualified(node.value)
            return {name + '.' + node.attr for name in prefix}
        return set()

    found = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        names = qualified(node.func) & APIS
        for name in names:
            found[name] += 1
        if not names and isinstance(node.func, ast.Attribute) and node.func.attr in {"exec_module", "load_module"}:
            found['loader.' + node.func.attr] += 1
    return found


def scan(root: Path) -> dict[str, Counter]:
    result = {}
    for path in sorted(root.rglob('*.py')):
        relative = path.relative_to(root)
        if any(part in EXCLUDED for part in relative.parts):
            continue
        found = calls(path.read_text(encoding='utf-8-sig'))
        if found:
            result[relative.as_posix()] = found
    return result


def check(root: Path = ROOT, inventory: Path = INVENTORY) -> list[str]:
    records = json.loads(inventory.read_text(encoding='utf-8'))['adapters']
    expected = {}
    issues = []
    for row in records:
        path = row['path']
        if path in expected or not row.get('purpose', '').strip() or not row.get('verification', '').strip():
            issues.append('DYNAMIC_INVENTORY_INVALID:' + path)
        expected[path] = Counter(row['calls'])
    actual = scan(root)
    for path in sorted(actual.keys() | expected.keys()):
        if actual.get(path, Counter()) != expected.get(path, Counter()):
            issues.append('DYNAMIC_LOADING_CHANGED:' + path)
    return issues


def main() -> int:
    issues = check()
    print(json.dumps({'status': 'FAIL' if issues else 'PASS', 'issues': issues}, ensure_ascii=False))
    return bool(issues)


if __name__ == '__main__':
    raise SystemExit(main())
