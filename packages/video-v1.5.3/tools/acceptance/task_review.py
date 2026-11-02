"""Current-output validation and evidence aggregation for Windows tasks.

Paths and validators are explicit inputs. This module neither builds drafts nor
ships them; final delivery remains a separate authorized workflow.
"""
from __future__ import annotations

import json
import pathlib
from typing import Callable, Iterable

from . import evaluation as pe


def evaluate_task(task_dir: pathlib.Path, task: dict, drafts: list[str],
                  qc_reports: list[dict], frozen: dict, *,
                  draft_root: pathlib.Path, runtime_root: pathlib.Path,
                  package_validator: Callable, read_json: Callable) -> dict:
    """Persist current-output evidence before any final distribution/table gate."""
    pe.verify_contract(frozen)
    if (not drafts or any(not isinstance(name, str) or name in {"", ".", ".."}
                          or pathlib.Path(name).name != name for name in drafts)
            or len(set(drafts)) != len(drafts)):
        raise ValueError("TASK_DRAFTS_MISSING_OR_INVALID")
    outputs = [draft_root / name for name in drafts]
    eval_dir = task_dir / "eval" / frozen["run_id"]
    eval_dir.mkdir(parents=True, exist_ok=True)
    identity = pe.output_identity(outputs)
    binding = {"task_id": frozen["task_id"], "run_id": frozen["run_id"],
               "contract_hash": frozen["contract_hash"], "output_sha256": identity["sha256"]}
    checks = [package_validator(path) for path in outputs]
    package = dict(binding, status="PASS" if checks and all(c["ok"] for c in checks) else "FAIL",
                   files_verified=bool(checks) and all(c["ok"] for c in checks), checks=checks)
    package_path = eval_dir / ("package-" + identity["sha256"] + ".json")
    pe.write_once(package_path, package)
    output_by_name = {path.name: path for path in outputs}
    report_rows = list(qc_reports) if isinstance(qc_reports, list) else []
    qc_files = [pathlib.Path(str(row.get("report") or "")) if isinstance(row, dict)
                else pathlib.Path("") for row in report_rows]
    qc_payloads = [read_json(path) if path.is_file() else {} for path in qc_files]
    qc_payloads = [row if isinstance(row, dict) else {} for row in qc_payloads]
    statuses = [pe.visual_qc_contract.report_status(row) for row in qc_payloads]
    seen_drafts = set()
    qc_identity_valid = len(report_rows) == len(outputs) and bool(outputs)
    for ref, report in zip(report_rows, qc_payloads):
        draft_name = ref.get("draft") if isinstance(ref, dict) else None
        target = output_by_name.get(str(draft_name))
        if (target is None or str(draft_name) in seen_drafts
                or not pe.visual_qc_contract.report_matches_draft(report, target)):
            qc_identity_valid = False
        if target is not None:
            seen_drafts.add(str(draft_name))
    qc_identity_valid = qc_identity_valid and len(seen_drafts) == len(outputs)
    qc_status = ("FAIL" if "FAIL" in statuses else
                 "PASS_WITH_DEGRADED" if statuses and len(statuses) == len(drafts)
                 and qc_identity_valid
                 and all(s in {"PASS", "PASS_WITH_DEGRADED"} for s in statuses)
                 and "PASS_WITH_DEGRADED" in statuses else
                 "PASS" if len(statuses) == len(drafts) and statuses and qc_identity_valid
                 and all(s == "PASS" for s in statuses)
                 else "UNCERTIFIED")
    contract_status = ("VALIDATED" if len(qc_payloads) == len(drafts) and qc_payloads and qc_identity_valid
                       and all(isinstance(row.get("response_contract"), dict)
                               and row["response_contract"].get("status") == "VALIDATED"
                               and status in {"PASS", "PASS_WITH_DEGRADED", "FAIL"}
                               for row, status in zip(qc_payloads, statuses)) else "UNCERTIFIED")
    qc = dict(binding, qc_status=qc_status, contract_status=contract_status,
              response_version=(qc_payloads[0].get("response_contract", {}).get("version")
                                if qc_payloads and len({r.get("response_contract", {}).get("version") for r in qc_payloads}) == 1
                                else "MIXED_OR_MISSING"),
              status="FAIL" if qc_status == "FAIL" else "PASS" if qc_status in {"PASS", "PASS_WITH_DEGRADED"} else "UNCERTIFIED",
              reports=[{"path": str(p), "sha256": pe.file_hash(p)} for p in qc_files if p.is_file()])
    qc_path = eval_dir / ("qc-" + pe.digest(qc) + ".json")
    pe.write_once(qc_path, qc)
    proof_map = {"DRAFT-PACKAGE": package_path, "VISUAL-QC": qc_path}
    try:
        runtime = pe.runtime_identity(runtime_root)
    except (OSError, ValueError, KeyError) as exc:
        runtime = {"verification_error": str(exc)}
    result = pe.evaluate(frozen, task, runtime, outputs, proof_map)
    path = eval_dir / ("receipt-" + result["receipt_sha256"] + ".json")
    pe.write_once(path, result)
    return dict(result, receipt_path=str(path))


