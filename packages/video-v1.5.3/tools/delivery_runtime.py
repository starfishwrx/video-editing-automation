"""Read-only diagnosis of delivery runtime files across package roots."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from typing import Any


PIPELINE_FILES = (
    "batch_worker.py",
    "closed_loop.py",
    "tools/closed_loop.py",
    "tools/workflow/__init__.py",
    "tools/workflow/closed_loop.py",
    "tools/queueing/__init__.py",
    "tools/queueing/contracts.py",
    "tools/draft_delivery.py",
    "tools/nas_portable.py",
    "tools/delivery_identity.py",
    "tools/delivery_runtime.py",
    "tools/production_eval.py",
    "tools/acceptance/__init__.py",
    "tools/acceptance/evaluation.py",
    "tools/acceptance/task_review.py",
    "tools/acceptance/delivery_gate.py",
    "tools/acceptance/finalization.py",
    "tools/workflow/scheduled.py",
    "scripts/run_pipeline.ps1",
    "scripts/install_pipeline_task.ps1",
    "doubao-jianying-orchestrator/orchestrator/visual_qc_contract.py",
    "doubao-jianying-orchestrator/orchestrator/runtime/vision_config.py",
)

SHIP_FILES = (
    "doubao-jianying-orchestrator/orchestrator/ship_distribute.py",
    "tools/draft_delivery.py",
    "tools/nas_portable.py",
    "tools/delivery_identity.py",
    "tools/delivery_runtime.py",
    "nas-tools/素材本地化修复.bat",
    "nas-tools/fix_local.ps1",
    "nas-tools/一键取稿.bat",
    "nas-tools/上传草稿.bat",
    "nas-tools/nas-tool.ps1",
)


def require_ship_dependencies(root: Path) -> None:
    """Raise a clear error when this root cannot run its bundled ship entrypoint.

    This intentionally checks only files under ``root``. It never searches
    other sys.path entries or site-packages for similarly named modules.
    """
    root = Path(root)
    missing = [relative for relative in SHIP_FILES if not (root / relative).is_file()]
    if missing:
        raise RuntimeError("SHIP_DEPENDENCIES_MISSING: " + ", ".join(missing))


def _safe_inventory_path(value: object) -> bool:
    if not isinstance(value, str) or not value or re.search(r'[\\:*?"<>|\x00-\x1f]', value):
        return False
    parts = value.split('/')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                *(f'LPT{i}' for i in range(1, 10))}
    return all(p not in {'', '.', '..'} and p == p.strip().rstrip('.')
               and p.split('.')[0].upper() not in reserved for p in parts)


def _read_candidate(package: Path, *, all_declared: bool = False) -> tuple[str | None, set[str], list[str]]:
    issues: list[str] = []
    if all_declared and any((package / name).is_symlink()
                            or not (package / name).resolve().is_relative_to(package)
                            for name in ('VERSION', 'release-manifest.json')):
        return None, set(), ['PACKAGE_METADATA_PATH_ESCAPE_OR_SYMLINK']
    try:
        version = (package / "VERSION").read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        version = None
        issues.append("PACKAGE_VERSION_MISSING")
    if all_declared and not version and 'PACKAGE_VERSION_MISSING' not in issues:
        issues.append('PACKAGE_VERSION_MISSING')
    try:
        manifest = json.loads((package / "release-manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError, TypeError):
        manifest = {}
        issues.append("PACKAGE_RELEASE_MANIFEST_INVALID")
    if not isinstance(manifest, dict):
        manifest = {}
        if "PACKAGE_RELEASE_MANIFEST_INVALID" not in issues:
            issues.append("PACKAGE_RELEASE_MANIFEST_INVALID")
    manifest_version = str(manifest.get("version") or "")
    if version and manifest_version != version:
        issues.append("PACKAGE_VERSION_MANIFEST_MISMATCH")
    manifest_files = manifest.get("files", [])
    if not isinstance(manifest_files, list):
        manifest_files = []
        issues.append("PACKAGE_RELEASE_MANIFEST_FILES_INVALID")
    declared = set()
    seen = set()
    for row in manifest_files:
        value = row.get('path') if isinstance(row, dict) else None
        if all_declared:
            if not _safe_inventory_path(value):
                issues.append('PACKAGE_UNSAFE_OR_INVALID_MANIFEST_PATH')
                continue
            if value.casefold() in seen:
                issues.append('PACKAGE_DUPLICATE_MANIFEST_PATH:' + value)
            seen.add(value.casefold())
        if value:
            declared.add(str(value))
    if all_declared and (not manifest_files or type(manifest.get('file_count')) is not int
                         or manifest.get('file_count') != len(manifest_files)):
        issues.append('PACKAGE_MANIFEST_FILE_COUNT_INVALID')
    return version, declared, issues


def _file_check(root: Path, relative: str, source: Path | None, *, confined: bool = False) -> dict[str, Any]:
    path = root / relative
    if confined and (path.is_symlink() or not path.resolve().is_relative_to(root)):
        status = 'path_escape_or_symlink'
    elif not path.is_file():
        status = "missing"
    elif source is None:
        status = "manifest_entry_missing"
    elif not source.is_file():
        status = "package_source_missing"
    elif path != source and path.read_bytes() != source.read_bytes():
        status = "content_mismatch"
    else:
        status = "ok"
    return {"path": relative, "status": status}


def diagnose(package: Path, pipeline: Path, skills: Path, *, all_declared: bool = False) -> dict[str, Any]:
    """Compare required source files directly; no frozen per-file hash manifest."""
    package, pipeline, skills = (Path(p).resolve() for p in (package, pipeline, skills))
    version, declared, package_issues = _read_candidate(package, all_declared=all_declared)
    critical = sorted(set(PIPELINE_FILES) | set(SHIP_FILES) | (declared if all_declared else set()))
    package_checks = []
    issues = list(package_issues)
    for relative in critical:
        expected = package / relative if relative in declared else None
        check = _file_check(package, relative, expected, confined=all_declared)
        package_checks.append(check)
        good = check["status"] == "ok"
        if not good:
            issues.append("PACKAGE_" + check["status"].upper() + ":" + relative)

    runtimes: dict[str, Any] = {"package": {"version": version, "files": package_checks}}
    for label, root, required in (
        ("pipeline", pipeline, PIPELINE_FILES),
        ("skills", skills, SHIP_FILES),
    ):
        try:
            if all_declared and ((root / 'VERSION').is_symlink()
                                 or not (root / 'VERSION').resolve().is_relative_to(root)):
                raise ValueError('VERSION_PATH_ESCAPE_OR_SYMLINK')
            runtime_version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
            version_status = "ok" if runtime_version == version else "version_mismatch"
        except ValueError:
            runtime_version, version_status = None, 'path_escape_or_symlink'
        except (OSError, UnicodeError):
            runtime_version, version_status = None, "missing"
        safe_sources = ({item['path'] for item in package_checks if item['status'] == 'ok'}
                        if all_declared else declared)
        files = [_file_check(root, relative, package / relative if relative in safe_sources else None,
                             confined=all_declared)
                 for relative in (critical if all_declared else required)]
        runtimes[label] = {"root": str(root), "version": runtime_version,
                           "version_status": version_status, "files": files}
        if version_status != "ok":
            issues.append(label.upper() + "_VERSION_" + version_status.upper())
        for check in files:
            if check["status"] != "ok":
                issues.append(label.upper() + "_" + check["status"].upper() + ":" + check["path"])
    return {"status": "OK" if not issues else "BLOCKED", "package_root": str(package),
            "package_version": version, "scope": "all-declared" if all_declared else "delivery",
            "runtimes": runtimes, "issues": issues}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--pipeline", required=True, type=Path)
    parser.add_argument("--skills", required=True, type=Path)
    parser.add_argument("--all-declared", action="store_true",
                        help="Compare every declared package file on both installation faces; read-only")
    args = parser.parse_args(argv)
    try:
        result = diagnose(args.package, args.pipeline, args.skills, all_declared=args.all_declared)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        result = {"status": "BLOCKED", "issues": ["DOCTOR_INPUT_INVALID: " + str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
