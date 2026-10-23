"""Read-only diagnosis of delivery runtime files across package roots."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


PIPELINE_FILES = (
    "batch_worker.py",
    "closed_loop.py",
    "tools/closed_loop.py",
    "tools/draft_delivery.py",
    "tools/nas_portable.py",
    "tools/delivery_identity.py",
    "tools/delivery_runtime.py",
    "tools/production_eval.py",
    "tools/eval_stage.py",
    "tools/mac_review_handoff.py",
    "doubao-jianying-orchestrator/orchestrator/visual_qc_contract.py",
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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_ship_dependencies(root: Path) -> None:
    """Raise a clear error when this root cannot run its bundled ship entrypoint.

    This intentionally checks only files under ``root``. It never searches
    other sys.path entries or site-packages for similarly named modules.
    """
    root = Path(root)
    missing = [relative for relative in SHIP_FILES if not (root / relative).is_file()]
    if missing:
        raise RuntimeError("SHIP_DEPENDENCIES_MISSING: " + ", ".join(missing))


def _read_candidate(package: Path) -> tuple[str | None, dict[str, str], list[str]]:
    issues: list[str] = []
    try:
        version = (package / "VERSION").read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        version = None
        issues.append("PACKAGE_VERSION_MISSING")
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
    declared = {
        str(row.get("path")): str(row.get("sha256", "")).lower()
        for row in manifest_files
        if isinstance(row, dict) and row.get("path") and row.get("sha256")
    }
    return version, declared, issues


def _file_check(root: Path, relative: str, expected: str | None, *, source_ok: bool = True) -> dict[str, Any]:
    path = Path(root) / Path(relative)
    actual = sha256(path) if path.is_file() else None
    if actual is None:
        status = "missing"
    elif expected is None:
        status = "manifest_entry_missing"
    elif not source_ok:
        status = "package_source_drift"
    elif actual != expected:
        status = "hash_mismatch"
    else:
        status = "ok"
    return {"path": relative, "status": status, "expected_sha256": expected,
            "actual_sha256": actual}


def diagnose(package: Path, pipeline: Path, skills: Path) -> dict[str, Any]:
    """Compare declared candidate bytes with the two actual execution roots."""
    package, pipeline, skills = (Path(p).resolve() for p in (package, pipeline, skills))
    version, declared, package_issues = _read_candidate(package)
    critical = sorted(set(PIPELINE_FILES) | set(SHIP_FILES))
    package_checks = []
    source_ok: dict[str, bool] = {}
    issues = list(package_issues)
    for relative in critical:
        expected = declared.get(relative)
        check = _file_check(package, relative, expected)
        package_checks.append(check)
        good = check["status"] == "ok"
        source_ok[relative] = good
        if not good:
            issues.append("PACKAGE_" + check["status"].upper() + ":" + relative)

    runtimes: dict[str, Any] = {"package": {"version": version, "files": package_checks}}
    for label, root, required in (
        ("pipeline", pipeline, PIPELINE_FILES),
        ("skills", skills, SHIP_FILES),
    ):
        try:
            runtime_version = (root / "VERSION").read_text(encoding="utf-8-sig").strip()
            version_status = "ok" if runtime_version == version else "version_mismatch"
        except (OSError, UnicodeError):
            runtime_version, version_status = None, "missing"
        files = [_file_check(root, relative, declared.get(relative),
                             source_ok=source_ok.get(relative, False))
                 for relative in required]
        runtimes[label] = {"root": str(root), "version": runtime_version,
                           "version_status": version_status, "files": files}
        if version_status != "ok":
            issues.append(label.upper() + "_VERSION_" + version_status.upper())
        for check in files:
            if check["status"] != "ok":
                issues.append(label.upper() + "_" + check["status"].upper() + ":" + check["path"])
    return {"status": "OK" if not issues else "BLOCKED", "package_root": str(package),
            "package_version": version, "runtimes": runtimes, "issues": issues}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--pipeline", required=True, type=Path)
    parser.add_argument("--skills", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = diagnose(args.package, args.pipeline, args.skills)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        result = {"status": "BLOCKED", "issues": ["DOCTOR_INPUT_INVALID: " + str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
