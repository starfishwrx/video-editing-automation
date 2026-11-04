"""Pure queue input, retry and terminal-result contracts.

Worker paths, process state and environment reads stay in batch_worker. Callers
supply the active runtime version and retry markers explicitly.
"""
from __future__ import annotations

import pathlib
import re

TRANSIENT_BUILD_MARKERS = (
    "402", "502", "503", "429", "Too Many Requests", "temporarily unavailable",
    "Insufficient Balance", "timed out", "SHOT_ANALYSIS_PENDING",
    "VISION_CALL_FAILED", "SHOT_MATCH_BLOCKED", "Connection reset",
    "VISION_JSON_INVALID",
    "Connection refused", "could not resolve host", "network",
    "vision analysis incomplete", "empty description",
)


def task_runtime_overrides(task: dict) -> dict:
    """Apply the same explicit settings to queue preflight and generated builds."""
    nested = task.get("manifest")
    if nested is None:
        nested = {}
    if not isinstance(nested, dict):
        raise ValueError("INVALID_TASK_MANIFEST")
    result = {}
    for key in ("selection_mode", "draft_backend", "subtitle_font_path"):
        value = task.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            value = nested.get(key)
        if value is not None and not (isinstance(value, str) and not value.strip()):
            result[key] = value
    return result


def task_product_fields(task: dict) -> dict:
    """Normalize product identity once for preparation and selection."""
    nested = task.get("manifest") or {}
    if not isinstance(nested, dict):
        raise ValueError("INVALID_TASK_MANIFEST")
    result = {}
    for key in ("product", "product_key", "product_id", "brand", "sku", "category",
                "product_name", "title", "task_title", "series"):
        value = task.get(key)
        if value is None or not str(value).strip():
            value = nested.get(key)
        if value is not None and str(value).strip():
            result[key] = str(value).strip()
    return result


