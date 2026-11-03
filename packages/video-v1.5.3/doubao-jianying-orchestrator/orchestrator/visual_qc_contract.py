"""One contract for independent visual responses and their delivery consumers."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
from pathlib import Path
from typing import Any

REPORT_VERSION = "chatcut-visual-qc-v3"
RESPONSE_VERSION = "chatcut-visual-qc-response-v3"
AI_REPORT_VERSION = "chatcut-visual-qc-v5"
AI_RESPONSE_VERSION = "chatcut-visual-qc-response-v5"
MATCH_LEVELS = {"DIRECT", "ACCEPTABLE_DEGRADED", "MISMATCH", "INSUFFICIENT_EVIDENCE"}
OVERALL_LEVELS = {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS", "FAIL", "UNCERTIFIED"}


def _draft_documents():
    path = Path(__file__).with_name('runtime') / 'draft_documents.py'
    spec = importlib.util.spec_from_file_location('_qc_draft_documents', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _current_editorial_rules(report: dict) -> bool:
    if __package__:
        from . import ai_editor_contract as contract
    else:
        spec = importlib.util.spec_from_file_location("_chatcut_ai_contract", Path(__file__).with_name("ai_editor_contract.py"))
        contract = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(contract)
    recorded = report.get("editorial_rules")
    return (isinstance(recorded, dict) and report.get("policy_version") == contract.POLICY_VERSION
        and {k: recorded.get(k) for k in ("category", "versions")} == contract.rules_identity({"category": recorded.get("category")}))


def _source_identity(path: str) -> dict:
    source = Path(path).resolve(strict=True)
    info = source.stat()
    return {"path": str(source), "size": info.st_size, "mtime_ns": info.st_mtime_ns}


def _source_bindings_valid(report: dict, draft: Path | None = None) -> bool:
    bindings = report.get("source_bindings")
    if (not isinstance(bindings, list) or not bindings
            or any(not isinstance(row, dict) or not isinstance(row.get("path"), str)
                   or not isinstance(row.get("identity"), dict) for row in bindings)):
        return False
    if any(not isinstance(row["identity"].get("path"), str)
           or type(row["identity"].get("size")) is not int or row["identity"]["size"] < 0
           or type(row["identity"].get("mtime_ns")) is not int for row in bindings):
        return False
    if draft is not None:
        paths = [os.path.normcase(str(Path(row["path"]).resolve(strict=True))) for row in bindings]
        if any(_source_identity(row["path"]) != row["identity"] for row in bindings):
            return False
        # Every source actually referenced by the video timeline needs a binding.
        info = _draft_documents().timeline(draft)
        used = {str(segment.get("material_id") or "") for track in info.get("tracks", [])
                for segment in track.get("segments", [])}
        for material in (info.get("materials") or {}).get("videos", []):
            if str(material.get("id") or material.get("material_id") or "") not in used:
                continue
            raw = str(material.get("path") or material.get("media_path") or "")
            source = Path(raw)
            try:
                readable = source.is_file()
            except OSError:
                readable = False
            if not readable:
                source = draft / "media" / Path(raw).name
            if os.path.normcase(str(source.resolve(strict=True))) not in paths:
                return False
    return True


def draft_metadata_fingerprint(draft: Path) -> str:
    """Hash JianYing timeline metadata only, never the referenced media tree."""
    root = Path(draft).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("DRAFT_DIRECTORY_REQUIRED")
    rows = []
    names = ['draft_info.json', 'draft_content.json', 'ai_selection_identity.json']
    if _draft_documents().is_native(root):
        names += ['headless-production.json', 'draft_meta_info.json', 'template-2.tmp',
                  'draft_content.json.bak', 'timeline_layout.json', 'Timelines/project.json', 'Timelines/project.json.bak',
                  'caption_identity.json', 'audio_identity.json']
        names += [str(p.relative_to(root)) for p in (root / 'Timelines').glob('*/draft_content.json')]
        names += [str(p.relative_to(root)) for p in (root / 'Timelines').glob('*/draft_content.json.bak')]
        names += [str(p.relative_to(root)) for p in (root / 'Timelines').glob('*/template-2.tmp')]
    for name in names:
        path = root / name
        if path.is_symlink():
            raise ValueError("DRAFT_METADATA_SYMLINK_REJECTED")
        if path.is_file():
            rows.append({"name": name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    if not rows:
        raise ValueError("DRAFT_METADATA_MISSING")
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def normalize_match_level(row: dict[str, Any]) -> str:
    raw = str(row.get("match_level") or row.get("level") or "").strip().upper()
    # Old negative judgments still block; a boolean success never proves DIRECT.
    level = {"FAIL": "MISMATCH", "UNCERTAIN": "INSUFFICIENT_EVIDENCE",
             "NOT_VERIFIED": "INSUFFICIENT_EVIDENCE",
             "DEGRADED": "ACCEPTABLE_DEGRADED"}.get(raw, raw)
    if level == "MISMATCH":
        return level
    if not isinstance(level, str) or level not in MATCH_LEVELS:
        return "INSUFFICIENT_EVIDENCE"
    expected = level in {"DIRECT", "ACCEPTABLE_DEGRADED"}
    if row.get("visual_ok") is not expected:
        return "INSUFFICIENT_EVIDENCE"
    return level


def row_errors(row: Any) -> list[str]:
    if not isinstance(row, dict):
        return ["SEGMENT_NOT_OBJECT"]
    errors = []
    if type(row.get("index")) is not int or row["index"] < 1:
        errors.append("INDEX_INVALID")
    level = row.get("match_level")
    if not isinstance(level, str) or level not in MATCH_LEVELS:
        errors.append("MATCH_LEVEL_INVALID")
    if type(row.get("visual_ok")) is not bool:
        errors.append("VISUAL_OK_INVALID")
    elif isinstance(level, str) and level in MATCH_LEVELS and row["visual_ok"] != (level in {"DIRECT", "ACCEPTABLE_DEGRADED"}):
        errors.append("VERDICT_CONFLICT")
    for key in ("observed", "reason"):
        if not isinstance(row.get(key), str) or not row[key].strip():
            errors.append(key.upper() + "_MISSING")
    confidence = row.get("confidence")
    try:
        confidence_valid = type(confidence) in (int, float) and math.isfinite(confidence) and 0 <= confidence <= 1
    except OverflowError:
        confidence_valid = False
    if not confidence_valid:
        errors.append("CONFIDENCE_INVALID")
    elif confidence == 0 and isinstance(level, str) and level in {"DIRECT", "ACCEPTABLE_DEGRADED"}:
        errors.append("POSITIVE_VERDICT_ZERO_CONFIDENCE")
    return errors


def response_errors(model: dict[str, Any], expected_count: int, *, expected_version: str = RESPONSE_VERSION) -> list[str]:
    errors = list(model.get("contract_errors") or [])
    if model.get("response_version") != expected_version:
        errors.append("RESPONSE_VERSION_UNSUPPORTED")
    if not isinstance(model.get("overall"), str) or model["overall"] not in OVERALL_LEVELS:
        errors.append("OVERALL_INVALID")
    rows = model.get("segments")
    if not isinstance(rows, list):
        return errors + ["SEGMENTS_NOT_LIST"]
    indices = []
    for row in rows:
        errors.extend(f"S{row.get('index', '?')}:{error}" if isinstance(row, dict) else error
                      for error in row_errors(row))
        if (expected_version != RESPONSE_VERSION and isinstance(row, dict)
                and normalize_match_level(row) in {"DIRECT", "ACCEPTABLE_DEGRADED"}
                and type(row.get("confidence")) in (int, float) and row["confidence"] < .6):
            errors.append("AI_EDITOR_POSITIVE_CONFIDENCE_LOW")
        if isinstance(row, dict) and type(row.get("index")) is int:
            indices.append(row["index"])
    if sorted(indices) != list(range(1, expected_count + 1)):
        errors.append("BATCH_INDEX_COVERAGE_INVALID")
    return sorted(set(errors))


def report_status(report: Any) -> str:
    try:
        return _report_status(report)
    except (OSError, RuntimeError, KeyError, TypeError, ValueError, AttributeError, OverflowError):
        return "FAIL" if isinstance(report, dict) and report.get("status") == "FAIL" else "UNCERTIFIED"


def report_matches_draft(report: Any, draft: Path) -> bool:
    """Bind a persisted QC report to the exact draft directory it inspected."""
    if not isinstance(report, dict) or not isinstance(report.get("draft"), str):
        return False
    try:
        expected = Path(draft).resolve(strict=True)
        recorded = Path(report["draft"]).resolve(strict=True)
        ai_identity = expected / "ai_selection_identity.json"
        mode = report.get("selection_mode")
        if ai_identity.exists() or (mode in {"ai_editor", "rule_then_ai"}
                                    and report.get("version") == AI_REPORT_VERSION):
            if not ai_identity.is_file() or mode not in {"ai_editor", "rule_then_ai"}:
                return False
            identity = json.loads(ai_identity.read_text(encoding="utf-8"))
            if (identity.get("selection_mode") != mode
                    or identity.get("selection_sha256") != report.get("selection_sha256")
                    or report.get("response_contract", {}).get("version") != AI_RESPONSE_VERSION):
                return False
            if (not _current_editorial_rules(report) or identity.get("editorial_rules") != report.get("editorial_rules")
                    or identity.get("policy_version") != report.get("policy_version")):
                return False
        if not _source_bindings_valid(report, expected):
            return False
        return (expected.is_dir() and recorded.is_dir()
                and os.path.normcase(str(expected)) == os.path.normcase(str(recorded))
                and report.get("draft_metadata_sha256") == draft_metadata_fingerprint(expected)
                and report.get("draft_metadata_sha256_final") == report.get("draft_metadata_sha256"))
    except (OSError, RuntimeError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _report_status(report: Any) -> str:
    """Recheck the same evidence contract before trusting a persisted PASS."""
    if not isinstance(report, dict):
        return "UNCERTIFIED"
    rows = report.get("model_review") or []
    if report.get("status") == "FAIL" or any(
            isinstance(row, dict) and normalize_match_level(row) == "MISMATCH" for row in rows):
        return "FAIL"
    status = report.get("status")
    if (str(report.get("delivery_mode") or "").strip().lower() != "formal"
            or report.get("release_eligibility") == "PREVIEW_ONLY" and status == "PASS"
            or status not in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}):
        return "UNCERTIFIED"
    if not _source_bindings_valid(report):
        return "UNCERTIFIED"
    contract = report.get("response_contract") or {}
    mode = report.get("selection_mode")
    ai_mode = (mode == "ai_editor" or (mode == "rule_then_ai" and report.get("version") == AI_REPORT_VERSION))
    expected_report = AI_REPORT_VERSION if ai_mode else REPORT_VERSION
    expected_response = AI_RESPONSE_VERSION if ai_mode else RESPONSE_VERSION
    if (report.get("version") != expected_report or not isinstance(contract, dict)
            or contract.get("version") != expected_response
            or contract.get("status") != "VALIDATED" or contract.get("errors") != []):
        return "UNCERTIFIED"
    metadata_hash = report.get("draft_metadata_sha256")
    if (not isinstance(metadata_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", metadata_hash)
            or report.get("draft_metadata_sha256_final") != metadata_hash):
        return "UNCERTIFIED"
    if not isinstance(rows, list) or not rows or any(row_errors(row) for row in rows):
        return "UNCERTIFIED"
    if ai_mode:
        if (not _current_editorial_rules(report)
                or not re.fullmatch(r"[0-9a-f]{64}", str(report.get("selection_sha256") or ""))
                or report.get("ai_final_windows_verified" if mode == "rule_then_ai" else "final_windows_verified") is not True
                or report.get("issues")
                or any(row.get("confidence", 0) < .6 for row in rows)):
            return "UNCERTIFIED"
    indices = [row["index"] for row in rows]
    mapping = report.get("segment_map") or []
    batches = report.get("batch_results") or []
    if (not isinstance(mapping, list) or not isinstance(batches, list) or not batches
            or any(not isinstance(row, dict) for row in mapping + batches)):
        return "UNCERTIFIED"
    expected = [row.get("claim_index") for row in mapping]
    batch_indices = [index for batch in batches for index in (batch.get("claim_indices") or [])]
    if (len(set(indices)) != len(indices) or sorted(indices) != sorted(expected)
            or sorted(indices) != sorted(batch_indices)
            or any(batch.get("status") != "PARSED" or batch.get("contract_status") != "VALIDATED"
                   for batch in batches)
            or contract.get("validated_batches") != list(range(1, len(batches) + 1))):
        return "UNCERTIFIED"
    levels = [normalize_match_level(row) for row in rows]
    if "INSUFFICIENT_EVIDENCE" in levels or (status == "PASS" and any(level != "DIRECT" for level in levels)):
        return "UNCERTIFIED"
    return status
