"""Shared-path delivery and explicitly authorized generated-version recovery.

File verification is deliberately separate from Jianying playback/visual QC.
Run from the package root with: uv run --frozen python -m tools.draft_delivery.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile

from tools import nas_portable as transport
from tools.delivery_identity import tree_identity

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def same_path(left, right):
    return os.path.normcase(str(Path(left).resolve())) == os.path.normcase(str(Path(right).resolve()))


def verify_shared(root: Path, reference_root: Path) -> dict:
    """Check literal references, including active timelines, without basename fallback.

    A distribution/recipient copy intentionally still references the main NAS
    directory. Compare its collected bytes with those literal shared files too.
    """
    root, reference_root = Path(root).resolve(), Path(reference_root).resolve()
    docs, refs = transport.inspect(root)
    if not refs:
        raise RuntimeError("SHARED_MEDIA_REFERENCES_EMPTY")
    for reference in refs:
        literal = Path(reference)
        if not literal.is_absolute() or not same_path(literal.parent, reference_root / "media"):
            raise RuntimeError("SHARED_REFERENCE_NOT_ABSOLUTE_OR_OUTSIDE_TARGET: " + reference)
        if not literal.is_file():
            raise RuntimeError("SHARED_LITERAL_MEDIA_MISSING: " + reference)
        collected = root / "media" / literal.name
        if not collected.is_file() or transport.digest(collected) != transport.digest(literal):
            raise RuntimeError("SHARED_MEDIA_BYTES_DIFFER: " + reference)
    info, meta = docs["draft_info.json"], docs["draft_meta_info.json"]
    if (not info.get("id") or meta.get("draft_id") != info["id"]
            or info.get("name") != reference_root.name
            or meta.get("draft_name") != reference_root.name):
        raise RuntimeError("SHARED_REGISTRATION_ID_OR_NAME_MISMATCH")
    for key, expected in (("draft_fold_path", reference_root),
                          ("draft_root_path", reference_root.parent),
                          ("draft_json_file", reference_root / "draft_info.json")):
        if not meta.get(key) or not same_path(meta[key], expected):
            raise RuntimeError("SHARED_REGISTRATION_PATH_MISMATCH: " + key)
    return {"status": "SHARED_MEDIA_VERIFIED", "literal_references": len(refs),
            "reference_root": str(reference_root), "desktop_playback_verified": False,
            "visual_verified": False, "requires_same_NAS_access": True}


def ship_module():
    path = PACKAGE_ROOT / "doubao-jianying-orchestrator/orchestrator/ship_distribute.py"
    spec = importlib.util.spec_from_file_location("draft_delivery_ship", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def publish_shared(source: Path, share: Path, name: str, work: Path) -> dict:
    """Publish NEW shared-path drafts; never alter source or overwrite a target.

    ``work`` must be a new evidence directory outside source and destinations.
    An authenticated NAS connection must already exist. No credentials are read.
    """
    source, share, work = Path(source).resolve(), Path(share).resolve(), Path(work).resolve()
    if not str(share).startswith("\\\\"):
        raise RuntimeError("NAS_UNC_SHARE_REQUIRED")
    if not name or Path(name).name != name or name in (".", "..") or any(c in name for c in '/\\:<>"|?*'):
        raise RuntimeError("SINGLE_DRAFT_NAME_REQUIRED")
    final, distribution = share / "测试产出" / name, share / "测试产出/分发草稿" / name
    for target in (work, final, distribution):
        if target.exists():
            raise RuntimeError("DESTINATION_EXISTS: " + str(target))
        if target.is_relative_to(source) or source.is_relative_to(target):
            raise RuntimeError("SOURCE_DESTINATION_OVERLAP")
    if any(work.is_relative_to(target) or target.is_relative_to(work) for target in (final, distribution)):
        raise RuntimeError("WORK_DESTINATION_OVERLAP")
    # Inspect before creating anything: encrypted current timelines must fail.
    transport.inspect(source)
    before = tree_identity(source)
    work.mkdir(parents=True, exist_ok=False)
    package, staging = work / "transport", work / "staging" / name
    transport.package_draft(source, package)
    transport.import_package(package, staging)
    # Certificates describe the relative transport package, not the new UNC
    # derivative. Only delete these two files inside our newly created staging.
    for filename in (transport.MANIFEST, "nas-import-receipt.json"):
        (staging / filename).unlink(missing_ok=True)
    ship = ship_module()
    previous = os.environ.get("JY_NAS_SHARE")
    try:
        os.environ["JY_NAS_SHARE"] = str(share)
        ship.rewrite(str(staging), "unc")
    finally:
        if previous is None:
            os.environ.pop("JY_NAS_SHARE", None)
        else:
            os.environ["JY_NAS_SHARE"] = previous
    ship.inject_tools(str(staging))
    identity = tree_identity(staging)
    for target in (final, distribution):
        shutil.copytree(staging, target)  # raises on existing target, no merge
        if tree_identity(target)["sha256"] != identity["sha256"]:
            raise RuntimeError("PUBLISHED_TREE_HASH_MISMATCH: " + str(target))
        verify_shared(target, final)
    archive = work / (name + ".zip")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as zipped:
        for file in sorted(staging.rglob("*")):
            if file.is_file():
                zipped.write(file, (Path(name) / file.relative_to(staging)).as_posix())
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip() is not None:
            raise RuntimeError("ZIP_CRC_FAILED")
        zipped.extractall(work / "receiver")  # archive was made locally above
    verify_shared(work / "receiver" / name, final)
    if tree_identity(source)["sha256"] != before["sha256"]:
        raise RuntimeError("SOURCE_CHANGED_DURING_DELIVERY")
    receipt = {"status": "SHARED_DELIVERY_VERIFIED", "source_preserved": True,
               "final": str(final), "distribution": str(distribution), "zip": str(archive),
               "tree_sha256": identity["sha256"], "zip_sha256": transport.digest(archive),
               "relocated_zip_references_verified": True, "recipient_python_required": False,
               **verify_shared(final, final)}
    receipt["status"] = "SHARED_DELIVERY_VERIFIED"
    transport.write_json(work / "delivery-receipt.json", receipt)
    return receipt


def compare_generated(original: dict, restored: dict):
    if original["duration"] != restored["duration"] or any(
            original["canvas_config"][key] != restored["canvas_config"][key]
            for key in ("width", "height")):
        raise RuntimeError("GENERATED_DURATION_OR_CANVAS_CHANGED")
    if original["tracks"] != restored["tracks"] or original["materials"] != restored["materials"]:
        raise RuntimeError("GENERATED_TRACKS_OR_MATERIALS_CHANGED")
    if not restored.get("id") or restored["id"] == original["id"]:
        raise RuntimeError("FRESH_PROJECT_ID_REQUIRED")


def restore_generated(source: Path, task_file: Path, result_file: Path,
                      destination: Path, *, confirmed: bool = False) -> dict:
    """Restore the acknowledged original generated snapshot, NOT encrypted edits.

    The caller confirms no later edits need preserving and that draft_info is
    the original generated snapshot. A matching successful run is also required.
    """
    if not confirmed:
        raise RuntimeError("GENERATED_VERSION_RESTORE_REQUIRES_EXPLICIT_CONFIRMATION")
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise RuntimeError("DESTINATION_EXISTS")
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise RuntimeError("SOURCE_DESTINATION_OVERLAP")
    original = transport.read_json(source / "draft_info.json")
    task, result = transport.read_json(Path(task_file)), transport.read_json(Path(result_file))
    old_name = original.get("name")
    if (not old_name or task.get("manifest", {}).get("draft_name") != old_name
            or result.get("status") != "SUCCESS" or result.get("draft_name") != old_name
            or not isinstance(result.get("total_s"), (int, float))
            or abs(original.get("duration", -1) / 1e6 - result["total_s"]) > 0.001):
        raise RuntimeError("GENERATED_RUN_EVIDENCE_MISMATCH")
    before = tree_identity(source)
    sys.path.insert(0, str(PACKAGE_ROOT / "jianying-editor/scripts"))
    from jy_wrapper import JyProject
    import pyJianYingDraft as draft
    template = draft.ScriptFile.load_template(str(source / "draft_info.json"))
    project = JyProject(destination.name, width=template.width, height=template.height,
                        drafts_root=str(destination.parent), overwrite=False)
    project.script.fps = template.fps
    project.script.maintrack_adsorb = template.maintrack_adsorb
    # Full template containers preserve historical subtitle extras. Importing
    # tracks separately can prune materials and reject old orphan references.
    project.script.imported_tracks = copy.deepcopy(template.imported_tracks)
    project.script.imported_materials = copy.deepcopy(template.imported_materials)
    project.script.duration = template.duration
    project.save()
    saved = transport.read_json(destination / "draft_info.json")
    compare_generated(original, saved)
    transport.inspect(destination)
    if tree_identity(source)["sha256"] != before["sha256"]:
        raise RuntimeError("SOURCE_CHANGED_DURING_RESTORE")
    receipt = {"status": "GENERATED_VERSION_RESTORED", "source_preserved": True,
               "user_authorized_generated_version_restore": True,
               "current_encrypted_editing_session_migrated": False,
               "generated_snapshot_sha256": transport.digest(source / "draft_info.json"),
               "task_sha256": transport.digest(Path(task_file)),
               "result_sha256": transport.digest(Path(result_file)),
               "source_tree_sha256": before["sha256"], "new_project_id": saved["id"],
               "destination": str(destination), "editing_unchanged": True,
               "generated_run_delivery_mode": result.get("delivery_mode"),
               "desktop_playback_verified": False, "visual_verified": False}
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify-shared")
    verify.add_argument("--draft", required=True, type=Path)
    verify.add_argument("--reference-root", required=True, type=Path)
    publish = commands.add_parser("publish-shared")
    publish.add_argument("--source", required=True, type=Path)
    publish.add_argument("--share", required=True, type=Path)
    publish.add_argument("--name", required=True)
    publish.add_argument("--work", required=True, type=Path)
    restore = commands.add_parser("restore-generated")
    for option in ("source", "task", "result", "destination"):
        restore.add_argument("--" + option, required=True, type=Path)
    restore.add_argument("--confirm-generated-version", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "verify-shared":
            receipt = verify_shared(args.draft, args.reference_root)
        elif args.command == "publish-shared":
            receipt = publish_shared(args.source, args.share, args.name, args.work)
        else:
            receipt = restore_generated(args.source, args.task, args.result, args.destination,
                                        confirmed=args.confirm_generated_version)
        print(json.dumps(receipt, ensure_ascii=False))
        return 0
    except (RuntimeError, OSError, ValueError) as exc:
        print(json.dumps({"status": "FAIL", "reason": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
