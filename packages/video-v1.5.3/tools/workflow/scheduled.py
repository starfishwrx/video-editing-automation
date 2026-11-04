"""Bounded single-run Windows pipeline entrypoint for a local task queue.

Scheduling selects work; batch_dispatch owns analysis, build, QC and delivery.
This module never generates drafts or upgrades delivery evidence itself.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import uuid

MAX_BUILD_ATTEMPTS = 2
MAX_QC_ATTEMPTS = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("JSON_OBJECT_REQUIRED:" + path.name)
    return value


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def _pipeline_lock(work: Path):
    """OS-owned byte lock survives abandoned lock files, never stale PID guesses."""
    work.mkdir(parents=True, exist_ok=True)
    with (work / ".pipeline.lock").open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TimeoutError("PIPELINE_ALREADY_RUNNING") from exc
        try:
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _safe_name(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip().rstrip("."):
        return False
    if value in {".", ".."} or re.search(r'[\\/:*?"<>|\x00-\x1f]', value):
        return False
    return value.split(".", 1)[0].upper() not in {
        "CON", "PRN", "AUX", "NUL", *("COM%d" % n for n in range(1, 10)),
        *("LPT%d" % n for n in range(1, 10)),
    }


def _input_key(task: dict) -> str:
    return hashlib.sha256(json.dumps(task, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _active_task(task_dir: Path) -> bool:
    path = task_dir / ".task_run.lock"
    if not path.exists():
        return False
    # Reuse the worker's signal-free Windows process-identity check. Unknown
    # ownership stays occupied; only a confirmed exited/reused PID is stale.
    from tools.process_liveness import assert_process_alive
    try:
        owner = _read(path)
        pid = int(owner["pid"])
        if pid <= 0:
            return True
        assert_process_alive(pid, owner.get("process_created_at"))
    except ProcessLookupError:
        return False
    except (OSError, ValueError, TypeError, KeyError):
        return True
    return True


def _plan(task: dict, work: Path, prior: dict) -> tuple[str | None, str]:
    rid = task["record_id"]
    directory = work / rid
    if directory.resolve().parent != work:
        return None, "TASK_PATH_OUTSIDE_WORK_ROOT"
    if _active_task(directory):
        return None, "TASK_ALREADY_RUNNING"
    mode = str(task.get("delivery_mode") or "preview").strip().lower()
    if mode not in {"formal", "preview"}:
        return None, "INVALID_DELIVERY_MODE"
    if task.get("manifest") is not None and not isinstance(task["manifest"], dict):
        return None, "INVALID_TASK_MANIFEST"
    if int(task.get("count") or 1) < 1:
        return None, "INVALID_TASK_COUNT"
    done_path = directory / "done.json"
    done = _read(done_path) if done_path.exists() else {}
    if done.get("status") == "OK":
        return None, "ALREADY_COMPLETED"
    if prior and prior.get("input_key") != _input_key(task):
        # A scheduled tick is not permission to overwrite a frozen run. Queue
        # edits require a new record or explicit, targeted recovery/reset.
        return None, "TASK_INPUT_CHANGED_REQUIRES_RESET"
    if done.get("drafts"):
        drafts = done["drafts"]
        if not isinstance(drafts, list) or not all(_safe_name(name) for name in drafts):
            return None, "INVALID_RETAINED_DRAFT_NAMES"
        if len(drafts) < max(1, int(task.get("count") or 1)):
            return None, "PARTIAL_BUILD_REQUIRES_RECOVERY"
        refs = done.get("qc_reports") or []
        if not isinstance(refs, list):
            return None, "INVALID_QC_REFERENCES"
        statuses = [str(row.get("status") or "").upper() for row in refs if isinstance(row, dict)]
        if "FAIL" in statuses:
            return None, "QC_FAILED_REQUIRES_RECOVERY"
        if (mode == "preview" and done.get("resume_stage") != "qc"
                and "UNCERTIFIED_TIMEOUT" not in statuses):
            return None, "PREVIEW_LOCAL_ONLY"
        if int(prior.get("qc_attempts", 0)) >= MAX_QC_ATTEMPTS:
            return None, "QC_RETRY_LIMIT"
        return "qc", "REUSE_LOCAL_DRAFTS"
    attempts = int(prior.get("build_attempts", 0))
    if done or attempts:
        retryable = done.get("retryable") is True or prior.get("retryable") is True
        if not retryable:
            return None, "FAILED_OR_ATTEMPTED_REQUIRES_RECOVERY"
    if attempts >= MAX_BUILD_ATTEMPTS:
        return None, "BUILD_RETRY_LIMIT"
    return "build", "NEW_TASK" if not attempts else "BOUNDED_TRANSIENT_RETRY"


def _default_dispatcher(tasks: list[dict], **kwargs) -> list[dict]:
    from tools.batch_dispatch import dispatch
    return dispatch(tasks, **kwargs)


def _execute(queue_path: Path, work: Path, summary: dict, dispatcher, record_id: str | None = None) -> None:
    raw = json.loads(queue_path.read_text(encoding="utf-8-sig"))
    tasks = raw.get("tasks") if isinstance(raw, dict) else raw
    if not isinstance(tasks, list):
        raise ValueError("QUEUE_TASK_LIST_REQUIRED")
    if record_id is not None:
        if not _safe_name(record_id):
            raise ValueError("INVALID_RECORD_ID")
        tasks = [task for task in tasks if isinstance(task, dict) and
                 str(task.get("record_id") or "").casefold() == record_id.casefold()]
        if len(tasks) != 1:
            raise ValueError("PIPELINE_RECORD_NOT_UNIQUE_OR_MISSING")
    state_path = work / "pipeline_state.json"
    state = _read(state_path) if state_path.exists() else {"schema_version": 1, "records": {}}
    if state.get("schema_version") != 1 or not isinstance(state.get("records"), dict):
        raise ValueError("PIPELINE_STATE_INVALID")
    records = {}
    for rid, prior in state["records"].items():
        key = rid.casefold()
        if key in records:
            raise ValueError("PIPELINE_STATE_RECORD_CASE_COLLISION")
        records[key] = prior
    state["records"] = records
    groups = {"qc": [], "build": []}
    seen = set()
    for task in tasks:
        rid = task.get("record_id") if isinstance(task, dict) else None
        row = {"record_id": rid if isinstance(rid, str) else None, "status": "SKIPPED"}
        summary["tasks"].append(row)
        if not _safe_name(rid):
            row["reason"] = "INVALID_RECORD_ID"
            continue
        if rid.casefold() in seen:
            row["reason"] = "DUPLICATE_RECORD_ID"
            continue
        seen.add(rid.casefold())
        try:
            prior = records.get(rid.casefold()) or {}
            if not isinstance(prior, dict):
                raise ValueError("TASK_STATE_INVALID")
            stage, reason = _plan(task, work, prior)
            row.update(stage=stage, reason=reason)
            if stage:
                row["status"] = "PLANNED"
                groups[stage].append((dict(task), row))
        except Exception as exc:
            row.update(status="ERROR", reason="TASK_INSPECTION_FAILED", error=str(exc))
    if summary["dry_run"]:
        return
    dispatcher = dispatcher or _default_dispatcher
    for stage, selected in groups.items():
        if not selected:
            continue
        # Count before dispatch: a killed supervisor cannot retry forever.
        for task, row in selected:
            rid = task["record_id"]
            prior = records.setdefault(rid.casefold(), {"input_key": _input_key(task)})
            key = stage + "_attempts"
            prior[key] = int(prior.get(key, 0)) + 1
            prior.update(last_stage=stage, last_attempt_at=_now(), retryable=False)
            row["attempt"] = prior[key]
        _write(state_path, state)
        try:
            kwargs = {"work": work}
            if stage == "qc":
                kwargs["resume_stage"] = "qc"
            result = dispatcher([task for task, _ in selected], **kwargs)
            if not isinstance(result, list):
                raise ValueError("DISPATCH_RESULT_LIST_REQUIRED")
            summary["dispatch_results"].extend(result)
            for task, row in selected:
                outcomes = [item for item in result if isinstance(item, dict)
                            and item.get("record_id") == task["record_id"]]
                row.update(status="DISPATCHED" if outcomes else "UNKNOWN", results=outcomes)
                # A caught dispatcher exception must not invent retryability.
                records[task["record_id"].casefold()]["retryable"] = any(
                    item.get("retryable") is True for item in outcomes)
                control_outcomes = [item for item in outcomes if not (
                    item.get("stage") == "material" and item.get("status") == "READY")]
                if (control_outcomes and all((item.get("stage") == "preflight" and item.get("status") == "BLOCKED")
                        or (item.get("preflight_only") is True and item.get("status") == "ERROR")
                        for item in control_outcomes)):
                    # No build or QC began. Any completed material preparation
                    # remains cached; retry the blocked step through this entry.
                    prior = records[task["record_id"].casefold()]
                    attempt_key = stage + "_attempts"
                    prior[attempt_key] = max(0, int(prior.get(attempt_key, 0)) - 1)
                    prior["retryable"] = True
                    row["preflight_only"] = True
        except Exception as exc:
            for _, row in selected:
                row.update(status="ERROR", reason="DISPATCH_FAILED", error=str(exc))
        _write(state_path, state)


def run_once(queue_path, work_root, *, dry_run: bool = False, dispatcher=None,
             record_id: str | None = None) -> dict:
    """Run an admitted local queue once; dry-run only writes its local summary.

    ``dispatcher`` accepts batch_dispatch.dispatch's ``tasks, work`` and optional
    ``resume_stage`` arguments. It exists for offline tests, not alternate gates.
    """
    queue_path, work = Path(queue_path).resolve(), Path(work_root).resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12]
    summary = {"schema_version": 1, "run_id": run_id, "started_at": _now(),
               "queue_path": str(queue_path), "work_root": str(work), "dry_run": bool(dry_run),
               "status": "OK", "tasks": [], "dispatch_results": []}
    try:
        if dry_run:
            _execute(queue_path, work, summary, dispatcher, record_id)
        else:
            with _pipeline_lock(work):
                _execute(queue_path, work, summary, dispatcher, record_id)
        if any(row["status"] in {"ERROR", "UNKNOWN"} for row in summary["tasks"]):
            summary["status"] = "ERROR"
        if any(isinstance(row, dict) and (row.get("rc", 0) != 0 or row.get("status") in {
                "ERROR", "BLOCKED", "PARTIAL", "UNCERTIFIED", "UNCERTIFIED_TIMEOUT"})
               for row in summary["dispatch_results"]):
            summary["status"] = "ERROR"
    except TimeoutError as exc:
        summary.update(status="SKIPPED", reason=str(exc))
    except Exception as exc:
        summary.update(status="ERROR", reason="PIPELINE_RUN_FAILED", error=str(exc))
    summary["finished_at"] = _now()
    summary["summary_path"] = str(work / "pipeline_runs" / (run_id + ".json"))
    _write(Path(summary["summary_path"]), summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--record-id")
    args = parser.parse_args(argv)
    result = run_once(args.queue, args.work_root, dry_run=args.dry_run, record_id=args.record_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(result["status"] == "ERROR")


if __name__ == "__main__":
    raise SystemExit(main())
