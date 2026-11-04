# -*- coding: utf-8 -*-
"""Windows 脚本流水线的单任务执行器，由定时入口或 dispatcher 调用。

逐条消费 `work/batch_queue.json`：
  enumerate materials -> manifest -> build x count -> independent QC
  -> ship(unс 分发) -> 进度写 `work/batch_progress.jsonl` -> 结果写
  `work/<record_id>/done.json`。
正式草稿通过本机自动验收后分发和回写；预览仅保留本地。
失败/部分失败写入本机证据，有配置时回写失败状态。

原则：
- 任务之间互不影响：单条失败记 `error` + 证据（stderr 尾部/退出码），继续下一条。
- 幂等：任何阶段重跑不产生半成品（report dir 按 task 复用，TTS/素材缓存命中）。
- 凭据不出渲染机：NAS 挂载委托部署环境配置的外部脚本，源码不保存凭据。
"""
from __future__ import annotations

from tools.process_liveness import assert_process_alive as _assert_process_alive
from tools.process_liveness import process_created_at

import json
import hashlib
import os
import pathlib
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from tools import production_eval, material_index
from tools.queueing import contracts as queue_contracts
from tools.acceptance import task_review
from tools.delivery_identity import tree_identity
from tools.draft_delivery import verify_shared
from tools.delivery_runtime import require_ship_dependencies
from tools.acceptance import finalization as acceptance_finalization
from tools.acceptance.finalization import DeliveryContext
from tools.acceptance.delivery_gate import requested_mode, local_preview

try:
    # Windows OpenSSH sessions may expose a GBK console even though the
    # pipeline and provider responses are UTF-8.  A replacement character in
    # a model/error message must never crash the worker while it is logging.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

PIPE_ROOT = pathlib.Path(os.environ.get("JY_PIPE_ROOT", pathlib.Path(__file__).resolve().parent))
WORK = pathlib.Path(os.environ.get("JY_WORK_ROOT", str(PIPE_ROOT / "work")))
def _worker_skill_root() -> pathlib.Path:
    explicit = os.environ.get("JY_ORCHESTRATOR_ROOT", "").strip()
    if explicit:
        return pathlib.Path(explicit)
    legacy = os.environ.get("JY_SKILL_ROOT", "").strip()
    if legacy:
        root = pathlib.Path(legacy)
        for candidate in (root, root / "doubao-jianying-orchestrator", root.parent / "doubao-jianying-orchestrator"):
            if (candidate / "orchestrator" / "cli.py").is_file():
                return candidate
    return PIPE_ROOT / "doubao-jianying-orchestrator"


SKILL = _worker_skill_root()
DRAFTS = pathlib.Path(os.environ.get("JY_DRAFT_ROOT", str(PIPE_ROOT / "drafts")))
PY = os.environ.get("JY_PYTHON_EXE", sys.executable)
POST_DRAFT_QC = SKILL / "orchestrator" / "draft_visual_qc.py"
CAPACITY_PROFILE = pathlib.Path(os.environ.get("JY_CAPACITY_PROFILE", str(PIPE_ROOT / "host_capacity.json")))
PROGRESS = WORK / "batch_progress.jsonl"
STATUS_SNAPSHOT = WORK / "task_status_snapshot.json"
LOG = WORK / "batch_worker.log"
VIDEO_EXTS = (".mp4", ".mov", ".m4v")
SKIP_SHIP = os.environ.get("JY_SKIP_SHIP", "").strip() == "1"
_OUTPUT_LOCK = threading.Lock()
ACTIVE_RECORD_ID = ""
PIPE = PIPE_ROOT
CLOSED_LOOP_TOOL = PIPE / "tools" / "closed_loop.py"
CLOSED_LOOP_POLICY = PIPE / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json"


