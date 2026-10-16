"""Build and verify a source release without installing it or publishing it.

From the package root: uv run --frozen python -X utf8 -m tools.release_package --help
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import zipfile

from tools.nas_portable import digest, write_json

ROOT = Path(__file__).resolve().parents[1]
METADATA = ("build_manifest.py", "release-manifest.json", "PACKAGE_CONTENTS.sha256")


def builder(root: Path):
    spec = importlib.util.spec_from_file_location("video_release_builder", root / "build_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(root: Path) -> dict:
    root = Path(root).resolve()
    manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    package = json.loads((root / "SKILL_PACKAGE.json").read_text(encoding="utf-8"))
    if manifest["version"] != version or package["version"] != "v" + version:
        raise ValueError("PACKAGE_VERSION_MISMATCH")
    rows = manifest["files"]
    declared = [row["path"] for row in rows]
    if len(set(declared)) != len(rows) or len(rows) != manifest["file_count"]:
        raise ValueError("MANIFEST_FILE_COUNT_INVALID")
    for row in rows:
        relative = PurePosixPath(row["path"])
        if relative.is_absolute() or any(p in {"..", "."} or ":" in p or "\\" in p for p in relative.parts):
            raise ValueError("UNSAFE_MANIFEST_PATH")
        path = root / relative
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError("PACKAGE_SYMLINK_OR_PATH_ESCAPE")
        if not path.is_file() or digest(path) != row["sha256"]:
            raise ValueError("PACKAGE_HASH_MISMATCH: " + row["path"])
    # Verify target files with our maintained inventory policy. Do not execute
    # Python from a directory that is merely being checked.
    inventory = builder(ROOT)
    inventory.ROOT = root
    actual = {path.relative_to(root).as_posix() for path in inventory.files()}
    if actual != set(declared):
        raise ValueError("PACKAGE_INVENTORY_DRIFT")
    checksum_lines = (root / "PACKAGE_CONTENTS.sha256").read_text(encoding="utf-8-sig").splitlines()
    if checksum_lines != [row["sha256"] + "  " + row["path"] for row in rows]:
        raise ValueError("PACKAGE_CHECKSUM_LIST_DRIFT")
    return {"status": "PACKAGE_FILES_VERIFIED", "version": version,
            "file_count": len(rows), "manifest_sha256": digest(root / "release-manifest.json"),
            "declared_source_sha256": manifest["source_sha256"]}


def build(output: Path, checks: str) -> dict:
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("OUTPUT_EXISTS")
    if output.is_relative_to(ROOT) or ROOT.is_relative_to(output):
        raise ValueError("RELEASE_OUTPUT_MUST_BE_OUTSIDE_PACKAGE")
    module = builder(ROOT)
    with redirect_stdout(io.StringIO()):
        module.main()
    source = verify(ROOT)
    metadata_identity = {relative: digest(ROOT / relative) for relative in METADATA}
    manifest = json.loads((ROOT / "release-manifest.json").read_text(encoding="utf-8"))
    output.mkdir(parents=True, exist_ok=False)
    name = "video-v" + source["version"]
    archive = output / (name + ".zip")
    relatives = [row["path"] for row in manifest["files"]] + list(METADATA)
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
        for relative in sorted(relatives):
            zipped.write(ROOT / relative, name + "/" + relative)
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            raise ValueError("ZIP_CRC_FAILED")
        zipped.extractall(output / "clean-unpack")  # created locally above
    fresh = output / "clean-unpack" / name
    received = verify(fresh)
    if received != source:
        raise ValueError("UNPACKED_PACKAGE_IDENTITY_MISMATCH")
    for relative in METADATA:
        if digest(ROOT / relative) != digest(fresh / relative):
            raise ValueError("UNPACKED_RELEASE_METADATA_MISMATCH")
    if checks == "full":
        suites = [("package", "tests", "test_*.py"),
                  ("orchestrator", "doubao-jianying-orchestrator/tests", "test_*.py")]
    else:
        suites = [("delivery", "tests", "test_draft_delivery.py"),
                  ("transport", "tests", "test_nas_portable.py")]
    logs = []
    for label, directory, pattern in suites:
        log = output / (label + "-tests.log")
        with log.open("w", encoding="utf-8") as stream:
            result = subprocess.run([sys.executable, "-X", "utf8", "-m", "unittest", "discover",
                                     "-s", directory, "-p", pattern, "-v"], cwd=fresh,
                                    stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError("RELEASE_TESTS_FAILED: " + label + "; see " + str(log))
        logs.append(log.name)
    # Detect concurrent edits while the extracted package was being tested.
    if verify(ROOT) != source:
        raise ValueError("SOURCE_CHANGED_DURING_RELEASE_CHECK")
    if metadata_identity != {relative: digest(ROOT / relative) for relative in METADATA}:
        raise ValueError("RELEASE_METADATA_CHANGED_DURING_CHECK")
    receipt = {"status": "SOURCE_RELEASE_VERIFIED", **source, "checks": checks,
               "zip": archive.name, "zip_sha256": digest(archive), "test_logs": logs,
               "clean_unpack_verified": True, "deployed": False,
               "production_queue_modified": False, "visual_verified": False}
    receipt["status"] = "SOURCE_RELEASE_VERIFIED"
    (output / ("SHA256SUMS-" + name + ".txt")).write_text(
        receipt["zip_sha256"] + "  " + archive.name + "\n", encoding="utf-8")
    write_json(output / "release-receipt.json", receipt)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("verify", help="verify an existing manifest without changing files")
    check.add_argument("--package", type=Path, default=ROOT)
    pack = commands.add_parser("build", help="build, unzip and test in a new external output directory")
    pack.add_argument("--output", type=Path, required=True)
    pack.add_argument("--checks", choices=("delivery", "full"), default="delivery")
    args = parser.parse_args()
    try:
        receipt = verify(args.package) if args.command == "verify" else build(args.output, args.checks)
        print(json.dumps(receipt, ensure_ascii=False))
        return 0
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