def meeting_bgm_spec(value: object, *, base_dir: pathlib.Path | None = None) -> dict:
    """Preserve an explicit worker BGM choice; strings never become random auto."""
    import importlib.util
    path = pathlib.Path(__file__).resolve().parents[2] / "doubao-jianying-orchestrator/orchestrator/audio/bgm_request.py"
    spec = importlib.util.spec_from_file_location("_queue_bgm_request", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.normalize(value)
    if base_dir is not None and result.get("path"):
        target = pathlib.Path(str(result["path"])).expanduser()
        result["path"] = str((target if target.is_absolute() else base_dir / target).resolve())
    return result


def safe_manifest_stem(name: str) -> str:
    """Make a Windows-safe manifest filename without changing the draft name.

    Only the on-disk manifest file is sanitized; the Jianying draft_name inside
    the manifest keeps its original title, so drafts and their display names are
    never altered or renamed.
    """
    stem = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", str(name)).strip(" .")
    return stem or "draft"


def is_reusable_analysis_row(row: object) -> bool:
    if not isinstance(row, dict):
        return False
    empty_rejection = (row.get("visual_observation_complete") is True
                       and row.get("action_complete") is False
                       and row.get("status") == "pending_analysis"
                       and row.get("evidence_tags") == [])
    if not (str(row.get("description") or "").strip()
            and row.get("visual_tags") and (row.get("evidence_tags") or empty_rejection)):
        return False
    try:
        return float(row.get("analysis_confidence") or 0) > 0
    except (TypeError, ValueError):
        return False


def parallel_pair_safe(left: dict, right: dict) -> bool:
    """Avoid same-title draft collisions and same-pool cache contention."""
    left_title = str(left.get("title") or left.get("record_id") or "").strip().casefold()
    right_title = str(right.get("title") or right.get("record_id") or "").strip().casefold()
    left_material = str(left.get("material_dir") or "").strip().casefold()
    right_material = str(right.get("material_dir") or "").strip().casefold()
    return bool(left_title and right_title and left_title != right_title
                and left_material and right_material
                and left_material != right_material)


def batch_result_contract(status: str, *, pipeline_version: str, run_id: str, failures: list[dict] | None = None,
                           error: str = "", parent_run_id: str | None = None,
                           task_id: str | None = None,
                           manifest_sha256: str | None = None,
                           runtime: dict | None = None,
                           task_log: str | None = None) -> dict:
    """Return the monitor contract used to decide whether another task is safe."""
    rows = failures or []
    text = " ".join(str(row) for row in rows) + " " + str(error)
    lower = text.lower()
    first = rows[0] if rows else {}
    failure_type = str(first.get("type") or "").lower()
    if first.get("preflight_only") is True and first.get("failure_code") == "ENVIRONMENT_PRECHECK_FAILED":
        failure_class, failure_code = "environment", "ENVIRONMENT_PRECHECK_FAILED"
    elif any(marker in lower for marker in ("material_duration_", "material_geometry_requires_reanalysis",
                                             "material_source_changed_during_preparation", "material_source_missing")):
        failure_class, failure_code = "environment", "MATERIAL_DURATION_UNAVAILABLE"
    elif any(marker in lower for marker in ("fixed_narration_", "selection_audio_changed",
            "selection_recovery_audio_changed", "selection_recovery_bgm_", "bgm_changed_during_", "bgm_source_audio_invalid",
            "material_source_list_", "file_changed_during_hash")):
        failure_class, failure_code = "environment", "FIXED_INPUT_INVALID_OR_CHANGED"
    elif "voice_selection_required" in lower:
        failure_class, failure_code = "environment", "VOICE_SELECTION_REQUIRED"
    elif "bgm_required" in lower:
        failure_class, failure_code = "environment", "BGM_REQUIRED"
    elif "sentence_plan_review_required" in lower:
        failure_class, failure_code = "semantic", "SENTENCE_PLAN_REVIEW_REQUIRED"
    elif any(marker in lower for marker in ("vision_call_failed", "vision_json_invalid", "ai_editor_call_failed")):
        failure_class, failure_code = "vision", "VISION_CALL_FAILED"
    elif any(marker in lower for marker in ("visual_match_blocked", "shot_match_blocked", "material gap", "visual-missing")):
        failure_class, failure_code = "semantic", "VISUAL_MATCH_BLOCKED"
    elif "audio_video_mismatch" in lower or "shot_timing" in lower:
        failure_class, failure_code = "timing", "AUDIO_VIDEO_MISMATCH"
    elif "invalid argument" in lower or "task_budget" in lower or "budget" in lower:
        failure_class, failure_code = "environment", "INVALID_ARGUMENT" if "invalid argument" in lower else "TASK_BUDGET_EXHAUSTED"
    elif "task_already_running" in lower or "task_lock" in lower:
        failure_class, failure_code = "environment", "TASK_ALREADY_RUNNING"
    elif failure_type in {"environment", "preflight"} or "environment_precheck" in lower:
        failure_class, failure_code = "environment", "ENVIRONMENT_PRECHECK_FAILED"
    elif any(marker in lower for marker in (
        "nas mount", "material_dir", "no video files", "script empty",
        "draft root", "pipeline version",
    )):
        failure_class, failure_code = "environment", "ENVIRONMENT_PRECHECK_FAILED"
    elif "qc" in lower or "draft" in lower:
        failure_class, failure_code = "draft", "DRAFT_INVALID"
    elif failure_type == "transient":
        failure_class, failure_code = "provider", "TRANSIENT_BUILD_FAILURE"
    elif rows or error:
        failure_class, failure_code = "unknown", "TASK_FAILED"
    else:
        failure_class, failure_code = None, None

    retryable = bool(
        failure_class in {"vision", "provider"}
        and any(token in lower for token in (
            "timeout", "timed out", "429", "502", "503", "network",
            "connection reset", "temporarily unavailable",
            "deadline_exceeded", "request_timeout", "connect_timeout",
        ))
    )
    if (first.get("preflight_only") is True and first.get("retryable") is True
            and first.get("resume_stage") == "semantic_match"):
        retryable = True
    if any(row.get("stage_retry_exhausted") is True or row.get("retryable") is False for row in rows) or any(
            token in lower for token in ("unauthorized", "forbidden", "invalid api key", "insufficient balance", "http 401", "http 403")):
        retryable = False
    if status == "OK":
        terminal_state, next_action = "SUCCESS", "complete"
    elif status == "DEFERRED":
        terminal_state, next_action = "DEFERRED", "skip_task"
    elif status in {"PARTIAL", "PREVIEW_READY"}:
        terminal_state, next_action = "UNCERTIFIED", "manual_review"
    elif failure_class in {"environment", "semantic", "timing", "draft"}:
        terminal_state, next_action = "BLOCKED", "fix_environment" if failure_class == "environment" else "manual_review"
    else:
        terminal_state, next_action = "ERROR", "retry_stage" if retryable else "manual_review"
    return {
        "task_id": task_id,
        "run_id": run_id,
        "parent_run_id": parent_run_id,
        "pipeline_version": pipeline_version,
        "terminal_state": terminal_state,
        "failure_class": failure_class,
        "failure_code": failure_code,
        "retryable": retryable,
        "safe_to_continue": terminal_state == "SUCCESS",
        "next_action": next_action,
        "manifest_sha256": manifest_sha256,
        "runtime": runtime,
        "task_log": task_log,
        "resume_stage": first.get("resume_stage") if retryable else None,
    }


def classify_build_failure(text: str, *, transient_markers=TRANSIENT_BUILD_MARKERS) -> dict:
    """Auto-classify a build failure so the loop can pick a repair strategy.

    Transient = retryable with a different strategy (fresh vision, analysis
    reuse, or a longer timeout).  Hard = a genuine visual-analysis blocker that
    must be recorded, never faked as OK.
    """
    blob = text or ""
    lower_blob = blob.lower()
    hits = [m for m in transient_markers if m.lower() in lower_blob]
    transient_signals = (
        "502", "503", "429", "too many requests", "temporarily unavailable",
        "timed out", "timeout", "connection reset", "connection refused",
        "could not resolve host", "network", "provider unavailable",
        "deadline_exceeded", "request_timeout", "connect_timeout",
    )
    transient = any(marker in lower_blob for marker in transient_signals)
    credential_or_input_block = any(marker in lower_blob for marker in (
        "credential", "unauthorized", "forbidden", "invalid api key",
        "insufficient balance", "401", "403", "invalid path", "path not found",
        "file not found", "no such file", "encoding error", "unicode decode",
    ))
    material_duration_block = any(marker in lower_blob for marker in (
        "material_duration_", "material_geometry_requires_reanalysis",
        "material_source_changed_during_preparation", "material_source_missing"))
    deterministic_block = material_duration_block or any(marker.lower() in lower_blob for marker in (
        "VISUAL_MATCH_BLOCKED", "SHOT_MATCH_BLOCKED", "AUDIO_VIDEO_MISMATCH",
        "TASK_BUDGET", "Invalid argument", "INVALID_ARGUMENT",
        "DESKTOP_IMPORT_BLOCKED", "DRAFT_INVALID", "VOICE_SELECTION_REQUIRED",
        "BGM_REQUIRED", "SENTENCE_PLAN_REVIEW_REQUIRED",
        "SELECTION_STAGE_RETRY_EXHAUSTED", "SELECTION_RECOVERY_INPUT_CHANGED",
        "FIXED_NARRATION_", "SELECTION_AUDIO_CHANGED", "SELECTION_RECOVERY_AUDIO_CHANGED",
        "SELECTION_RECOVERY_BGM_", "BGM_CHANGED_DURING_", "BGM_SOURCE_AUDIO_INVALID", "MATERIAL_SOURCE_LIST_", "FILE_CHANGED_DURING_HASH",
    )) or credential_or_input_block or ("vision_call_failed" in lower_blob and not transient)
    vision_hard = ("draft_visual_qc_blocked" in lower_blob) or (
        "no auditable visual top1" in lower_blob) or (
        "visual_match_blocked" in lower_blob and not hits)
    return {
        "transient": bool(hits and transient and not credential_or_input_block and not material_duration_block),
        "vision_hard": vision_hard,
        "deterministic_block": deterministic_block,
        "markers": hits,
    }


def select_queue_for_single_run(queue: list[dict], *,
                                record_id: str = "",
                                force_rebuild: bool = False,
                                resume_partial: bool = False) -> list[dict]:
    """Select exactly one task; a targeted run must never drain the queue."""
    target = str(record_id or "").strip()
    if force_rebuild and not target:
        raise ValueError("force_rebuild requires an exact record_id")
    if target:
        matches = [task for task in queue
                   if str(task.get("record_id") or "").strip() == target]
        if len(matches) != 1:
            raise ValueError("expected exactly one queued task for record_id=%s; found %d"
                             % (target, len(matches)))
        selected = dict(matches[0])
        if force_rebuild:
            selected["force_rebuild"] = True
        if resume_partial:
            selected["force_rebuild"] = False
            selected["retry_deferred"] = True
            selected["retry_error"] = False
        return [selected]
    return [dict(queue[0])] if queue else []