def validate_draft_package(draft_dir: pathlib.Path, *,
                           video_extensions: Iterable[str]) -> dict:
    """Hard gate the package before NAS shipping.

    A successful build or a timed-out visual model call is not evidence that
    JianYing contains usable video.  Verify the draft graph and the resolved
    local media paths first; otherwise shipping would turn an unverified
    audio-only/empty draft into a false delivery.
    """
    video_extensions = tuple(video_extensions)
    try:
        from tools.native_drafts import timeline
        info = timeline(draft_dir)
    except FileNotFoundError:
        return {"ok": False, "issues": ["DRAFT_INFO_MISSING"]}
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return {"ok": False, "issues": ["DRAFT_INFO_INVALID:%s" % exc]}
    tracks = [t for t in (info.get("tracks") or []) if isinstance(t, dict)]
    preferred = [t for t in tracks if str(t.get("name") or "").casefold() in {
        "video_broll", "video", "main video", "主视频", "视频"
    }]
    video_tracks = preferred or [t for t in tracks if t.get("segments")]
    segments = [s for t in video_tracks for s in (t.get("segments") or [])
                if isinstance(s, dict)]
    if not video_tracks:
        return {"ok": False, "issues": ["VIDEO_TRACK_MISSING"]}
    if not segments:
        return {"ok": False, "issues": ["VIDEO_SEGMENTS_EMPTY"]}
    materials = {}
    for item in (info.get("materials") or {}).get("videos") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or item.get("material_id") or "").strip()
        if key:
            materials[key] = item
    issues = []
    resolved = 0
    real_segments = 0
    for idx, segment in enumerate(segments, 1):
        material_id = str(segment.get("material_id") or "").strip()
        item = materials.get(material_id)
        if not item:
            issues.append("SEGMENT_%d_MATERIAL_MISSING:%s" % (idx, material_id))
            continue
        raw = str(item.get("path") or item.get("media_path") or "").strip()
        source = pathlib.Path(raw) if raw else pathlib.Path()
        if not source.is_file():
            source = draft_dir / "media" / pathlib.Path(raw).name
        if not source.is_file():
            issues.append("SEGMENT_%d_MEDIA_MISSING:%s" % (idx, raw or material_id))
            continue
        resolved += 1
        if source.suffix.lower() in video_extensions:
            real_segments += 1
    if real_segments <= 0:
        issues.append("VIDEO_MEDIA_RESOLVED_ZERO")
    return {"ok": not issues, "issues": issues,
            "video_tracks": len(video_tracks), "video_segments": len(segments),
            "resolved_video_media": resolved, "real_video_segments": real_segments}