def _pipeline_version() -> str:
    try:
        return (SKILL / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _runtime_facts() -> dict:
    """Capture the execution surface that produced a batch receipt."""
    if str(SKILL) not in sys.path:
        sys.path.insert(0, str(SKILL))
    from orchestrator.runtime import vision_config
    return {
        "python": str(PY),
        "skill_root": str(SKILL),
        "worker_path": str(pathlib.Path(__file__).resolve()),
        "pipeline_version": _pipeline_version(),
        "ffmpeg": shutil.which("ffmpeg") or "",
        "ffprobe": shutil.which("ffprobe") or "",
        **vision_config.facts(),
    }


def _payload_sha256(payload: object) -> str:
    """Hash a JSON payload with stable ordering for run-to-input correlation."""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_meeting_bgm_spec = queue_contracts.meeting_bgm_spec


def _batch_result_contract(status: str, *, run_id: str, failures: list[dict] | None = None,
                           error: str = "", parent_run_id: str | None = None,
                           task_id: str | None = None,
                           manifest_sha256: str | None = None,
                           runtime: dict | None = None,
                           task_log: str | None = None) -> dict:
    return queue_contracts.batch_result_contract(
        status, pipeline_version=_pipeline_version(), run_id=run_id,
        failures=failures, error=error, parent_run_id=parent_run_id,
        task_id=task_id, manifest_sha256=manifest_sha256, runtime=runtime,
        task_log=task_log)


def _nas_path(name: str) -> str:
    if not NAS_TEST:
        raise RuntimeError("JY_NAS_TEST_ROOT_MISSING")
    return NAS_TEST.rstrip("\\/") + "\\" + name

def _env_int(name: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


# A single build attempt must fit the calibrated per-host timeout, but a
# transient provider failure (402/502/503/429/timeout) must never stop the
# queue nor fake an OK.  The per-draft loop retries a bounded number of times
# and escalates strategy each attempt (fresh vision -> reuse analysis ->
# larger timeout).  When attempts are exhausted the task is recorded as a
# real ERROR/PARTIAL with evidence, never as OK.
BUILD_TIMEOUT_SECONDS = _env_int("JY_BUILD_TIMEOUT_S", 1500, 60)
MAX_DRAFT_ATTEMPTS = _env_int("JY_MAX_DRAFT_ATTEMPTS", 3, 1)
# Per-task wall-clock budget so an oversized material pool (e.g. 526 fresh
# vision sources) cannot stall the whole queue: once the budget is spent the
# worker stops retrying that task and records an explicit BLOCKED failure,
# then continues.  Default 3600s; raise via JY_TASK_BUDGET_S to allow a
# genuinely large but healthy task to finish.
TASK_BUDGET_SECONDS = _env_int("JY_TASK_BUDGET_S", 1200, 60)
TIMEOUT_ESCALATION_MULT = _env_int("JY_TIMEOUT_ESCALATION", 2, 1)
TRANSIENT_BUILD_MARKERS = queue_contracts.TRANSIENT_BUILD_MARKERS


def log(*args):
    line = " ".join(str(a) for a in args)
    with _OUTPUT_LOCK:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%m-%d %H:%M:%S')}] {line}\n")
        try:
            print(line, flush=True)
        except (OSError, UnicodeError):
            # A redirected Windows/SSH console may reject a GBK byte sequence
            # or close while the build is still running. Logging must never
            # replace the real build/validation failure with worker ERROR.
            try:
                sys.stdout.write(line.encode("unicode_escape", errors="backslashreplace").decode("ascii") + "\n")
                sys.stdout.flush()
            except (OSError, UnicodeError):
                pass


def progress(entry: dict):
    entry["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    if str(SKILL) not in sys.path:
        sys.path.insert(0, str(SKILL))
    from orchestrator.file_safety import file_lock
    with _OUTPUT_LOCK, file_lock(PROGRESS):
        with PROGRESS.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    try:
        _write_status_snapshot()
    except Exception as exc:  # noqa: BLE001
        # Status publication must never stop the production worker.
        log("STATUS_SNAPSHOT_WARN %s" % exc)


def _read_json(path: pathlib.Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _acquire_record_lock(task_dir: pathlib.Path, run_id: str) -> pathlib.Path:
    """Prevent two worker processes from touching one record at once."""
    lock_path = task_dir / ".task_run.lock"
    payload = {"pid": os.getpid(), "process_created_at": process_created_at(os.getpid()), "run_id": run_id,
               "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "pipeline_version": _pipeline_version()}
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            return lock_path
        except FileExistsError:
            current = _read_json(lock_path)
            pid = current.get("pid")
            alive = False
            try:
                if pid and int(pid) == os.getpid():
                    alive = True
                elif pid:
                    _assert_process_alive(int(pid), current.get("process_created_at"))
                    alive = True
            except (OSError, TypeError, ValueError, ProcessLookupError):
                alive = False
            if alive:
                raise RuntimeError("TASK_ALREADY_RUNNING: record_id=%s run_id=%s pid=%s" % (
                    task_dir.name, current.get("run_id") or "unknown", pid))
            try:
                lock_path.unlink()
            except OSError:
                raise RuntimeError("TASK_LOCK_STALE_UNCLEAR: record_id=%s" % task_dir.name)
    raise RuntimeError("TASK_ALREADY_RUNNING: record_id=%s" % task_dir.name)


def _release_record_lock(lock_path: pathlib.Path | None, run_id: str) -> None:
    if not lock_path:
        return
    current = _read_json(lock_path)
    if current.get("run_id") == run_id:
        try:
            lock_path.unlink()
        except OSError:
            pass


def _qc_state(task_dir: pathlib.Path, drafts: list[str], done: dict) -> tuple[str, int, list[str]]:
    """Summarize QC without allowing missing reports to look like RUNNING."""
    reports = []
    qc_refs = done.get("qc_reports") or []
    for index, draft in enumerate(drafts, 1):
        report_path = None
        if index <= len(qc_refs) and isinstance(qc_refs[index - 1], dict):
            report_ref = str(qc_refs[index - 1].get("report") or "")
            parts = pathlib.PureWindowsPath(report_ref).parts
            lowered = [part.lower() for part in parts]
            try:
                work_index = lowered.index("work")
                if work_index + 1 < len(parts) and parts[work_index + 1] == task_dir.name:
                    report_path = task_dir.joinpath(*parts[work_index + 2:])
            except ValueError:
                pass
        if report_path is None:
            report_dir = task_dir / "report" / (str(draft) if index == 1 else "report_%d" % index)
            report_path = report_dir / "draft_visual_qc.json"
        payload = _read_json(report_path)
        reports.append(str(payload.get("status") or "UNCERTIFIED").upper() if payload else "MISSING")
    if not reports:
        return "UNCERTIFIED", 0, reports
    if all(item == "PASS" for item in reports):
        return "CERTIFIED", len(reports), reports
    if any(item == "FAIL" for item in reports):
        return "FAIL", len(reports), reports
    return "UNCERTIFIED", len(reports), reports


def _record_lock_is_live(task_dir: pathlib.Path) -> bool:
    """A lock owned by a live, matching process identifies another worker."""
    owner = _read_json(task_dir / ".task_run.lock")
    try:
        pid = int(owner.get("pid") or 0)
        if pid <= 0:
            return False
        _assert_process_alive(pid, owner.get("process_created_at"))
        return True
    except (OSError, TypeError, ValueError, ProcessLookupError):
        return False


def _write_status_snapshot() -> None:
    """Serialize read, reduction and atomic publication across worker processes."""
    if str(SKILL) not in sys.path:
        sys.path.insert(0, str(SKILL))
    from orchestrator.file_safety import file_lock
    with file_lock(STATUS_SNAPSHOT):
        _write_status_snapshot_locked()


def _write_status_snapshot_locked() -> None:
    """Terminal done.json beats old start events unless a fresh owner is live."""
    latest: dict[str, dict] = {}
    if PROGRESS.is_file():
        for raw in PROGRESS.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except (ValueError, TypeError):
                continue
            rid = str(row.get("record_id") or "").strip()
            if rid:
                latest[rid] = row
    tasks = {}
    for task_dir in sorted(WORK.iterdir() if WORK.exists() else []):
        if not task_dir.is_dir():
            continue
        rid = task_dir.name
        event = latest.get(rid, {})
        done_path = task_dir / "done.json"
        done = _read_json(done_path) if done_path.is_file() else {}
        drafts = [str(item) for item in (done.get("drafts") or [])]
        qc_status, qc_count, qc_rows = _qc_state(task_dir, drafts, done)
        terminal = str(done.get("status") or "").upper()
        done_finished_at = str(done.get("finished_at") or "")
        live_worker = rid == ACTIVE_RECORD_ID or _record_lock_is_live(task_dir)
        new_attempt = (
            live_worker
            and str(event.get("event") or "").lower() == "start"
            and str(event.get("ts") or "") > done_finished_at
        )
        if new_attempt:
            state, source = "RUNNING", "live_worker_new_attempt"
        elif done_path.is_file() and terminal in {"OK", "PARTIAL", "PREVIEW_READY", "ERROR", "DEFERRED"}:
            state, source = terminal, "done.json"
        elif (str(event.get("event") or "").lower() == "start"
              and live_worker):
            state, source = "RUNNING", "live_worker_event"
        elif event:
            state, source = "STALE_ORPHANED", "historical_progress_only"
        else:
            state, source = "NOT_STARTED", "no_evidence"
        tasks[rid] = {
            "record_id": rid,
            "state": state,
            "terminal_status": terminal or None,
            "terminal_state": done.get("terminal_state"),
            "run_id": done.get("run_id") or event.get("run_id"),
            "parent_run_id": done.get("parent_run_id") or event.get("parent_run_id"),
            "failure_class": done.get("failure_class") or event.get("failure_class"),
            "failure_code": done.get("failure_code") or event.get("failure_code"),
            "retryable": bool(done.get("retryable", event.get("retryable", False))),
            "safe_to_continue": bool(done.get("safe_to_continue", event.get("safe_to_continue", False))),
            "next_action": done.get("next_action") or event.get("next_action"),
            "certification": qc_status,
            "qc_report_count": qc_count,
            "qc_reports": qc_rows,
            "done_path": str(done_path) if done_path.is_file() else None,
            "source": source,
            "last_event": event,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    payload = {
        "version": "status-reducer-v1",
        "rule": "done.json > live process > historical start",
        "worker_pid": os.getpid(),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tasks": tasks,
    }
    from orchestrator.file_safety import atomic_write_text
    atomic_write_text(STATUS_SNAPSHOT, json.dumps(payload, ensure_ascii=False, indent=2))


def video_files(material_dir: str) -> list[str]:
    d = pathlib.Path(material_dir)
    if not d.is_dir():
        return []
    hits = []
    # 素材目录可能是「父目录/日期子目录/文件」两级结构（实测：闪闪洗脸巾\2026.09.15），
    # 只列顶层会漏子目录；递归但限制深度为 3，避免把无关内容卷进来。
    for p in sorted(d.rglob("*")):
        if not p.is_file():
            continue
        try:
            depth = len(p.relative_to(d).parts)
        except ValueError:
            continue
        if depth <= 3 and p.suffix.lower() in VIDEO_EXTS:
            hits.append(str(p))
    return hits


def calibrated_source_limit() -> int | None:
    """Read the render host's last verified source limit, if available."""
    try:
        data = json.loads(CAPACITY_PROFILE.read_text(encoding="utf-8"))
        limit = int(data.get("safe_source_limit") or 0)
        return limit if limit > 0 else None
    except (OSError, ValueError, TypeError):
        return None


def validate_visual_match(report_dir: pathlib.Path) -> tuple[bool, str]:
    """Fail closed unless every selected script claim has real visual evidence."""
    analysis_path = report_dir / "shot_candidates.json"
    match_path = report_dir / "shot_match_report.json"
    if not analysis_path.is_file():
        return False, "VISUAL_MATCH_BLOCKED: shot_candidates.json missing"
    if not match_path.is_file():
        return False, "VISUAL_MATCH_BLOCKED: shot_match_report.json missing"
    try:
        analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
        match = json.loads(match_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return False, "VISUAL_MATCH_BLOCKED: report unreadable: %s" % exc

    vision = analysis.get("vision_analysis") or {}
    segments = match.get("segments") or []
    mixed_rule_ai = match.get("selection_mode") == "rule_then_ai"
    if ((match.get("manifest") or {}).get("selection_mode") == "ai_editor"
            or match.get("selection_mode") == "ai_editor"
            or any(isinstance(s, dict) and "ai_selection" in s for s in segments)):
        contract = production_eval.ai_editor_contract
        if (match.get("ok") is not True or not isinstance(segments, list) or not segments
                or match.get("selection_mode") not in ("ai_editor", "rule_then_ai")
                or (mixed_rule_ai and (match.get("manifest") or {}).get("selection_mode") != "rule_then_ai")
                or contract.digest(segments) != contract.digest((match.get("manifest") or {}).get("segments"))):
            return False, "VISUAL_MATCH_BLOCKED: AI selection report identity invalid"
        candidates = {s.get("shot_id"): s for s in analysis.get("shots", []) if isinstance(s, dict)}
        for index, segment in enumerate(segments, 1):
            if mixed_rule_ai and isinstance(segment, dict) and segment.get("selection_mode") != "ai_editor" and "ai_selection" not in segment:
                continue  # Rule segments still pass every legacy check below.
            if not isinstance(segment, dict) or segment.get("selection_mode") != "ai_editor":
                return False, "VISUAL_MATCH_BLOCKED: mixed AI/legacy segments"
            check = contract.validate_segment_receipt(segment, index=index, verify_source=True, manifest=match.get("manifest"))
            if not check["ok"]:
                return False, "VISUAL_MATCH_BLOCKED: " + ";".join(check["reasons"])
            for part in segment.get("span_parts") or [segment]:
                sid = part.get("shot_id") or part.get("temporary_shot_id")
                if sid not in candidates or str(part.get("video")) != str(candidates[sid].get("video")):
                    return False, "VISUAL_MATCH_BLOCKED: AI selection outside task source catalog"
        sequence = contract.sequence_errors([s for s in segments if isinstance(s, dict) and s.get("selection_mode") == "ai_editor"])
        if sequence:
            return False, "VISUAL_MATCH_BLOCKED: " + ";".join(sequence)
        if not mixed_rule_ai:
            return True, "ai_visual_match_valid_independent_qc_required"
    errors = vision.get("errors") or []
    shots = analysis.get("shots") or []
    ready = [row for row in shots
             if isinstance(row, dict) and row.get("status") == "ready_for_matching"]
    if errors:
        first = errors[0] if isinstance(errors, (list, tuple)) else errors
        return False, "VISUAL_MATCH_BLOCKED: vision errors: %s" % str(first)[:600]
    if not shots:
        return False, "VISUAL_MATCH_BLOCKED: no candidate shots analysed"
    empty_desc = [row for row in shots if not isinstance(row, dict)
                  or not str(row.get("description") or "").strip()]
    if empty_desc:
        return False, "VISUAL_MATCH_BLOCKED: %d/%d candidate shots lack visual descriptions" % (len(empty_desc), len(shots))
    if not ready:
        return False, "VISUAL_MATCH_BLOCKED: no shots are ready_for_matching"

    delivery_manifest = match.get("manifest") or {}
    preview_mode = str(delivery_manifest.get("delivery_mode") or "").lower() in (
        "preview", "preview_only", "预览")
    preview_degraded_counters = {
        "visual_missing_count", "material_gap_count", "degraded_count",
        "degraded_fallback_count", "preview_unresolved_count"
    }
    for counter, label in (
        ("visual_missing_count", "visual-missing segments"),
        ("material_gap_count", "material gaps"),
        ("preview_unresolved_count", "unresolved previews"),
        ("degraded_count", "degraded matches"),
        ("degraded_fallback_count", "degraded fallbacks"),
    ):
        try:
            count = int(match.get(counter) or 0)
        except (TypeError, ValueError):
            return False, "VISUAL_MATCH_BLOCKED: invalid %s" % counter
        if count and not (preview_mode and counter in preview_degraded_counters):
            return False, "VISUAL_MATCH_BLOCKED: %s reported (%d)" % (label, count)
    for key, label in (
        ("visual_missing_matches", "visual-missing matches"),
        ("material_gaps", "material gaps"),
        ("material_gap_claims", "uncovered script claims"),
        ("preview_unresolved", "unresolved previews"),
        ("degraded_matches", "degraded matches"),
    ):
        rows = match.get(key) or []
        if rows and not (preview_mode and key in {
                "visual_missing_matches", "material_gaps", "material_gap_claims",
                "preview_unresolved", "degraded_matches"}):
            return False, "VISUAL_MATCH_BLOCKED: %s reported (%d)" % (label, len(rows))

    if match.get("ok") is not True:
        return False, "VISUAL_MATCH_BLOCKED: shot match report not ok"
    coverage = match.get("claim_gate_coverage") or {}
    if (coverage.get("certification") != "CERTIFIED"
            or coverage.get("validation_passed") is not True):
        # Preview delivery is explicitly allowed to carry a related,
        # lower-confidence shot.  The selected segment/path/description
        # checks below still reject broken or empty media; this branch only
        # stops the claim-gate certification from blocking the whole queue.
        if not preview_mode:
            return False, "VISUAL_MATCH_BLOCKED: claim gate not certified (%s)" % str(
                coverage.get("uncertified_reasons") or coverage.get("coverage_status") or "missing")
    segments = match.get("segments") or []
    if not segments:
        return False, "VISUAL_MATCH_BLOCKED: shot match report has no segments"
    accepted = match.get("accepted_shots") or []
    if not isinstance(accepted, list):
        return False, "VISUAL_MATCH_BLOCKED: accepted_shots is not a list"
    accepted_ids = set()
    for item in accepted:
        if isinstance(item, str):
            accepted_ids.add(item.strip())
        elif isinstance(item, dict):
            shot_id = item.get("temporary_shot_id") or item.get("shot_id")
            if shot_id:
                accepted_ids.add(str(shot_id).strip())
    required_segments = [row for row in segments
                         if isinstance(row, dict) and row.get("visual_missing") is not True]
    if len(accepted_ids) < len(required_segments):
        return False, "VISUAL_MATCH_BLOCKED: accepted shots fewer than non-missing script segments"

    from math import isfinite
    seen_ids = set()
    placeholders = {"", "n/a", "na", "none", "null", "unknown", "tbd",
                    "placeholder", "visual_missing", "unmatched"}
    for index, row in enumerate(segments, 1):
        if not isinstance(row, dict):
            return False, "VISUAL_MATCH_BLOCKED: segment %d is malformed" % index
        if (row.get("preview_unresolved") is True
                and row.get("visual_missing") is not True
                and not preview_mode):
            return False, "VISUAL_MATCH_BLOCKED: segment %d has missing/unresolved visuals" % index
        claim = str(row.get("claim_text") or row.get("text") or "").strip()
        if row.get("visual_missing") is True:
            if not preview_mode or not claim or str(row.get("selection_mode") or "") != "visual_missing":
                return False, "VISUAL_MATCH_BLOCKED: segment %d invalid intentional visual gap" % index
            duration = row.get("duration")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not isfinite(duration) or duration <= 0:
                return False, "VISUAL_MATCH_BLOCKED: segment %d invalid visual-gap timing" % index
            continue
        shot_id = str(row.get("temporary_shot_id") or "").strip()
        video = str(row.get("video") or "").strip()
        visual = str(row.get("visual_description") or "").strip()
        if not claim or not shot_id or not video or not visual or visual.casefold() in placeholders:
            return False, "VISUAL_MATCH_BLOCKED: segment %d lacks claim, shot id, video path, or visual description" % index
        if shot_id not in accepted_ids:
            return False, "VISUAL_MATCH_BLOCKED: segment %d shot is not in accepted_shots" % index
        if shot_id in seen_ids:
            return False, "VISUAL_MATCH_BLOCKED: temporary shot reused across segments"
        seen_ids.add(shot_id)
        if not os.path.isabs(video) or not os.path.isfile(video):
            return False, "VISUAL_MATCH_BLOCKED: segment %d video path missing/unreachable" % index
        source_start = row.get("source_start")
        duration = row.get("duration")
        if (not isinstance(source_start, (int, float)) or isinstance(source_start, bool)
                or not isfinite(source_start) or source_start < 0
                or not isinstance(duration, (int, float)) or isinstance(duration, bool)
                or not isfinite(duration) or duration <= 0):
            return False, "VISUAL_MATCH_BLOCKED: segment %d lacks valid placement/timing" % index
        mode = str(row.get("selection_mode") or "").casefold()
        if ((row.get("degraded_no_match") is True or "degraded" in mode or "fallback" in mode)
                and not preview_mode):
            return False, "VISUAL_MATCH_BLOCKED: segment %d uses degraded/fallback matching" % index
        gate = row.get("claim_gate") or {}
        gate_required = gate.get("required") if isinstance(gate, dict) else {}
        claim_requirements = row.get("required_claims") or []
        gate_blocked = (isinstance(gate, dict)
                and (gate.get("ok") is False or str(gate.get("status") or "").casefold() in {"blocked", "failed", "error"})
                or isinstance(gate_required, dict) and gate_required.get("material_gap") is True
                or any(isinstance(req, dict) and req.get("material_gap") is True for req in claim_requirements))
        if gate_blocked and not (preview_mode and row.get("degraded_no_match") is True):
            return False, "VISUAL_MATCH_BLOCKED: segment %d claim gate blocked/uncovered" % index
    return True, "visual_match_valid"

safe_manifest_stem = queue_contracts.safe_manifest_stem


def classify_build_failure(text: str) -> dict:
    return queue_contracts.classify_build_failure(
        text, transient_markers=TRANSIENT_BUILD_MARKERS)


_is_reusable_analysis_row = queue_contracts.is_reusable_analysis_row


def run_build(manifest: dict, report_dir: pathlib.Path, draft_name: str, *,
              timeout: int | None = None, reuse_analysis: str | None = None,
              attempt: int = 1, force_rebuild: bool = False) -> dict:
    manifest = dict(manifest)
    manifest["draft_name"] = draft_name
    manifest["delivery_mode"] = requested_mode(manifest)
    from tools.workflow import selection_recovery
    if force_rebuild:
        selection_recovery.archive_state(report_dir)
    if not force_rebuild and selection_recovery.pending(report_dir):
        env = dict(os.environ, PYTHONUTF8="1")
        deadline = time.time() + (timeout or BUILD_TIMEOUT_SECONDS)
        inherited = env.get("JY_TASK_DEADLINE_TS") or env.get("JY_TASK_DEADLINE_EPOCH")
        if inherited:
            deadline = min(deadline, float(inherited))
        env["JY_STAGE_DEADLINE_TS"] = env["JY_STAGE_DEADLINE_EPOCH"] = str(deadline)
        log("resume selection only from frozen narration and observations: %s" % report_dir)
        return selection_recovery.recover(manifest, report_dir,
            prefix=[PY, "-m", "orchestrator.cli"], run=subprocess.run, env=env,
            cwd=str(SKILL), deadline=deadline, drafts_root=DRAFTS)
    if reuse_analysis:
        try:
            cached_payload = json.loads(pathlib.Path(reuse_analysis).read_text(
                encoding="utf-8"))
            cached_shots = (cached_payload.get("shots") or []
                            if isinstance(cached_payload, dict) else [])
            reusable = [row for row in cached_shots
                        if _is_reusable_analysis_row(row)]
            if reusable:
                # The orchestrator consumes inline `shot_analysis`; the old
                # `shot_analysis_file` manifest key was ignored, so retries
                # re-ran vision despite a persisted candidate report.
                manifest["shot_analysis"] = {"shots": cached_shots}
                log("reuse partial shot analysis rows=%d/%d from %s" % (
                    len(reusable), len(cached_shots), reuse_analysis))
        except (OSError, ValueError, TypeError, AttributeError):
            log("cached shot analysis unavailable; continue with material cache")
    report_dir.mkdir(parents=True, exist_ok=True)
    mpath = report_dir.parent / (f"manifest.{safe_manifest_stem(draft_name)}"
                                 f".a{attempt}.json")
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    cmd = [PY, "-m", "orchestrator.cli", "build",
           "--input", str(mpath), "--report-dir", str(report_dir),
           "--delivery-mode", manifest["delivery_mode"]]
    env = dict(os.environ, PYTHONUTF8="1")
    if str(SKILL) not in sys.path:
        sys.path.insert(0, str(SKILL))
    from orchestrator.runtime import vision_config
    env["JY_VISION_TRANSPORT"] = vision_config.transport()
    vision_model = vision_config.model()
    timeout = timeout or BUILD_TIMEOUT_SECONDS
    deadline = time.time() + timeout
    inherited = env.get("JY_TASK_DEADLINE_TS") or env.get("JY_TASK_DEADLINE_EPOCH")
    if inherited:
        deadline = min(deadline, float(inherited))
    timeout = max(0.001, deadline - time.time())
    env["JY_STAGE_DEADLINE_EPOCH"] = str(deadline)
    env["JY_STAGE_DEADLINE_TS"] = str(deadline)
    log("build draft=%s vision_transport=%s vision_model=%s timeout=%ss reuse=%s attempt=%s" % (
        draft_name, env["JY_VISION_TRANSPORT"], vision_model or "unconfigured", timeout,
        "yes" if reuse_analysis else "no", attempt))
    previous_selection_run = _read_json(report_dir / "selection_resume.json").get("run_id")
    try:
        p = subprocess.run(cmd, cwd=str(SKILL), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        retained = selection_recovery.selection_timeout(report_dir, previous_run_id=previous_selection_run)
        if retained:
            return {**retained, "manifest": str(mpath)}
        if selection_recovery.completed(report_dir, previous_run_id=previous_selection_run):
            failure = {"status": "DRAFT_WRITE_TIMEOUT", "failure_class": "draft", "retryable": False,
                       "resume_stage": "draft_write", "reason": "DRAFT_WRITE_DEADLINE_EXCEEDED"}
            (report_dir / "last_result.json").write_text(json.dumps(failure), encoding="utf-8")
            return {"rc": 1, "stdout": json.dumps(failure), "stderr": "", "combined": json.dumps(failure)}
        (report_dir / "shot_match_report.json").write_text(json.dumps({
            "ok": False, "input_manifest": manifest, "status": "BUILD_DEADLINE_EXCEEDED",
        }, ensure_ascii=False), encoding="utf-8")
        raise
    out = (p.stdout or "") + "\n" + (p.stderr or "")
    return {"rc": p.returncode, "stdout": (p.stdout or "")[-20000:],
            "stderr": (p.stderr or "")[-20000:], "manifest": str(mpath),
            "combined": out}


def run_post_draft_qc(draft_dir: pathlib.Path, report_dir: pathlib.Path) -> dict:
    """Run the independent ChatCut-style visual acceptance after build.

    The build/match gate checks the plan. This second gate reads the generated
    JianYing draft and fresh frames from the actual placed source ranges, then
    runs an independent visual judgment. It is deliberately after build and
    before NAS shipping.
    """
    output = report_dir / "draft_visual_qc.json"
    def finish(payload: dict) -> dict:
        from orchestrator.file_safety import atomic_write_text
        status = payload.get("status") or "UNCERTIFIED"
        atomic_write_text(report_dir / "qc_stage.json", json.dumps({
            "draft": str(draft_dir), "report_dir": str(report_dir),
            "status": status, "completed": status == "PASS",
            "failed_stage": None if status == "PASS" else "qc",
            "resume_stage": None if status == "PASS" else "qc",
            "updated_at": time.time()}, ensure_ascii=False, indent=2))
        return payload
    if str(SKILL) not in sys.path:
        sys.path.insert(0, str(SKILL))
    if not POST_DRAFT_QC.is_file():
        return finish({"rc": 2, "status": "UNCERTIFIED",
                "message": "DRAFT_VISUAL_QC_BLOCKED: script missing",
                "output": str(output)})
    cmd = [PY, str(POST_DRAFT_QC), "--draft", str(draft_dir),
           "--report-dir", str(report_dir), "--output", str(output)]
    if str(SKILL) not in sys.path:
        sys.path.insert(0, str(SKILL))
    from orchestrator.runtime_policy import load_policy
    policy = load_policy()
    request_timeout = policy.request_timeout
    stage_timeout = policy.qc_stage_timeout
    task_deadline = (os.environ.get("JY_TASK_DEADLINE_TS") or os.environ.get("JY_TASK_DEADLINE_EPOCH", "")).strip()
    if task_deadline:
        stage_timeout = min(stage_timeout, max(0, float(task_deadline) - time.time()))
    if stage_timeout <= 0:
        return finish({"rc": 2, "status": "UNCERTIFIED_TIMEOUT",
                "message": "TASK_DEADLINE_EXCEEDED: draft retained for deferred QC",
                "output": str(output), "nonblocking_timeout": True,
                "failed_stage": "qc", "resume_stage": "qc"})
    stage_deadline = time.time() + stage_timeout
    env = dict(os.environ, PYTHONUTF8="1",
               JY_POST_QC_TIMEOUT_S=str(int(request_timeout)) if float(request_timeout).is_integer() else str(request_timeout),
               JY_POST_QC_STAGE_TIMEOUT_S=str(stage_timeout),
               JY_STAGE_DEADLINE_EPOCH=str(stage_deadline),
               JY_STAGE_DEADLINE_TS=str(stage_deadline))
    finish({"status": "RUNNING"})
    try:
        p = subprocess.run(cmd, cwd=str(SKILL), env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=stage_timeout + 30)
    except subprocess.TimeoutExpired as exc:
        # A provider timeout is not the same as a visual mismatch.  The draft
        # has already passed the build/match gate; archive it as an explicitly
        # uncertified preview so one slow QC call cannot rebuild the same draft
        # and block the queue.  Other QC failures remain hard blockers.
        return finish({"rc": 2, "status": "UNCERTIFIED_TIMEOUT",
                "message": "POST_DRAFT_VISION_QC_TIMEOUT: post-draft visual QC timeout (%ss); draft retained for deferred QC" % exc.timeout,
                "output": str(output), "nonblocking_timeout": True,
                "failed_stage": "qc", "resume_stage": "qc"})
    payload = {}
    if output.is_file():
        try:
            payload = json.loads(output.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    status = production_eval.visual_qc_contract.report_status(payload)
    if p.returncode != 0 and status in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}:
        status = "UNCERTIFIED"
        payload.setdefault("issues", []).append("QC_CHILD_EXIT_NONZERO")
    if status in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"} and not production_eval.visual_qc_contract.report_matches_draft(payload, draft_dir):
        status = "UNCERTIFIED"
        payload.setdefault("issues", []).append("QC_REPORT_CURRENT_DRAFT_BINDING_MISMATCH")
    issues_text = " ".join(str(item) for item in (payload.get("issues") or []))
    if p.returncode != 0 or status not in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}:
        error_codes = {row.get("error_code") for row in payload.get("batch_results", []) if isinstance(row, dict)}
        if status != "FAIL" and error_codes & {"REQUEST_TIMEOUT", "DEADLINE_EXCEEDED", "LIMIT_WAIT_TIMEOUT", "LEDGER_WAIT_TIMEOUT"}:
            return finish({"rc": 2, "status": "UNCERTIFIED_TIMEOUT",
                    "message": "POST_DRAFT_VISION_QC_TIMEOUT: internal visual call timed out; draft retained for deferred QC",
                    "output": str(output), "report": payload,
                    "nonblocking_timeout": True, "failed_stage": "qc", "resume_stage": "qc"})
        issues = payload.get("issues") or ((p.stdout or "") + (p.stderr or ""))[-1200:]
        return finish({"rc": p.returncode or 2, "status": status,
                "message": "DRAFT_VISUAL_QC_BLOCKED: %s" % str(issues)[-1600:],
                "output": str(output), "report": payload})
    return finish({"rc": 0, "status": status, "output": str(output),
            "report": payload})


def resume_task_qc(task: dict) -> dict:
    """Resume retained final drafts through QC and the existing delivery gates.

    This entry never invokes TTS, shot selection or draft construction. An
    unfinished requested output remains PARTIAL rather than being synthesized.
    """
    if str(SKILL) not in sys.path:
        sys.path.insert(0, str(SKILL))
    from orchestrator.file_safety import atomic_write_text
    rid = str(task.get("record_id") or "")
    if not rid or pathlib.Path(rid).name != rid:
        raise ValueError("invalid record_id")
    task_dir = WORK / rid
    task_dir.mkdir(parents=True, exist_ok=True)
    run_id = "%s-qc-%s" % (rid, uuid.uuid4().hex[:8])
    record_lock = _acquire_record_lock(task_dir, run_id)
    try:
        previous = _read_json(task_dir / "done.json")
        retained = {}
        for state_path in (task_dir / "report").glob("**/qc_stage.json"):
            state = _read_json(state_path)
            draft = pathlib.Path(str(state.get("draft") or ""))
            if draft.is_dir():
                retained[draft.name] = (draft, state_path.parent)
        for index, name in enumerate(previous.get("drafts") or [], 1):
            draft = DRAFTS / str(name)
            report = task_dir / "report" / (str(task.get("title") or rid) if index == 1 else "report_%d" % index)
            if draft.is_dir():
                retained[str(name)] = retained.get(str(name), (draft, report))
        if not retained:
            raise ValueError("QC_RESUME_NO_RETAINED_DRAFT")
        reports = []
        for name, (draft, report_dir) in retained.items():
            answer = run_post_draft_qc(draft, report_dir)
            reports.append({"draft": name, "status": answer.get("status"),
                            "report": answer.get("output")})
        all_pass = all(row["status"] == "PASS" for row in reports)
        payload = dict(previous, record_id=rid, drafts=list(retained), qc_reports=reports,
                       status="PREVIEW_READY" if all_pass else "PARTIAL",
                       resume_stage="delivery" if all_pass else "qc",
                       terminal_state="QC_READY_FOR_DELIVERY" if all_pass else "QC_PENDING",
                       failed_stage=None if all_pass else "qc", safe_to_continue=True,
                       finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        atomic_write_text(task_dir / "done.json", json.dumps(payload, ensure_ascii=False, indent=2))
        if all_pass and len(retained) >= max(1, int(task.get("count") or 1)) and not SKIP_SHIP:
            result = finalize_closed_loop(task_dir, task, list(retained), reports)
            payload = _read_json(task_dir / "done.json")
            payload["resume_stage"] = None if result.get("ok") else "delivery"
            payload["terminal_state"] = "DELIVERED" if result.get("ok") else "QC_READY_FOR_DELIVERY"
            payload["safe_to_continue"] = True
            atomic_write_text(task_dir / "done.json", json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    finally:
        _release_record_lock(record_lock, run_id)


def ship_one(draft_dir: pathlib.Path, task_dir: pathlib.Path | None = None,
             frozen: dict | None = None, *, context: DeliveryContext | None = None) -> dict:
    """Ship a verified copy so path rewriting never mutates the reviewed draft."""
    skill = context.skill_root if context else SKILL
    work = context.work_root if context else WORK
    python = context.python if context else PY
    require_ship_dependencies(skill.parent)
    source_identity = production_eval.output_identity([draft_dir])
    task_dir = task_dir or (work / '.manual-ship-stage')
    staging = task_dir / 'shipping-stage' / (uuid.uuid4().hex) / draft_dir.name
    from tools.native_drafts import is_native
    if is_native(draft_dir):
        from tools.native_delivery import ship as ship_native
        try:
            payload = ship_native(draft_dir, staging)
            if production_eval.output_identity([draft_dir]) != source_identity:
                raise RuntimeError('SOURCE_CHANGED_DURING_DELIVERY')
            receipt = {'schema_version': 1, 'status': 'VERIFIED', 'draft_name': draft_dir.name,
                'task_id': (frozen or {}).get('task_id'), 'run_id': (frozen or {}).get('run_id'),
                'contract_hash': (frozen or {}).get('contract_hash'),
                'source_output_sha256': source_identity['sha256'], 'staging_path': str(staging),
                'nas_target': payload['dist_target'], 'transformed_tree_sha256': payload['staged_tree_sha256'],
                'transformed_file_count': payload['staged_file_count'], 'nas_root_verified': True,
                'distribution_verified': True, 'shared_media_references_verified': True,
                'shared_reference_root': payload['shared_reference_root'], 'draft_backend': 'headless'}
            return {'rc': 0, 'out': 'HEADLESS_SHARED_COPY_VERIFIED', 'receipt': receipt, 'distribution': payload}
        except (OSError, ValueError, RuntimeError) as exc:
            return {'rc': 2, 'out': str(exc), 'staging_path': str(staging),
                    'source_output_sha256': source_identity['sha256']}
    cmd = [python, str(skill / "orchestrator" / "ship_distribute.py"),
           "--draft", str(draft_dir), "--style", "unc", "--work", str(staging.parent)]
    env = dict(os.environ, PYTHONUTF8="1")
    p = subprocess.run(cmd, cwd=str(skill), env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=600)
    raw = (p.stdout or "").strip().splitlines()
    payload = None
    for line in reversed(raw):
        try:
            candidate = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if (p.returncode != 0 or not payload or payload.get('status') != 'OK'
            or payload.get('nas_root_verified') is not True
            or payload.get('distribution_verified') is not True
            or payload.get('shared_media_references_verified') is not True
            or not payload.get('dist_target')):
        return {"rc": p.returncode or 2, "out": (p.stdout or p.stderr or "")[-1200:],
                "staging_path": str(staging), "source_output_sha256": source_identity['sha256'],
                "distribution": payload or {"status": "UNCERTIFIED"}}
    if production_eval.output_identity([draft_dir]) != source_identity:
        return {"rc": 2, "out": 'SOURCE_CHANGED_DURING_DELIVERY',
                "staging_path": str(staging), "source_output_sha256": source_identity['sha256']}
    receipt = {
        'schema_version': 1, 'status': 'VERIFIED',
        'draft_name': draft_dir.name,
        'task_id': (frozen or {}).get('task_id'), 'run_id': (frozen or {}).get('run_id'),
        'contract_hash': (frozen or {}).get('contract_hash'),
        'source_output_sha256': source_identity['sha256'],
        'staging_path': str(staging),
        'nas_target': payload['dist_target'],
        'transformed_tree_sha256': payload['staged_tree_sha256'],
        'transformed_file_count': payload['staged_file_count'],
        'nas_root_verified': payload['nas_root_verified'],
        'distribution_verified': payload['distribution_verified'],
        'shared_media_references_verified': payload['shared_media_references_verified'],
        'shared_reference_root': payload['shared_reference_root'],
    }
    return {"rc": 0, "out": (p.stdout or "")[-600:], "receipt": receipt,
            "distribution": payload}


def _save_frozen_task(task_dir: pathlib.Path, task: dict, frozen: dict) -> None:
    """Persist exactly the normalized task bound to the active run contract."""
    if (str(task.get("task_id") or task.get("record_id") or "").strip() != str(frozen.get("task_id") or "")
            or production_eval.digest(task) != frozen.get("task_sha256")):
        raise ValueError("TASK_CONTRACT_CONTENT_MISMATCH")
    from orchestrator.file_safety import atomic_write_text
    atomic_write_text(task_dir / "task.json", json.dumps(task, ensure_ascii=False, indent=2))


def finalize_closed_loop(task_dir: pathlib.Path, task: dict, drafts: list[str],
                         qc_reports: list[dict], delivery_receipts: list[dict] | None = None, *,
                         context: DeliveryContext | None = None) -> dict:
    explicit = context is not None
    context = context or DeliveryContext(
        draft_root=DRAFTS, workflow_root=PIPE, skill_root=SKILL, work_root=WORK,
        runtime_root=pathlib.Path(os.environ.get("JY_EVAL_RUNTIME_ROOT", str(PIPE_ROOT))),
        closed_loop_tool=CLOSED_LOOP_TOOL, closed_loop_policy=CLOSED_LOOP_POLICY,
        python=PY)
    evaluate = (lambda *args: evaluate_task(*args, context=context)) if explicit else evaluate_task
    ship = (lambda *args, **kwargs: ship_one(*args, context=context, **kwargs)) if explicit else ship_one
    return acceptance_finalization.finalize_closed_loop(
        task_dir, task, drafts, qc_reports, delivery_receipts, context=context,
        read_json=_read_json, evaluate=evaluate, validate_package=validate_draft_package,
        ship=ship, run=subprocess.run, verify_shared=verify_shared, tree_identity=tree_identity)


def evaluate_task(task_dir: pathlib.Path, task: dict, drafts: list[str],
                  qc_reports: list[dict], frozen: dict, *, context: DeliveryContext | None = None) -> dict:
    runtime_root = context.runtime_root if context else pathlib.Path(os.environ.get("JY_EVAL_RUNTIME_ROOT", str(PIPE_ROOT)))
    return task_review.evaluate_task(
        task_dir, task, drafts, qc_reports, frozen, draft_root=context.draft_root if context else DRAFTS,
        runtime_root=runtime_root, package_validator=validate_draft_package,
        read_json=_read_json)


def publish_failure_notification(task_dir: pathlib.Path) -> dict:
    """Best-effort failure status/feedback; never blocks the production queue."""
    task_path = task_dir / "task.json"
    done_path = task_dir / "done.json"
    if not CLOSED_LOOP_TOOL.is_file() or not CLOSED_LOOP_POLICY.is_file():
        return {"ok": False, "reason": "CLOSED_LOOP_TOOL_OR_POLICY_MISSING"}
    command = [PY, str(CLOSED_LOOP_TOOL), "failure-update",
               "--task-json", str(task_path), "--done-json", str(done_path),
               "--policy", str(CLOSED_LOOP_POLICY)]
    try:
        result = subprocess.run(command, cwd=str(PIPE), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=150)
        raw = (result.stdout or result.stderr or "")[-6000:]
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = {"ok": False, "reason": raw}
        payload.setdefault("returncode", result.returncode)
        log("FAILURE_NOTIFICATION record=%s ok=%s feedback_only=%s" % (
            task_dir.name, payload.get("ok"), payload.get("feedback_only_ok")))
        return payload
    except Exception as exc:  # noqa: BLE001
        # The task is already durably failed in done.json.  A notification
        # outage must not hold the next queue item.
        log("FAILURE_NOTIFICATION_DEFERRED record=%s reason=%s" % (task_dir.name, exc))
        return {"ok": False, "reason": str(exc)}


def validate_draft_package(draft_dir: pathlib.Path) -> dict:
    return task_review.validate_draft_package(draft_dir, video_extensions=VIDEO_EXTS)


def reusable_analysis_rows(candidate_report: pathlib.Path) -> tuple[int, int]:
    """Count auditable rows in an incremental shot report for retry reuse."""
    try:
        payload = json.loads(candidate_report.read_text(encoding="utf-8"))
        shots = payload.get("shots") or [] if isinstance(payload, dict) else []
    except (OSError, ValueError, TypeError, AttributeError):
        return 0, 0
    ready = sum(1 for row in shots if _is_reusable_analysis_row(row))
    return ready, len(shots)


def existing_task_has_nonpass_qc(task_dir: pathlib.Path) -> bool:
    """Detect stale OK markers whose persisted visual QC is not PASS.

    A resumed task must not trust ``done.json.status == OK`` by itself:
    older workers could retain a draft after a post-draft timeout and then
    incorrectly promote the task to OK on the next single-step run.
    Missing QC files are kept backward-compatible; an existing non-PASS file
    is an explicit reason to re-check the draft.
    """
    done = _read_json(task_dir / "done.json")
    drafts = [str(item) for item in (done.get("drafts") or [])]
    if not drafts:
        return False
    for index, draft in enumerate(drafts, 1):
        report_dir = task_dir / "report" / (draft if index == 1 else "report_%d" % index)
        path = report_dir / "draft_visual_qc.json"
        if not path.is_file():
            return True
        payload = _read_json(path)
        if (production_eval.visual_qc_contract.report_status(payload) != "PASS"
                or not production_eval.visual_qc_contract.report_matches_draft(payload, DRAFTS / draft)):
            return True
    return False


def prior_draft_requires_recheck(task_dir: pathlib.Path, index: int,
                                 base_draft: str, previous: dict) -> bool:
    """Return whether a retained draft has unresolved acceptance evidence."""
    deferred = set()
    for raw in (previous.get("deferred_draft_indices") or []):
        try:
            deferred.add(int(raw))
        except (TypeError, ValueError):
            continue
    if index in deferred:
        return True
    for failure in (previous.get("failures") or []):
        if not isinstance(failure, dict):
            continue
        try:
            failure_index = int(failure.get("index") or 0)
        except (TypeError, ValueError):
            failure_index = 0
        if failure_index == index:
            return True
    report_dir = task_dir / "report" / (base_draft if index == 1 else "report_%d" % index)
    qc_path = report_dir / "draft_visual_qc.json"
    if not qc_path.is_file():
        return True
    try:
        payload = json.loads(qc_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return True
    draft_name = base_draft if index == 1 else f"{base_draft}-{index}"
    return (production_eval.visual_qc_contract.report_status(payload) != "PASS"
            or not production_eval.visual_qc_contract.report_matches_draft(payload, DRAFTS / draft_name))


def qc_rejected_shots_from_report(rd: pathlib.Path, report: dict | None = None, *,
                                  expected_sources: list[dict] | None = None) -> dict:
    """Map QC-failed sentences to the exact shot ids the draft used there.

    Only rows whose QC claim text equals the match-report sentence are used;
    an ambiguous mapping is skipped rather than guessed.
    """
    report = report if report is not None else _read_json(rd / "draft_visual_qc.json")
    segments = _read_json(rd / "shot_match_report.json").get("segments") or []
    out = {}
    for row in (report or {}).get("model_review") or []:
        if not isinstance(row, dict) or row.get("visual_ok") is not False:
            continue
        try:
            index = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if not 1 <= index <= len(segments) or not isinstance(segments[index - 1], dict):
            continue
        segment = segments[index - 1]
        claim = str(row.get("claim") or "").strip()
        text = str(segment.get("text") or "").strip()
        if not claim or not text or claim != text:
            continue
        parts = segment.get("span_parts") or [segment]
        if expected_sources is not None and any(
                not isinstance(part, dict)
                or not isinstance(part.get("ai_selection"), dict)
                or part["ai_selection"].get("source_identity") not in expected_sources
                for part in parts):
            continue
        ids = {str(part.get("shot_id")) for part in (segment.get("span_parts") or [])
               if isinstance(part, dict) and part.get("shot_id")}
        if segment.get("shot_id"):
            ids.add(str(segment["shot_id"]))
        if ids:
            out.setdefault(index, set()).update(ids)
    return out


def qc_rejection_identity(manifest: dict) -> dict:
    """Bind retry exclusions to editing inputs and the actual local sources."""
    transient = {"draft_name", "force_overwrite", "parent_run_id", "recovery_stage",
                 "shot_analysis_file", "shot_analysis", "_selection_attempt",
                 "qc_rejected_shots", "variant_index", "variant_count",
                 "variant_pool_size", "variant_score_tolerance"}
    logical = {key: value for key, value in manifest.items() if key not in transient}
    sources = []
    for raw in manifest.get("material_sources") or []:
        path = pathlib.Path(str(raw)).resolve()
        try:
            info = path.stat()
            sources.append({"path": str(path), "size": info.st_size, "mtime_ns": info.st_mtime_ns})
        except OSError:
            sources.append({"path": str(path), "missing": True})
    return {"manifest_sha256": _payload_sha256(logical), "sources": sources}


def save_qc_rejections(rd: pathlib.Path, identity: dict, rejected: dict) -> None:
    """Checkpoint before another build can overwrite the latest QC evidence."""
    from orchestrator.file_safety import atomic_write_text
    atomic_write_text(rd / "qc_rejected_shots.json", json.dumps({
        "schema": "sentence-qc-rejections-v1", "identity": identity,
        "rejected_shots": {str(index): sorted(ids) for index, ids in sorted(rejected.items())},
    }, ensure_ascii=False, indent=2))


def restore_qc_rejections(rd: pathlib.Path, manifest: dict) -> tuple[dict, dict]:
    identity = qc_rejection_identity(manifest)
    saved = _read_json(rd / "qc_rejected_shots.json")
    rejected = {}
    if saved.get("schema") == "sentence-qc-rejections-v1" and saved.get("identity") == identity:
        rows = saved.get("rejected_shots")
        for key, values in (rows.items() if isinstance(rows, dict) else []):
            if (isinstance(key, str) and key.isdecimal() and int(key) > 0
                    and isinstance(values, list) and all(isinstance(v, str) and v for v in values)):
                rejected[int(key)] = set(values)
    # Recover a crash after QC wrote its report but before our checkpoint. Old
    # reports are usable only with the same input and still-current source receipts.
    report = _read_json(rd / "draft_visual_qc.json")
    match = _read_json(rd / "shot_match_report.json")
    prior_input = match.get("input_manifest")
    input_matches = (isinstance(prior_input, dict)
                     and qc_rejection_identity(prior_input)["manifest_sha256"] == identity["manifest_sha256"])
    if not input_matches and isinstance(prior_input, dict):
        # Selection-only recovery enriches the CLI input with frozen audio and
        # planning fields. Bind that exact derived input back to its original
        # worker request; do not discard arbitrary editorial fields to match it.
        frozen = _read_json(rd / "selection_resume.json")
        recovery = _read_json(rd / "selection_recovery_state.json")
        original, prepared = frozen.get("input_manifest"), frozen.get("manifest")
        if (frozen.get("run_id") and recovery.get("status") == "SUCCESS"
                and recovery.get("source_run_id") == frozen["run_id"]
                and isinstance(original, dict) and isinstance(prepared, dict)):
            expected = {**prepared, "parent_run_id": frozen["run_id"],
                        "draft_name": prior_input.get("draft_name"),
                        "narration_timing_file": str(rd / "narration_timing.json")}
            input_matches = (prior_input == expected
                             and qc_rejection_identity(original)["manifest_sha256"] == identity["manifest_sha256"])
    try:
        current = ((rd / "draft_visual_qc.json").stat().st_mtime_ns
                   >= (rd / "shot_match_report.json").stat().st_mtime_ns)
    except OSError:
        current = False
    if (current and input_matches
            and report.get("status")
            and str(report["status"]) not in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}):
        for index, ids in qc_rejected_shots_from_report(
                rd, report, expected_sources=identity["sources"]).items():
            rejected.setdefault(index, set()).update(ids)
    save_qc_rejections(rd, identity, rejected)
    return identity, rejected


def build_progress_inputs(manifest: dict, analysis_file: str | None, repair_indices) -> dict:
    """Track meaningful candidates/observations rather than output names or errors."""
    analysis = _read_json(pathlib.Path(analysis_file)) if analysis_file else {}
    fields = ("shot_id", "video", "source_start", "source_end", "visual_description",
              "description", "action", "object", "result", "evidence_intervals",
              "action_segments", "text_reads", "text_pass", "visual_observation_complete")
    sources = []
    for name in manifest.get("material_sources", []):
        path = pathlib.Path(str(name))
        try:
            stat = path.stat()
            sources.append({"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        except OSError:
            sources.append({"path": str(path), "missing": True})
    return {"sources": sources, "manifest": {key: value for key, value in manifest.items()
                         if key not in {"draft_name", "run_id", "attempt"}},
            "repairs": sorted(repair_indices),
            "candidates": [{key: row.get(key) for key in fields}
                           for row in analysis.get("shots", []) if isinstance(row, dict)]}


def process_task(task: dict, *, retry_partial: bool = False,
                 retry_error: bool = False, admitted: bool = False) -> None:
    global ACTIVE_RECORD_ID
    if not admitted:
        raise RuntimeError("DISPATCH_ADMISSION_REQUIRED: submit through tools.batch_dispatch")
    if str(SKILL) not in sys.path:
        sys.path.insert(0, str(SKILL))
    from orchestrator.task_inputs import normalize_meeting_task, manifest_meeting_fields
    task = normalize_meeting_task(task)
    rid = task["record_id"]
    run_id = "%s-%s-%s" % (rid, time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])
    parent_run_id = str(task.get("parent_run_id") or "").strip() or None
    ACTIVE_RECORD_ID = str(rid)
    force_rebuild = bool(task.get("force_rebuild"))
    task_id = str(task.get("task_id") or rid)
    manifest_sha256 = _payload_sha256(task)
    runtime = _runtime_facts()
    from orchestrator.runtime_policy import load_policy
    task_started = time.time()
    task_timeout = load_policy().task_timeout
    inherited_deadline = (os.environ.get("JY_TASK_DEADLINE_TS") or os.environ.get("JY_TASK_DEADLINE_EPOCH", "")).strip()
    task_deadline = min(task_started + task_timeout,
                        float(inherited_deadline) if inherited_deadline else float("inf"))
    os.environ["JY_TASK_DEADLINE_EPOCH"] = str(task_deadline)
    os.environ["JY_TASK_DEADLINE_TS"] = str(task_deadline)
    log("=" * 60)
    log("TASK %s title=%r count=%s voice=%s" % (rid, task.get("title"),
                                                task.get("count", 1),
                                                task.get("voice")))
    progress({"record_id": rid, "event": "start", "run_id": run_id,
              "parent_run_id": parent_run_id, "pipeline_version": _pipeline_version()})
    tdir = WORK / rid
    tdir.mkdir(parents=True, exist_ok=True)
    report_root = tdir / "report"
    report_root.mkdir(parents=True, exist_ok=True)

    def contract(status: str, *, failures: list[dict] | None = None,
                 error: str = "") -> dict:
        return _batch_result_contract(
            status, run_id=run_id, failures=failures, error=error,
            parent_run_id=parent_run_id, task_id=task_id,
            manifest_sha256=manifest_sha256, runtime=runtime,
            task_log=str(report_root))

    try:
        record_lock = _acquire_record_lock(tdir, run_id)
    except RuntimeError as exc:
        receipt = contract("ERROR", error=str(exc))
        progress({"record_id": rid, "event": "blocked",
                  "error": str(exc), **receipt})
        log("TASK BLOCKED %s: %s" % (rid, exc))
        return
    done = tdir / "done.json"
    previous_payload = {}
    previous_drafts = []
    previous_deferred_indices = []
    if done.exists():
        try:
            prev = json.loads(done.read_text(encoding="utf-8"))
            previous_payload = prev if isinstance(prev, dict) else {}
            previous_drafts = [str(name) for name in (prev.get("drafts") or [])]
            previous_deferred_indices = list(prev.get("deferred_draft_indices") or [])
            if not parent_run_id:
                parent_run_id = str(prev.get("run_id") or "").strip() or None
            if force_rebuild:
                log("force_rebuild=true; prior drafts are retained but will not be reused")
                previous_drafts = []
                previous_deferred_indices = []
            if prev.get("status") == "OK" and not force_rebuild:
                if not existing_task_has_nonpass_qc(tdir):
                    log("already done, skip")
                    _release_record_lock(record_lock, run_id)
                    return
                log("previous OK has non-PASS visual QC evidence; rechecking")
            if prev.get("status") == "PARTIAL" and not force_rebuild and not (
                retry_partial or bool(task.get("retry_deferred"))
            ):
                log("partial task deferred, skip; explicit recovery required; stop this run")
                progress({"record_id": rid, "event": "deferred_skip",
                          "drafts": previous_drafts,
                          "run_id": run_id,
                          **contract("PARTIAL", failures=prev.get("failures") or []),
                          "deferred_draft_indices": prev.get(
                              "deferred_draft_indices", [])})
                _release_record_lock(record_lock, run_id)
                return
            if prev.get("status") == "ERROR" and not force_rebuild and not (
                retry_error or bool(task.get("retry_error"))
            ):
                log("previous error deferred, skip; explicit recovery required; stop this run")
                progress({"record_id": rid, "event": "error_deferred_skip",
                          "error": prev.get("error", ""),
                          "run_id": run_id,
                          **contract("ERROR", failures=prev.get("failures") or [],
                                     error=prev.get("error", ""))})
                _release_record_lock(record_lock, run_id)
                return
            log("previous attempt was %s (%s), retrying" % (prev.get("status"),
                                                            prev.get("error", "")[:80]))
        except (OSError, ValueError):
            pass
    try:
        if str(task.get("voice_tempo_status") or "").lower() == "invalid":
            raise RuntimeError("VOICE_TEMPO_INVALID: refusing category-default fallback")
        mdir = str(task.get("material_dir") or "").strip()
        if not mdir:
            raise RuntimeError("material_dir empty")
        sources = video_files(mdir)
        if not sources:
            raise RuntimeError("no video files under material_dir: %s" % mdir)
        log("candidate_pool=%d" % len(sources))
        safe_limit = calibrated_source_limit()
        requested_limit = int(task.get("source_limit") or 0)
        effective_limit = requested_limit or safe_limit or 0
        selection_info = {
            "original_count": len(sources),
            "selected_count": None,
            "limit": effective_limit or None,
            "strategy": "full_pool_then_script_driven_semantic_match",
            "selection_stage": "orchestrator_temporary_match",
            "final_unique_source_limit": effective_limit or None,
        }
        log("candidate_pool=%d; script_match_required=true; final_unique_source_limit=%s" % (
            len(sources), effective_limit or "none"))
        count = max(1, int(task.get("count") or 1))
        base_draft = str(task.get("title") or rid).strip() or rid
        manifest = {
            "mode": "standard",
            "delivery_mode": requested_mode(task),
            "task_id": str(task.get("task_id") or rid),
            "record_id": rid,
            "parent_run_id": parent_run_id,
            "full_script": str(task.get("script") or "").strip(),
            "product": str(task.get("product") or "").strip(),
            "category": str(task.get("category") or "").strip(),
            # Feedback guides editing; independent QC must verify the result.
            "revision_feedback": str(task.get("revision_feedback") or "").strip(),
            "voice": str(task.get("voice") or "").strip(),
            "voice_tempo": task.get("voice_tempo"),
            **manifest_meeting_fields(task),
            "bgm": _meeting_bgm_spec(task.get("bgm")),
            "bgm_state_dir": str(WORK / "bgm-selection"),
            "batch_id": str(task.get("batch_id") or task.get("batch_identity")
                             or task.get("worker_batch_identity") or task.get("task_id") or rid),
            # 音色/BGM 已由飞书表格给定（运营已选定），等于运营侧确认，必须显式
            # 声明 —— 否则引擎会进 audio_selection_gate，要求可播放的试听候选，
            # 批量环境下会直接 BGM_PREVIEW_UNAVAILABLE。与 R3 可跑通稿口径一致。
            "audio_selection_confirmed": True,
            "material_sources": sources,
            "source_selection": selection_info,
            "clip_selection_policy": {
                "mode": "script_driven_semantic_match",
                "derive_clip_count_from_script": True,
                "candidate_pool_is_not_final_clip_count": True,
                "max_final_unique_sources": effective_limit or None,
            },
            "temporary_material_analysis": True,
            "max_source_reuse": 2,
            "head_trim_s": 0.3,
            "tail_trim_s": 0,
        }
        task_id = str(manifest["task_id"])
        manifest_sha256 = _payload_sha256(manifest)
        if not manifest["full_script"]:
            raise RuntimeError("script empty, skip (nbz_test-like row)")
        index_file = str(task.get("material_index_file") or os.environ.get("JY_MATERIAL_INDEX_FILE") or "").strip()
        if index_file:
            cached_index = production_eval.read(pathlib.Path(index_file))
            policy_hash = production_eval.file_hash(CLOSED_LOOP_POLICY)
            approved_rows = material_index.select(cached_index.get("materials", []),
                category=str(task.get("category") or ""), sku=str(task.get("sku") or ""),
                tags=task.get("shot_tags") or [], policy_hash=policy_hash,
                tagger_version=cached_index.get("tagger_version", ""))
            if not approved_rows:
                raise RuntimeError("MATERIAL_INDEX_NO_APPROVED_SAME_SKU")
            cached_analysis = tdir / "material_index_analysis.json"
            cached_analysis.write_text(json.dumps(material_index.shot_analysis(approved_rows), ensure_ascii=False, indent=2), encoding="utf-8")
            manifest["shot_analysis_file"] = str(cached_analysis)
            manifest["material_sources"] = list(dict.fromkeys(row["source_path"] for row in approved_rows))
            manifest["material_index_sha256"] = production_eval.file_hash(pathlib.Path(index_file))
            manifest["material_cache_reused"] = True
            sources = manifest["material_sources"]
        runtime_root = pathlib.Path(os.environ.get("JY_EVAL_RUNTIME_ROOT", str(PIPE_ROOT)))
        runtime_identity = production_eval.runtime_identity(runtime_root)
        # A run-specific freeze precedes TTS, vision and draft generation. It
        # does not alter any retained historical contract or approval.
        frozen = production_eval.freeze(task, run_id, [pathlib.Path(p) for p in sources],
                                         runtime_identity, mode="enforce")
        _save_frozen_task(tdir, task, frozen)
        frozen_path = tdir / "eval" / run_id / "contract.json"
        production_eval.write_once(frozen_path, frozen)
        active_contract = tdir / "eval" / "active_contract.json"
        active_contract.write_text(json.dumps(frozen, ensure_ascii=False, indent=2), encoding="utf-8")
        drafts = []
        local_only_drafts = []
        failures = []
        qc_reports = []
        distribution_receipts = []
        qc_gaps = []
        repair_visual_missing_indices = set()
        analysis_cache = None
        # A partial task may already have shipped draft 1 before the queue
        # moved on.  Recover its validated candidate report so draft 2/3 do
        # not re-run the entire visual scan from scratch.
        existing_report = tdir / "report" / base_draft / "shot_candidates.json"
        if existing_report.exists():
            reusable_count, shot_count = reusable_analysis_rows(existing_report)
            if reusable_count:
                analysis_cache = str(existing_report)
                log("reuse existing shot analysis rows=%d/%d for remaining drafts: %s" % (
                    reusable_count, shot_count, analysis_cache))
        shared_analysis = os.environ.get("JY_SHARED_ANALYSIS_FILE", "").strip()
        if shared_analysis:
            shared_path = pathlib.Path(shared_analysis)
            if shared_path.is_file():
                reusable_count, shot_count = reusable_analysis_rows(shared_path)
                if reusable_count and reusable_count > (reusable_analysis_rows(pathlib.Path(analysis_cache))[0] if analysis_cache else 0):
                    analysis_cache = str(shared_path)
                    log("reuse shared shot analysis rows=%d/%d: %s" % (
                        reusable_count, shot_count, analysis_cache))
        # A large candidate pool is scanned in full, but it must not monopolise
        # the only queue worker. Keep a shorter wall-clock slice for oversized
        # pools; the retained material cache/report makes the next pass cheap.
        task_budget_seconds = max(0, int(task_deadline - task_started))
        log("task_wall_clock_budget=%ss candidate_pool=%d" % (
            task_budget_seconds, len(sources)))
        for i in range(1, count + 1):
            name = base_draft if i == 1 else f"{base_draft}-{i}"

            # A partial task is resumable: keep already shipped drafts and only
            # retry the missing slot.  Without this, a timeout on draft 3 would
            # rebuild/re-upload drafts 1 and 2 on the next queue pass.
            if i <= len(previous_drafts):
                prior = previous_drafts[i - 1]
                needs_recheck = prior_draft_requires_recheck(
                    tdir, i, base_draft, previous_payload)
                if (DRAFTS / prior).exists() and not needs_recheck:
                    drafts.append(prior)
                    log("already shipped draft %d/%d, keep: %s" % (i, count, prior))
                    continue
                if needs_recheck:
                    log("retained draft %d/%d has unresolved QC evidence; recheck: %s" % (
                        i, count, prior))
                    retained_report_dir = tdir / "report" / (
                        base_draft if i == 1 else "report_%d" % i)
                    retained_qc = run_post_draft_qc(
                        DRAFTS / prior, retained_report_dir)
                    retained_status = str(retained_qc.get("status") or "UNCERTIFIED")
                    if retained_status in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS", "UNCERTIFIED_TIMEOUT"}:
                        drafts.append(prior)
                        qc_reports.append({"draft": prior, "status": retained_status,
                                           "report": retained_qc.get("output")})
                        if retained_status in {"PASS_WITH_GAPS", "UNCERTIFIED_TIMEOUT"}:
                            qc_gaps.append({
                                "index": i, "draft": prior,
                                "phase": "post_draft_qc",
                                "type": "uncertified_preview" if retained_status == "PASS_WITH_GAPS" else "uncertified_timeout",
                                "message": retained_qc.get("message") or "retained draft is not formally certified",
                                "qc_report": retained_qc.get("output"),
                            })
                        log("retained draft %d/%d QC=%s; no duplicate rebuild: %s" % (
                            i, count, retained_status, prior))
                        continue
                    log("retained draft %d/%d QC=%s; rebuild requested: %s" % (
                        i, count, retained_status, prior))

            rd = tdir / "report" / (name if i == 1 else "report_%d" % i)
            draft_ok = False
            last_failure = None
            rejection_identity, qc_rejected = restore_qc_rejections(rd, manifest)
            if qc_rejected:
                log("restore post-draft QC rejections: %s" % {
                    k: sorted(v) for k, v in sorted(qc_rejected.items())})
            for attempt in range(1, MAX_DRAFT_ATTEMPTS + 1):
                applied_repair_indices = frozenset(repair_visual_missing_indices)
                applied_rejections = frozenset((k, s) for k, v in qc_rejected.items() for s in v)
                # Escalating strategies so one broken build never repeats
                # identically.  The first draft may scan the pool once; all
                # later drafts reuse that validated analysis, and retries
                # keep reusing it.  This skips repeated relay stalls and
                # makes three variants share one material-understanding pass.
                # Hard visual blockers are recorded, never faked as OK.
                #
                # Each attempt writes to a brand-new draft name so a partially
                # created draft from a failed attempt can never collide with the
                # next attempt (剪映 rejects "target draft already exists").
                a_name = name if attempt == 1 else f"{name}-{time.strftime('%H%M%S')}"
                guard = 0
                while (DRAFTS / a_name).exists():
                    guard += 1
                    a_name = f"{name}-{time.strftime('%H%M%S')}-{guard}"
                if a_name != name:
                    log("draft name bump (attempt %d): %s -> %s" % (
                        attempt, name, a_name))
                # Once a candidate analysis has been validated, reuse it for
                # every later draft in this task.  Re-running the same pool
                # for draft 2/3 was the main source of queue-wide delays.
                reuse = analysis_cache
                timeout = BUILD_TIMEOUT_SECONDS
                if attempt >= 2 and (not reuse or attempt >= 3):
                    timeout = BUILD_TIMEOUT_SECONDS * TIMEOUT_ESCALATION_MULT
                # Never exceed the per-task wall-clock budget; a task that
                # cannot finish inside it is recorded as BLOCKED and the queue
                # moves on instead of being stalled by an oversized pool.
                remaining = int(task_deadline - time.time())
                if remaining < 120:
                    last_failure = {
                        "index": i, "draft": a_name, "phase": "task_budget",
                        "type": "blocked",
                        "message": "素材池过大，任务墙钟预算(%ss)已用尽，延后单独处理；已保留既有草稿，未伪造 OK" % task_budget_seconds,
                    }
                    log("TASK BUDGET EXHAUSTED draft %d/%d after %s attempt(s); stop current task" % (i, count, attempt))
                    break
                timeout = min(timeout, remaining)
                try:
                    build_manifest = dict(manifest)
                    # Same-script variants must share one analysis cache but
                    # receive a deterministic rotation index. The allocator
                    # may rotate only within a near-tied, gate-valid pool.
                    build_manifest["variant_index"] = i - 1
                    build_manifest["variant_count"] = count
                    build_manifest["variant_pool_size"] = min(5, max(3, count))
                    build_manifest["variant_score_tolerance"] = 8
                    if qc_rejected:
                        # Re-select a different pixel-reviewed shot for each
                        # QC-rejected sentence; never blank or re-use it.
                        build_manifest["qc_rejected_shots"] = {
                            str(k): sorted(v) for k, v in sorted(qc_rejected.items())}
                        log("post-draft visual repair: reselect sentences excluding rejected shots=%s" %
                            build_manifest["qc_rejected_shots"])
                    attempt_state = rd / "build_attempt_state.json"
                    inputs = build_progress_inputs(build_manifest, analysis_cache,
                                                   repair_visual_missing_indices)
                    previous_inputs = _read_json(attempt_state).get("input")
                    from tools.workflow.selection_recovery import pending as selection_recovery_pending
                    if previous_inputs == inputs and not (force_rebuild and attempt == 1) and not selection_recovery_pending(rd):
                        if last_failure:
                            last_failure["no_progress"] = True
                        else:
                            last_failure = {"index": i, "draft": name, "phase": "build", "type": "blocked",
                                "message": "BUILD_NO_PROGRESS: no new candidates, windows or observations"}
                        log("BUILD_NO_PROGRESS: no new candidates, windows or observations")
                        break
                    from orchestrator.file_safety import atomic_write_text
                    atomic_write_text(attempt_state, json.dumps({"input": inputs,
                        "attempt": attempt, "status": "running", "updated_at": time.time()},
                        ensure_ascii=False, indent=2))
                    build_options = {"timeout": timeout, "reuse_analysis": reuse, "attempt": attempt}
                    if force_rebuild and attempt == 1:
                        build_options["force_rebuild"] = True
                    res = run_build(build_manifest, rd, a_name, **build_options)
                    a_name = res.get("draft_name") or a_name
                    candidate_report = rd / "shot_candidates.json"
                    if analysis_cache is None and candidate_report.exists():
                        reusable_count, shot_count = reusable_analysis_rows(candidate_report)
                        if reusable_count:
                            analysis_cache = str(candidate_report)
                            log("cache partial shot analysis rows=%d/%d before match gate: %s" % (
                                reusable_count, shot_count, analysis_cache))
                    failure_text = ""
                    qc_result = None
                    if res["rc"] != 0:
                        fb = tdir / ("build_fail_%d_a%d.log" % (i, attempt))
                        fb.write_text(
                            "RC=%s\n----STDOUT(20k)----\n%s\n----STDERR(20k)----\n%s\n"
                            % (res["rc"], res["stdout"], res["stderr"]),
                            encoding="utf-8")
                        failure_text = (res.get("combined") or res["stderr"])
                        failure_text += "\n[build rc=%s log=%s]" % (
                            res["rc"], fb.name)
                    else:
                        visual_ok, visual_message = validate_visual_match(rd)
                        if not visual_ok:
                            failure_text = visual_message
                        else:
                            qc_result = run_post_draft_qc(DRAFTS / a_name, rd)
                            if (qc_result.get("status") not in {"PASS", "PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}
                                    and qc_result.get("status") != "UNCERTIFIED_TIMEOUT"
                                    and not local_preview(task, qc_result)):
                                failure_text = qc_result.get("message") or (
                                    "DRAFT_VISUAL_QC_BLOCKED: status=%s" % qc_result.get("status"))
                            elif qc_result.get("status") == "UNCERTIFIED_TIMEOUT":
                                log("DRAFT %d/%d retained as UNCERTIFIED_TIMEOUT; defer post-draft visual QC: %s" % (
                                    i, count, a_name))
                    if failure_text:
                        cls = classify_build_failure(failure_text)
                        last_failure = {
                            "index": i, "draft": name, "phase": "build_or_validate",
                            "type": "vision_hard_block" if cls["vision_hard"]
                            else "transient",
                            "attempt": attempt, "markers": cls["markers"],
                            "message": failure_text[-800:],
                            "stage_retry_exhausted": res.get("stage_retry_exhausted", False),
                        }
                        stage_result = _read_json(rd / "last_result.json")
                        if stage_result.get("resume_stage") == "semantic_match":
                            last_failure.update({k: stage_result[k] for k in (
                                "retryable", "resume_stage", "failed_stage", "failure_class", "failure_code") if k in stage_result})
                        if qc_result is not None:
                            last_failure["qc_report"] = qc_result.get("output")
                            last_failure["qc_status"] = qc_result.get("status")
                            for row in (qc_result.get("report") or {}).get("model_review") or []:
                                if isinstance(row, dict) and row.get("visual_ok") is False:
                                    try:
                                        repair_visual_missing_indices.add(int(row.get("index")))
                                    except (TypeError, ValueError):
                                        pass
                            for issue in (qc_result.get("report") or {}).get("issues") or []:
                                match = re.search(r"POST_DRAFT_VISION_MISSING:S(\d+)", str(issue))
                                if match:
                                    repair_visual_missing_indices.add(int(match.group(1)))
                            for key, ids in qc_rejected_shots_from_report(
                                    rd, qc_result.get("report") or {}).items():
                                qc_rejected.setdefault(key, set()).update(ids)
                            save_qc_rejections(rd, rejection_identity, qc_rejected)
                        log("DRAFT %d/%d attempt %d failed (%s): %s" % (
                            i, count, attempt,
                            "hard" if cls["vision_hard"] else "transient",
                            failure_text[:160].replace("\n", " ")))
                        if res.get("stage_retry_exhausted"):
                            log("selection stage recovery stopped; no full-build fallback")
                            break
                        # A post-draft visual mismatch is actionable: the QC
                        # report has already identified the bad sentence(s),
                        # and the next build will turn those exact segments
                        # into intentional gaps. Allow one final repair pass;
                        # do not keep retrying deterministic match failures.
                        qc_repairable = bool(qc_result and (
                            repair_visual_missing_indices.difference(applied_repair_indices)
                            or {(k, s) for k, v in qc_rejected.items() for s in v}.difference(applied_rejections)))
                        if qc_result and not qc_repairable:
                            last_failure.update(phase="post_draft_qc", resume_stage="qc",
                                                failed_stage="qc", draft=a_name)
                            log("QC has no new repair window; retain draft for QC recovery")
                            break
                        # `vision_analyzer` has already performed its single
                        # bounded provider retry.  Re-running the complete
                        # build here would turn one service retry into a
                        # hidden second full vision pass.
                        vision_stage_failed = any(marker in failure_text.upper()
                                                  for marker in ("VISION_CALL_FAILED", "VISION_JSON_INVALID"))
                        if vision_stage_failed and not qc_repairable:
                            log("vision stage exhausted its bounded retry; stop current task")
                            break
                        # Deterministic semantic/timing/environment/UI failures
                        # are not provider retries.  Re-running the full draft
                        # only repeats the same blocker and used to consume the
                        # whole task budget.
                        if cls.get("deterministic_block") and not qc_repairable:
                            break
                        # A temporary vision/provider outage gets at most one
                        # bounded retry.  Later attempts must be a directed
                        # stage recovery, not another full build.
                        if cls["transient"] and attempt >= 2 and not qc_repairable:
                            break
                        if cls["vision_hard"] and attempt >= 2 and not qc_repairable:
                            break
                        if attempt >= MAX_DRAFT_ATTEMPTS:
                            break
                        continue
                    # A timeout is an explicit lack of visual certification,
                    # not a shippable preview. Keep the local draft for a
                    # later QC pass, record the deferment, and advance the
                    # queue without calling ship_distribute.
                    if qc_result and qc_result.get("status") == "UNCERTIFIED_TIMEOUT":
                        qc_reports.append({"draft": a_name,
                                           "status": "UNCERTIFIED_TIMEOUT",
                                           "report": qc_result.get("output")})
                        last_failure = {
                            "index": i, "draft": a_name,
                            "phase": "post_draft_qc",
                            "type": "uncertified_timeout",
                            "attempt": attempt,
                            "message": qc_result.get("message") or
                                       "独立视觉验收超时；保留本地草稿，未写入 NAS",
                            "qc_report": qc_result.get("output"),
                        }
                        log("DRAFT %d/%d not shipped: UNCERTIFIED_TIMEOUT; local-only deferment: %s" % (
                            i, count, a_name))
                        break
                    package_gate = validate_draft_package(DRAFTS / a_name)
                    if not package_gate.get("ok"):
                        failure_text = "PRE_SHIP_PACKAGE_BLOCKED: " + "; ".join(
                            str(item) for item in (package_gate.get("issues") or []))
                        last_failure = {
                            "index": i, "draft": a_name,
                            "phase": "pre_ship_package_gate",
                            "type": "draft_structure_block",
                            "attempt": attempt,
                            "message": failure_text,
                            "package_gate": package_gate,
                        }
                        log("DRAFT %d/%d not shipped: %s" % (i, count, failure_text))
                        if attempt >= MAX_DRAFT_ATTEMPTS:
                            break
                        continue
                    log("PRE_SHIP_PACKAGE_OK draft=%s video_tracks=%s video_segments=%s resolved_media=%s" % (
                        a_name, package_gate.get("video_tracks"),
                        package_gate.get("video_segments"),
                        package_gate.get("resolved_video_media")))
                    # Degraded/gapped visual QC is a usable preview, never a
                    # formal delivery. Retain the verified local draft and
                    # release the queue; do not send it to NAS or table write.
                    if qc_result and (qc_result.get("status") in {"PASS_WITH_DEGRADED", "PASS_WITH_GAPS"}
                                      or local_preview(task, qc_result)):
                        preview_status = qc_result.get("status")
                        drafts.append(a_name)
                        local_only_drafts.append(a_name)
                        qc_reports.append({"draft": a_name,
                                           "status": preview_status,
                                           "report": qc_result.get("output")})
                        qc_gaps.append({
                            "index": i, "draft": a_name,
                            "phase": "post_draft_qc",
                            "type": "degraded_preview" if preview_status == "PASS_WITH_DEGRADED" else "uncertified_preview",
                            "message": "视觉验收为%s；保留本地预览，不进入 NAS/表格正式回写" % preview_status,
                            "qc_report": qc_result.get("output"),
                        })
                        draft_ok = True
                        log("DRAFT %d/%d retained as %s preview; NAS shipping blocked: %s" % (
                            i, count, preview_status, a_name))
                        break
                    # All drafts remain local until the shared task-level gate has
                    # revalidated QC, package validity and output identity.
                    drafts.append(a_name)
                    local_only_drafts.append(a_name)
                    if qc_result is not None:
                        qc_reports.append({"draft": a_name,
                                           "status": qc_result.get("status"),
                                           "report": qc_result.get("output")})
                    draft_ok = True
                    log("DRAFT %d/%d retained locally for acceptance: %s" % (i, count, a_name))
                    break
                except subprocess.TimeoutExpired as exc:
                    if analysis_cache is None:
                        candidate_report = rd / "shot_candidates.json"
                        reusable_count, shot_count = reusable_analysis_rows(candidate_report)
                        if reusable_count:
                            analysis_cache = str(candidate_report)
                            log("cache partial shot analysis rows=%d/%d after timeout: %s" % (
                                reusable_count, shot_count, analysis_cache))
                    last_failure = {
                        "index": i, "draft": name, "phase": "build",
                        "type": "transient", "attempt": attempt,
                        "timeout_s": exc.timeout,
                        "message": "单稿构建超时（attempt %s, timeout %ss）；下次改用更长超时或复用素材分析" % (attempt, exc.timeout),
                    }
                    (tdir / ("build_timeout_%d_a%d.log" % (i, attempt))).write_text(
                        json.dumps(last_failure, ensure_ascii=False, indent=2),
                        encoding="utf-8")
                    log("DRAFT TIMEOUT %d/%d attempt %d: %s (timeout %ss)" % (
                        i, count, attempt, name, exc.timeout))
                    if attempt >= MAX_DRAFT_ATTEMPTS:
                        break
                    continue
                except Exception as exc:  # noqa: BLE001
                    if analysis_cache is None:
                        candidate_report = rd / "shot_candidates.json"
                        reusable_count, shot_count = reusable_analysis_rows(candidate_report)
                        if reusable_count:
                            analysis_cache = str(candidate_report)
                            log("cache partial shot analysis rows=%d/%d after error: %s" % (
                                reusable_count, shot_count, analysis_cache))
                    last_failure = {
                        "index": i, "draft": name, "phase": "build_or_ship",
                        "type": "error", "attempt": attempt,
                        "message": str(exc)[:800],
                    }
                    log("DRAFT ERROR %d/%d attempt %d: %s" % (
                        i, count, attempt, exc))
                    if attempt >= MAX_DRAFT_ATTEMPTS:
                        break
                    continue
            if not draft_ok and last_failure is not None:
                failures.append(last_failure)
                log("draft %d/%d unresolved after %d attempt(s); stop current task" % (i, count, MAX_DRAFT_ATTEMPTS))
                break

        closed_loop_result = None
        if not failures and not qc_gaps and not SKIP_SHIP:
            closed = finalize_closed_loop(tdir, task, drafts, qc_reports,
                                          delivery_receipts=distribution_receipts)
            closed_loop_result = closed
            if not closed.get("ok"):
                failures.append({
                    "phase": "closed_loop",
                    "type": "delivery_or_table_gate",
                    "detail": closed,
                    "message": str(closed),
                })
                log("CLOSED_LOOP_BLOCKED: %s" % closed)

        if failures or qc_gaps or SKIP_SHIP:
            status = ("PREVIEW_READY" if drafts and not failures else
                      "PARTIAL" if drafts else "ERROR")
            all_issues = [*failures, *qc_gaps]
            if SKIP_SHIP and drafts:
                all_issues.append({
                    "phase": "distribution",
                    "type": "local_only",
                    "message": "草稿已生成并保留在 Windows；本次配置不分发",
                })
            persisted_done = _read_json(done)
            distribution_receipts = (persisted_done.get("distribution_receipts")
                                     or distribution_receipts)
            verified_by_name = {row.get("draft_name"): row for row in distribution_receipts}
            shipped_drafts = [name for name in drafts if name in verified_by_name]
            if SKIP_SHIP:
                distribution_status = "LOCAL_ONLY"
            elif len(shipped_drafts) == len(drafts) and drafts:
                distribution_status = "SHIPPED_VERIFIED"
            elif frozen.get("mode") == "enforce" and shipped_drafts:
                distribution_status = "SHIPPED_PARTIAL"
            elif frozen.get("mode") == "enforce":
                distribution_status = "PENDING_ACCEPTANCE"
            elif local_only_drafts:
                distribution_status = "PENDING_ACCEPTANCE"
            elif shipped_drafts:
                distribution_status = "SHIPPED_VERIFIED"
            else:
                distribution_status = "NOT_SHIPPED"
            payload = {
                "record_id": rid, "status": status, "drafts": drafts,
                "nas_links": [verified_by_name[name]["nas_target"] for name in shipped_drafts],
                "local_only_drafts": local_only_drafts,
                "distribution_status": distribution_status,
                "distribution_receipts": distribution_receipts,
                "qc_reports": qc_reports,
                "failures": all_issues,
                "deferred_draft_indices": (list(range(
                    failures[0]["index"], count + 1)) if failures and "index" in failures[0] else []),
                "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            from orchestrator.task_inputs import done_meeting_fields
            payload.update(done_meeting_fields(task))
            payload.update(contract(status, failures=all_issues))
            if drafts:
                payload["eval_receipt"] = evaluate_task(tdir, task, drafts, qc_reports, frozen)
            done.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            if status in {"ERROR", "PARTIAL"}:
                notification = publish_failure_notification(tdir)
                payload["failure_notification"] = notification
                done.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
            progress({"record_id": rid, "event": status.lower(),
                      "drafts": drafts, "failures": all_issues,
                      **contract(status, failures=all_issues)})
            log("TASK %s: completed=%s deferred=%s; terminal=%s safe_to_continue=%s" % (
                status, ", ".join(drafts) or "none",
                ", ".join(str(f.get("index", "gate")) for f in failures),
                payload.get("terminal_state"), payload.get("safe_to_continue")))
            _release_record_lock(record_lock, run_id)
            return

        # finalize_closed_loop writes the verified grouped path and table receipt
        # into done.json. Preserve those fields instead of overwriting them with
        # the older flat-list success payload.
        existing_done = {}
        try:
            existing_done = json.loads(done.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            existing_done = {}
        done_payload = {
            "record_id": rid, "status": "OK",
            "drafts": drafts,
            "qc_reports": qc_reports,
            "nas_links": existing_done.get("nas_links", []),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        from orchestrator.task_inputs import done_meeting_fields
        done_payload.update(done_meeting_fields(task))
        done_payload.update(contract("OK"))
        for key in ("nas_parent_folder", "distribution_status", "distribution_receipt",
                    "table_update_receipt", "closed_loop_receipts", "eval_receipt",
                    "distribution_receipts"):
            if key in existing_done:
                done_payload[key] = existing_done[key]
        done.write_text(json.dumps(done_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        progress({"record_id": rid, "event": "ok", "drafts": drafts, **contract("OK")})
        log("TASK OK: %s" % ", ".join(drafts))
        _release_record_lock(record_lock, run_id)
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc(limit=3)
        # A preflight error can happen before the per-draft loop. Never erase
        # drafts already shipped by an earlier PARTIAL attempt.
        preserved_status = "PARTIAL" if previous_drafts else "ERROR"
        failure_rows = [{"phase": "preflight", "type": "error", "message": str(exc)}]
        error_payload = {
            "record_id": rid, "status": preserved_status,
            "drafts": previous_drafts,
            "nas_links": _read_json(done).get("nas_links", []),
            "error": str(exc), "trace": tb,
            "failures": failure_rows,
            "deferred_draft_indices": (previous_deferred_indices
                                        if previous_drafts else []),
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        from orchestrator.task_inputs import done_meeting_fields
        error_payload.update(done_meeting_fields(task))
        error_payload.update(contract(preserved_status, failures=failure_rows, error=str(exc)))
        done.write_text(json.dumps(error_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        notification = publish_failure_notification(tdir)
        done_payload = _read_json(done)
        if done_payload:
            done_payload["failure_notification"] = notification
            done.write_text(json.dumps(done_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        progress({"record_id": rid, "event": preserved_status.lower(),
                  "error": str(exc), "drafts": previous_drafts,
                  **contract(preserved_status, failures=failure_rows, error=str(exc))})
        log("TASK %s %s: %s" % (preserved_status, rid, exc))
        _release_record_lock(record_lock, run_id)


def _parallel_eligible(task: dict) -> bool:
    """Allow safe retry rows into the bounded two-task trial.

    A retry is safe to overlap only when `_parallel_pair_safe` proves that the
    title and material pool are distinct. Each task still owns its own report,
    cache keys and draft name; the worker remains capped at two processes.
    """
    rid = str(task.get("record_id") or "").strip()
    if not rid:
        return False
    done = WORK / rid / "done.json"
    if not done.exists():
        return True
    try:
        status = json.loads(done.read_text(encoding="utf-8")).get("status")
    except (OSError, ValueError, TypeError):
        status = None
    return bool(task.get("force_rebuild") or task.get("retry_error")
                or task.get("retry_deferred") or status in {"ERROR", "PARTIAL"})


_parallel_pair_safe = queue_contracts.parallel_pair_safe


def _run_task(task: dict) -> None:
    process_task(task,
                 retry_partial=bool(task.get("retry_deferred")),
                 retry_error=bool(task.get("retry_error")))


def select_queue_for_single_run(queue: list[dict], *,
                                record_id: str = "",
                                force_rebuild: bool = False) -> list[dict]:
    return queue_contracts.select_queue_for_single_run(
        queue, record_id=record_id, force_rebuild=force_rebuild,
        resume_partial=os.environ.get("JY_TASK_RESUME_PARTIAL", "").strip() == "1")


def _queue_preflight(task: dict) -> dict:
    """Check static queue inputs before starting the selected task."""
    issues = []
    try:
        requested_mode(task)
    except ValueError as exc:
        issues.append(str(exc))
    configuration_error = _runtime_facts().get("vision_configuration_error")
    if configuration_error:
        issues.append(configuration_error)
    rid = str(task.get("record_id") or "").strip()
    if not rid:
        issues.append("record_id missing")
    version_path = SKILL / "VERSION"
    if _pipeline_version() == "unknown" or not version_path.is_file():
        issues.append("pipeline VERSION missing or unreadable")
    try:
        require_ship_dependencies(SKILL.parent)
    except RuntimeError as exc:
        issues.append(str(exc))
    material_dir = str(task.get("material_dir") or "").strip()
    sources = []
    if not material_dir:
        issues.append("material_dir empty")
    elif not pathlib.Path(material_dir).is_dir():
        issues.append("material_dir unavailable: %s" % material_dir)
    else:
        sources = video_files(material_dir)
        if not sources:
            issues.append("no video files under material_dir: %s" % material_dir)
    if not DRAFTS.parent.is_dir():
        issues.append("draft root parent unavailable: %s" % DRAFTS.parent)
    if not str(task.get("script") or "").strip():
        issues.append("script empty")
    return {"ok": not issues, "issues": issues, "source_count": len(sources)}


def _record_queue_preflight_block(task: dict, prior: dict, preflight: dict) -> dict:
    """Persist a blocking receipt without entering generation or vision."""
    rid = str(task.get("record_id") or "").strip()
    task_dir = WORK / rid
    task_dir.mkdir(parents=True, exist_ok=True)
    report_root = task_dir / "report"
    report_root.mkdir(parents=True, exist_ok=True)
    run_id = "%s-preflight-%s" % (rid, uuid.uuid4().hex[:8])
    parent_run_id = str(prior.get("run_id") or task.get("parent_run_id") or "").strip() or None
    failures = [{"phase": "queue_preflight", "type": "environment",
                 "message": str(item)} for item in (preflight.get("issues") or [])]
    error = "ENVIRONMENT_PRECHECK_FAILED: " + "; ".join(
        str(item) for item in (preflight.get("issues") or []))
    contract = _batch_result_contract(
        "ERROR", run_id=run_id, failures=failures, error=error,
        parent_run_id=parent_run_id, task_id=str(task.get("task_id") or rid),
        manifest_sha256=_payload_sha256(task), runtime=_runtime_facts(),
        task_log=str(report_root))
    payload = {
        "record_id": rid, "status": "ERROR", "drafts": [],
        "error": error, "failures": failures,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        **contract,
    }
    (task_dir / "done.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    progress({"record_id": rid, "event": "queue_preflight_blocked",
              "error": error, "source_count": preflight.get("source_count", 0),
              **contract})
    log("QUEUE PREFLIGHT BLOCKED record_id=%s: %s" % (rid, error))
    return payload


def _task_receipt_complete(task_log: str) -> bool:
    """Accept a direct orchestrator report dir or its batch report root."""
    root = pathlib.Path(task_log)
    if not root.is_dir():
        return False
    candidates = [root]
    try:
        candidates.extend(path for path in root.rglob("result.json")
                         if path.is_file())
    except OSError:
        return False
    for candidate in candidates:
        result_path = candidate if candidate.name == "result.json" else candidate / "result.json"
        receipt_dir = result_path.parent
        events_path = receipt_dir / "events.jsonl"
        if not result_path.is_file() or not events_path.is_file():
            continue
        try:
            receipt = _read_json(result_path)
            finished = any(
                str(json.loads(line).get("event") or "") == "task_finished"
                for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip())
        except (OSError, ValueError, TypeError):
            continue
        if receipt and finished:
            return True
    return False


def _continuation_allowed(task: dict) -> tuple[bool, dict]:
    """Stop implicit recovery after a non-success terminal result.

    A retry/skip flag is an explicit operator decision.  Without it, the worker
    publishes a hold event and does not touch the failed task or the next queue
    item.
    """
    rid = str(task.get("record_id") or "").strip()
    done_path = WORK / rid / "done.json"
    if not done_path.is_file():
        return True, {}
    try:
        payload = json.loads(done_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False, {"reason": "DONE_JSON_INVALID"}
    if bool(payload.get("safe_to_continue")) and payload.get("terminal_state") == "SUCCESS":
        task_log = str(payload.get("task_log") or "").strip()
        if task_log:
            if not _task_receipt_complete(task_log):
                return False, {**payload, "reason": "TASK_RECEIPT_INCOMPLETE"}
        elif payload.get("pipeline_version") == _pipeline_version():
            return False, {**payload, "reason": "TASK_RECEIPT_MISSING"}
        elif not payload.get("finished_at"):
            return False, {**payload, "reason": "TASK_FINISH_EVENT_MISSING"}
        if payload.get("pipeline_version") == _pipeline_version():
            missing = [key for key in ("task_id", "manifest_sha256", "runtime")
                       if not payload.get(key)]
            if missing:
                return False, {**payload, "reason": "RUN_RECEIPT_FIELDS_MISSING",
                               "missing_fields": missing}
        return True, payload
    explicit = bool(
        task.get("force_rebuild") or task.get("retry_error") or
        task.get("retry_deferred") or
        os.environ.get("JY_TASK_SKIP_CURRENT", "").strip() == "1"
    )
    return explicit, payload


def _mark_deferred(task: dict, prior: dict) -> dict:
    """Record an explicit operator skip before selecting the next task."""
    rid = str(task.get("record_id") or "").strip()
    task_dir = WORK / rid
    task_dir.mkdir(parents=True, exist_ok=True)
    report_root = task_dir / "report"
    report_root.mkdir(parents=True, exist_ok=True)
    old_run_id = str(prior.get("run_id") or "").strip() or None
    run_id = "%s-deferred-%s" % (rid, uuid.uuid4().hex[:8])
    contract = _batch_result_contract(
        "DEFERRED", run_id=run_id, parent_run_id=old_run_id,
        task_id=str(task.get("task_id") or rid),
        manifest_sha256=_payload_sha256(task), runtime=_runtime_facts(),
        task_log=str(report_root))
    payload = dict(prior or {})
    payload.update({
        "record_id": rid,
        "status": "DEFERRED",
        "run_id": run_id,
        "parent_run_id": old_run_id,
        "pipeline_version": _pipeline_version(),
        "deferred_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "deferred_reason": "operator_skip_current",
        **contract,
    })
    (task_dir / "done.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    progress({"record_id": rid, "event": "deferred",
              "reason": "operator_skip_current", **contract})
    return payload


def main():
    queue_path = WORK / "batch_queue.json"
    if not queue_path.exists():
        log("queue missing: %s" % queue_path)
        sys.exit(2)
    all_tasks = json.loads(queue_path.read_text(encoding="utf-8"))
    target_id = os.environ.get("JY_TASK_RECORD_ID", "").strip()
    force_rebuild = os.environ.get("JY_TASK_FORCE_REBUILD", "").strip() == "1"
    skip_requested = os.environ.get("JY_TASK_SKIP_CURRENT", "").strip() == "1"
    if skip_requested and target_id:
        log("QUEUE_SELECTION_ERROR JY_TASK_SKIP_CURRENT cannot be combined with JY_TASK_RECORD_ID")
        sys.exit(2)
    try:
        queue = select_queue_for_single_run(
            all_tasks, record_id=target_id, force_rebuild=force_rebuild)
    except ValueError as exc:
        log("QUEUE_SELECTION_ERROR %s" % exc)
        sys.exit(2)
    if not queue:
        log("QUEUE empty; no task selected")
        return
    # This legacy command handles one queue item. Scheduled full runs use
    # tools.workflow.scheduled and the shared dispatcher directly.
    log("QUEUE single-step total=%d selected=%s" % (
        len(all_tasks), queue[0].get("record_id")))
    allowed, prior = _continuation_allowed(queue[0])
    if skip_requested and not target_id:
        _mark_deferred(queue[0], prior)
        remaining = [dict(item) for item in all_tasks
                     if str(item.get("record_id") or "").strip() !=
                     str(queue[0].get("record_id") or "").strip()]
        queue = select_queue_for_single_run(remaining)
        if not queue:
            log("QUEUE skip recorded; no next task remains")
            return
        allowed, prior = _continuation_allowed(queue[0])
    if not allowed:
        rid = str(queue[0].get("record_id") or "")
        hold = _batch_result_contract(
            str(prior.get("status") or "ERROR"),
            run_id=str(prior.get("run_id") or (rid + "-hold")),
            failures=prior.get("failures") or [],
            error=str(prior.get("error") or prior.get("reason") or ""),
            parent_run_id=prior.get("parent_run_id"),
        )
        # A persisted done.json is the authoritative terminal receipt.  Keep
        # its classification in the hold event even when the compact failure
        # rows were omitted by an older worker.
        for key in ("terminal_state", "failure_class", "failure_code", "retryable"):
            if key in prior and prior.get(key) is not None:
                hold[key] = prior[key]
        progress({"record_id": rid, "event": "queue_hold",
                  "reason": "previous task is not safe_to_continue",
                  **hold, "safe_to_continue": False,
                  "next_action": "manual_review"})
        log("QUEUE HOLD record_id=%s terminal_state=%s failure_class=%s; explicit retry/skip required" % (
            rid, hold.get("terminal_state"), hold.get("failure_class")))
        return
    preflight = _queue_preflight(queue[0])
    if not preflight.get("ok"):
        _record_queue_preflight_block(queue[0], prior, preflight)
        return
    if not (queue[0].get("batch_id") or queue[0].get("batch_identity")):
        # All tasks read from the same queue snapshot share one durable BGM
        # de-duplication cycle, including a selected task retried later.
        queue_identity = sorted(({
            "record_id": str(item.get("record_id") or ""),
            "task_id": str(item.get("task_id") or ""),
            "proc_key": str(item.get("proc_key") or ""),
            "script_sha256": hashlib.sha256(
                str(item.get("script") or "").encode("utf-8")).hexdigest(),
        } for item in all_tasks), key=lambda item: (
            item["record_id"], item["task_id"], item["proc_key"],
            item["script_sha256"]))
        queue[0]["worker_batch_identity"] = _payload_sha256(queue_identity)
    from tools.batch_dispatch import dispatch
    dispatch(queue, work=WORK)
    log("BATCH FINISHED")


if __name__ == "__main__":
    main()
