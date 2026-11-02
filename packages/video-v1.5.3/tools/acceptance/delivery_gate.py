"""Shared formal-delivery checks, run before any distribution side effect."""
from __future__ import annotations

from pathlib import Path

from . import evaluation as pe


def requested_mode(task: dict) -> str:
    mode = str(task.get("delivery_mode") or "preview").strip().lower()
    if mode not in {"formal", "preview"}:
        raise ValueError("INVALID_DELIVERY_MODE:" + mode)
    return mode


def preview_requested(task: dict, done: dict) -> bool:
    return requested_mode(task) != "formal" or any(
        "delivery_mode" in row and requested_mode(row) != "formal" for row in (done,))


def mode_issues(task: dict, done: dict) -> list[str]:
    try:
        return ["PREVIEW_NOT_FORMAL"] if preview_requested(task, done) else []
    except ValueError:
        return ["INVALID_DELIVERY_MODE"]


def local_preview(task: dict, qc_result: dict) -> bool:
    """Retain an explicitly uncertified preview; this never permits delivery."""
    return (requested_mode(task) == "preview" and qc_result.get("status") == "UNCERTIFIED"
            and isinstance(qc_result.get("report"), dict)
            and qc_result["report"].get("delivery_mode") == "preview")


def qc_statuses(task_dir: Path, done: dict, draft_root: Path) -> list[str]:
    """Revalidate each actual report and its output identity, never its label alone."""
    drafts = done.get("drafts") or []
    refs = done.get("qc_reports") or []
    if not isinstance(drafts, list) or not isinstance(refs, list):
        return ["UNCERTIFIED"]
    if (refs and len(refs) != len(drafts)) or len({str(name) for name in drafts}) != len(drafts):
        return ["UNCERTIFIED"] * max(1, len(drafts))
    statuses = []
    for index, raw in enumerate(drafts, 1):
        if (not isinstance(raw, str) or not raw.strip() or raw in {".", ".."}
                or Path(raw).name != raw or "/" in raw or "\\" in raw):
            statuses.append("UNCERTIFIED")
            continue
        draft = Path(raw)
        draft = draft if draft.is_absolute() else draft_root / draft
        ref = refs[index - 1] if index <= len(refs) and isinstance(refs[index - 1], dict) else {}
        if ref.get("draft") and str(ref["draft"]) not in {raw, draft.name, str(draft)}:
            statuses.append("UNCERTIFIED")
            continue
        path = Path(str(ref["report"])) if ref.get("report") else (
            task_dir / "report" / (draft.name if index == 1 else "report_%d" % index) / "draft_visual_qc.json")
        try:
            report = pe.read(path)
            status = pe.visual_qc_contract.report_status(report)
            if not pe.visual_qc_contract.report_matches_draft(report, draft):
                status = "UNCERTIFIED"
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            status = "UNCERTIFIED"
        statuses.append(status)
    return statuses


def issues(task_dir: Path, task: dict, done: dict, draft_root: Path,
           *, evaluation: dict | None = None) -> list[str]:
    reasons = mode_issues(task, done)
    statuses = qc_statuses(task_dir, done, draft_root)
    if not statuses or any(status != "PASS" for status in statuses):
        reasons.append("QC_NOT_FORMAL")
    if evaluation is not None:
        criteria = {row.get("id"): row.get("status") for row in evaluation.get("criteria", [])
                    if isinstance(row, dict)}
        required = {"INPUT-IDENTITY", "RUNTIME-IDENTITY", "DRAFT-PACKAGE", "VISUAL-QC"}
        if any(criteria.get(name) != "PASS" for name in required) or "FAIL" in criteria.values():
            reasons.append("DELIVERY_EVIDENCE_NOT_PASS")
    return reasons
