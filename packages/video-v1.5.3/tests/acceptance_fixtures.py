"""Synthetic on-disk evidence for acceptance tests; never real visual approval."""
from __future__ import annotations

import json
from pathlib import Path
import shutil

from tools.acceptance import evaluation as pe, task_review
from tools import draft_delivery
from tools.delivery_identity import tree_identity

ROOT = Path(__file__).resolve().parents[1]


def write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_video_draft(draft: Path) -> None:
    """Give structural fixtures a real referenced file without replacing audio."""
    (draft / "media").mkdir(parents=True, exist_ok=True)
    path = draft / "draft_info.json"
    payload = pe.read(path) if path.is_file() else {}
    used = {str(segment.get("material_id") or "") for track in payload.get("tracks", [])
            for segment in track.get("segments", [])}
    videos = payload.setdefault("materials", {}).setdefault("videos", [])
    if not any(str(row.get("id") or row.get("material_id") or "") in used for row in videos):
        media = draft / "media" / "qc-source.mp4"
        if not media.exists():
            media.write_bytes(b"synthetic video bytes for structural tests")
        videos.append({"id": "fixture-video", "name": media.name, "path": str(media)})
        payload.setdefault("tracks", []).append({"name": "Video", "type": "video", "segments": [
            {"material_id": "fixture-video", "target_timerange": {"start": 0, "duration": 1000000}}]})
        payload.setdefault("id", "fixture-" + draft.name)
        payload.setdefault("name", draft.name)
        write(path, payload)
        if (draft / "draft_content.json").is_file():
            write(draft / "draft_content.json", payload)
    if not (draft / "draft_meta_info.json").exists():
        write(draft / "draft_meta_info.json", {
            "draft_id": payload["id"], "draft_name": draft.name,
            "draft_fold_path": str(draft), "draft_root_path": str(draft.parent),
            "draft_json_file": str(draft / "draft_info.json")})


def qc_report(draft: Path, status: str = "PASS") -> dict:
    """Bind a complete synthetic response to every referenced video source."""
    ensure_video_draft(draft)
    payload = pe.read(draft / "draft_info.json")
    used = {str(segment.get("material_id") or "") for track in payload.get("tracks", [])
            for segment in track.get("segments", [])}
    bindings = []
    for row in payload["materials"]["videos"]:
        if str(row.get("id") or row.get("material_id") or "") not in used:
            continue
        source = Path(str(row.get("path") or row.get("media_path") or ""))
        source = source if source.is_file() else draft / "media" / source.name
        stat = source.stat()
        bindings.append({"path": str(source), "identity": {
            "path": str(source.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}})
    fingerprint = pe.visual_qc_contract.draft_metadata_fingerprint(draft)
    return {"version": pe.visual_qc_contract.REPORT_VERSION, "status": status, "delivery_mode": "formal", "issues": [],
            "draft": str(draft), "source_bindings": bindings,
            "draft_metadata_sha256": fingerprint, "draft_metadata_sha256_final": fingerprint,
            "response_contract": {"version": pe.visual_qc_contract.RESPONSE_VERSION,
                "status": "VALIDATED", "validated_batches": [1], "errors": []},
            "segment_map": [{"claim_index": 1, "draft_segments": [1]}],
            "batch_results": [{"batch_no": 1, "claim_indices": [1], "status": "PARSED",
                               "contract_status": "VALIDATED"}],
            "model_review": [{"index": 1, "match_level": "DIRECT", "visual_ok": True,
                "confidence": 0.8, "observed": "Synthetic observed fixture frame",
                "reason": "Offline fixture only; not real visual acceptance"}]}


def make_qc_reports(task_dir: Path, draft_root: Path, names: list[str]) -> list[dict]:
    reports = []
    for index, name in enumerate(names, 1):
        draft = draft_root / name
        report = qc_report(draft)
        path = task_dir / "report" / (draft.name if index == 1 else "report_%d" % index) / "draft_visual_qc.json"
        write(path, report)
        reports.append({"draft": name, "status": "PASS", "report": str(path)})
    return reports


def make_policy(path: Path) -> Path:
    write(path, {"policy_id": "synthetic", "policy_version": "fixture",
                 "qc": {"formal_delivery_requires": ["PASS"]}})
    return path


def make_acceptance(task_dir: Path, draft_root: Path, names: list[str], task: dict,
                    mode: str = "enforce") -> dict:
    """Run actual current-output validation against a synthetic Windows runtime."""
    task_dir.mkdir(parents=True, exist_ok=True)
    task = dict(task)
    task.setdefault("delivery_mode", "formal")
    reports = make_qc_reports(task_dir, draft_root, names)
    write(task_dir / "task.json", task)
    input_path = task_dir / "synthetic-input.mp4"
    input_path.write_bytes(b"offline frozen input fixture")
    runtime_root = task_dir / "runtime"
    runtime_root.mkdir(exist_ok=True)
    (runtime_root / "VERSION").write_text("fixture", encoding="utf-8")
    policy_name = "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json"
    write(runtime_root / policy_name, {"video_tool_version": "fixture", "visual_policy_version": "fixture"})
    write(runtime_root / "release-manifest.json", {"version": "fixture", "visual_policy_version": "fixture",
          "file_count": 2, "files": [{"path": "VERSION"}, {"path": policy_name}]})
    runtime = pe.runtime_identity(runtime_root)
    contract = pe.freeze(task, "fixture-run", [input_path], runtime, mode=mode)
    pe.write_once(task_dir / "eval" / contract["run_id"] / "contract.json", contract)
    pe.write_once(task_dir / "eval" / "active_contract.json", contract)
    receipt = task_review.evaluate_task(
        task_dir, task, names, reports, contract, draft_root=draft_root,
        runtime_root=runtime_root, read_json=pe.read,
        package_validator=lambda draft: task_review.validate_draft_package(
            draft, video_extensions=(".mp4", ".mov", ".m4v")))
    if receipt["status"] != "PASS":
        raise AssertionError("Current-output acceptance fixture failed: " + str(receipt))
    done = {"status": "PREVIEW_READY", "delivery_mode": "formal", "drafts": names,
            "local_only_drafts": names, "qc_reports": reports, "eval_receipt": receipt}
    write(task_dir / "done.json", done)
    return {"contract": contract, "qc_reports": reports, "receipt": receipt,
            "runtime": runtime, "runtime_root": runtime_root, "task": task, "done": done}


def make_distribution(acceptance: dict, draft_root: Path, names: list[str], share: Path) -> list[dict]:
    """Copy real local fixture files and verify their literal shared references."""
    receipts = []
    contract = acceptance["contract"]
    for name in names:
        source = draft_root / name
        reference_root = share / name
        target = share / "distribution" / name
        draft_delivery.archive_shared_copy(source, reference_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(reference_root, target)
        draft_delivery.verify_shared(target, reference_root)
        receipt = {"status": "VERIFIED", "draft_name": name,
            "task_id": contract["task_id"], "run_id": contract["run_id"],
            "contract_hash": contract["contract_hash"],
            "source_output_sha256": pe.output_identity([source])["sha256"],
            "nas_target": str(target), "transformed_tree_sha256": tree_identity(target)["sha256"],
            "eval_receipt_sha256": acceptance["receipt"]["receipt_sha256"], "eval_status": "PASS",
            "shared_media_references_verified": True, "shared_reference_root": str(reference_root)}
        receipt["receipt_sha256"] = pe.digest(receipt)
        receipts.append(receipt)
    return receipts
