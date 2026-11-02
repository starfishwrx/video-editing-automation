"""Explicit NAS review-copy delivery through the user-selected headless backend.

This never calls the formal finalizer or marks the business task complete.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from tools.delivery_identity import tree_identity
from tools.draft_delivery import publish_tree

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "doubao-jianying-orchestrator"))


def deliver_review(build, headless_root, timeline_path, report_dir, *, publish=False):
    from orchestrator.domain.timeline import EditTimeline
    from orchestrator.delivery.jianying_export import headless_plan
    from orchestrator.quality.timeline_export import compare_headless_documents
    from orchestrator.media.timeline_render import timeline_identity
    root, build, report = [Path(p).resolve() for p in (headless_root, build, report_dir)]
    if report.exists():
        raise ValueError("REVIEW_REPORT_EXISTS")
    if json.loads((root / "project.json").read_text(encoding="utf-8")).get("id") != "jianying-headless":
        raise ValueError("HEADLESS_PROJECT_IDENTITY_INVALID")
    sys.path.insert(0, str(root / "engine"))
    import jy14_headless as backend
    runtime = backend.nd.helper()._validate_runtime_environment()
    record = backend.verify_build(build)
    if (not record.get("detached_root") or record.get("delivery_mode") != "preview"
            or record.get("formal_delivery_allowed") is not False
            or runtime["runtime_profile"] != record["runtime_profile"]):
        raise ValueError("DETACHED_REVIEW_BUILD_REQUIRED")
    timeline = EditTimeline.load(timeline_path)
    plan = backend.read_json(build / "plan.json")
    subtitles = [t for t in plan["tracks"] if t["type"] == "text"]
    if not subtitles:
        raise ValueError("REVIEW_FONT_REQUIRED")
    expected = headless_plan(timeline, record["name"], subtitles[0]["segments"][0]["font_path"], review_only=True)
    if plan != expected:
        raise ValueError("HEADLESS_PLAN_TIMELINE_MISMATCH")
    target = Path(record["target"])
    distribution = target.parent / "分发草稿" / target.name
    if any(report.is_relative_to(p) or p.is_relative_to(report)
           for p in (build, target, distribution, Path(timeline_path).resolve())):
        raise ValueError("REVIEW_REPORT_DESTINATION_OVERLAP")
    codec = backend.nd.helper()

    def verify(folder, *, shared):
        if backend.files_manifest(folder) != record["files"]:
            raise ValueError("HEADLESS_DRAFT_FILES_CHANGED")
        document = codec._decrypt_metadata_in_memory(folder / backend.nd.TIMELINE_FILE)
        metadata = codec._decrypt_metadata_in_memory(folder / "draft_meta_info.json")
        # Read every current mirror through the native codec. Never substitute
        # an old plaintext sidecar for an unreadable active timeline.
        mirrors = [folder / "template-2.tmp", folder / (backend.nd.TIMELINE_FILE + ".bak")]
        active = folder / "Timelines" / record["timeline_id"]
        mirrors += [active / backend.nd.TIMELINE_FILE, active / "template-2.tmp",
                    active / (backend.nd.TIMELINE_FILE + ".bak")]
        for path in mirrors:
            if codec._decrypt_metadata_in_memory(path) != document:
                raise ValueError("HEADLESS_TIMELINE_MIRROR_MISMATCH")
        comparison = compare_headless_documents(timeline, plan, document, metadata, record)
        if comparison["issues"]:
            raise ValueError("HEADLESS_EXACT_READBACK_FAILED:" + str(comparison["issues"]))
        if shared:
            for asset in [*record["assets"], *record.get("font_assets", [])]:
                path = target / asset["relative"]
                if not path.is_file() or backend.nd.digest(path) != asset["sha256"]:
                    raise ValueError("HEADLESS_SHARED_LITERAL_ASSET_MISSING_OR_CHANGED")
            if backend.files_manifest(target) != record["files"]:
                raise ValueError("HEADLESS_SHARED_PRIMARY_CHANGED")
        return comparison

    comparison = verify(build / "draft", shared=False)
    source_identity = tree_identity(build / "draft")
    report.mkdir(parents=True, exist_ok=False)
    if publish:
        publish_tree(build / "draft", target, source_identity)
        verify(target, shared=True)
        publish_tree(build / "draft", distribution, source_identity)
        verify(distribution, shared=True)
    if tree_identity(build / "draft") != source_identity:
        raise ValueError("HEADLESS_SOURCE_CHANGED_DURING_DELIVERY")
    receipt = {"schema": "headless-review-delivery-v1",
        "status": "SHARED_REVIEW_COPY_VERIFIED" if publish else "REVIEW_BUILD_VERIFIED",
        "task_id": timeline.task_id, "revision": timeline.revision,
        "timeline_identity": timeline_identity(timeline), "headless_build": str(build),
        "nas_primary": str(target) if publish else None,
        "nas_distribution": str(distribution) if publish else None,
        "source_preserved": True, "encrypted_mirrors_read": 6,
        "source_identity": source_identity, "comparison": comparison,
        "local_home_registration_changed": False, "computer_use_used": False,
        "delivery_mode": "preview", "formal_delivery_allowed": False,
        "native_ui_acceptance": "pending", "content_issues": list(timeline.unresolved),
        "native_font": str(subtitles[0]["segments"][0]["font_path"]),
        "native_visual_fidelity": "not-certified"}
    (report / "delivery_receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", required=True, type=Path)
    parser.add_argument("--headless-root", required=True, type=Path)
    parser.add_argument("--timeline", required=True, type=Path)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--publish", action="store_true", help="仅在用户授权 NAS 待审副本时设置")
    args = parser.parse_args(argv)
    try:
        result = deliver_review(args.build, args.headless_root, args.timeline, args.report_dir, publish=args.publish)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc), "formal_delivery_allowed": False}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
