"""One contract for independent visual responses and their delivery consumers."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any

REPORT_VERSION = "chatcut-visual-qc-v3"
RESPONSE_VERSION = "chatcut-visual-qc-response-v3"
MATCH_LEVELS = {"DIRECT", "ACCEPTABLE_DEGRADED", "MISMATCH", "INSUFFICIENT_EVIDENCE"}
OVERALL_LEVELS = {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS", "FAIL", "UNCERTIFIED"}


def _file_sha256(path: str) -> str:
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def draft_metadata_fingerprint(draft: Path) -> str:
    """Hash JianYing timeline metadata only, never the referenced media tree."""
    root = Path(draft).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("DRAFT_DIRECTORY_REQUIRED")
    rows = []
    for name in ("draft_info.json", "draft_content.json", "ai_selection_identity.json"):
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
        if (expected_version.endswith("-v4") and isinstance(row, dict)
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
    except (TypeError, ValueError, AttributeError, OverflowError):
        return "FAIL" if isinstance(report, dict) and report.get("status") == "FAIL" else "UNCERTIFIED"


def report_matches_draft(report: Any, draft: Path) -> bool:
    """Bind a persisted QC report to the exact draft directory it inspected."""
    if not isinstance(report, dict) or not isinstance(report.get("draft"), str):
        return False
    try:
        expected = Path(draft).resolve(strict=True)
        recorded = Path(report["draft"]).resolve(strict=True)
        ai_identity = expected / "ai_selection_identity.json"
        if ai_identity.exists() or report.get("selection_mode") == "ai_editor":
            if not ai_identity.is_file() or report.get("selection_mode") != "ai_editor":
                return False
            identity = json.loads(ai_identity.read_text(encoding="utf-8"))
            if (identity.get("selection_sha256") != report.get("selection_sha256")
                    or report.get("response_contract", {}).get("version") != "chatcut-visual-qc-response-v4"):
                return False
            bindings = report.get("source_bindings")
            if not isinstance(bindings, list) or not bindings or any(_file_sha256(b["path"]) != b["sha256"] for b in bindings):
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
    if status not in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}:
        return "UNCERTIFIED"
    contract = report.get("response_contract") or {}
    ai_mode = report.get("selection_mode") == "ai_editor"
    expected_report = "chatcut-visual-qc-v4" if ai_mode else REPORT_VERSION
    expected_response = "chatcut-visual-qc-response-v4" if ai_mode else RESPONSE_VERSION
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
        if (not re.fullmatch(r"[0-9a-f]{64}", str(report.get("selection_sha256") or ""))
                or report.get("final_windows_verified") is not True
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
