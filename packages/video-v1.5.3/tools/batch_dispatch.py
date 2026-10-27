"""Single host batch supervisor; product preparation never occupies build workers.

Run with ``python -m tools.batch_dispatch run --queue work/batch_queue.json``.
All subprocesses inherit the same immutable policy file and account slot path.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "doubao-jianying-orchestrator"
if str(SKILL) not in sys.path:
    sys.path.insert(0, str(SKILL))
from orchestrator.file_safety import atomic_write_text, file_lock
from orchestrator.runtime_policy import RuntimePolicy, load_policy
from orchestrator.proc_slots import slot
from orchestrator.visual_qc_contract import report_status, report_matches_draft


def _string_seconds(value) -> str:
    """Format seconds for env vars: integer seconds as '360', otherwise keep float string."""
    try:
        val = float(value)
        return str(int(val)) if float(val).is_integer() else str(val)
    except Exception:
        return str(value)


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def product_key(task: dict) -> str:
    # Even explicit SKU identities include their source location: two different
    # material inventories may not silently borrow the other's observations.
    manifest = task.get("manifest") or {}
    sources = task.get("material_sources") or manifest.get("material_sources") or []
    identity = {key: task.get(key) or manifest.get(key) for key in
                ("product_key", "product", "sku", "category")}
    identity["material_dir"] = str(Path(str(task.get("material_dir") or ".")).resolve()).casefold()
    identity["sources"] = sorted(str(Path(str(path)).resolve()).casefold() for path in sources)
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


def formal_delivery_count(receipt: dict) -> int:
    """Count actual verified deliveries with independent PASS evidence only."""
    if receipt.get("distribution_status") != "SHIPPED_VERIFIED":
        return 0
    passed = set()
    for row in receipt.get("qc_reports", []):
        if not isinstance(row, dict) or row.get("status") != "PASS" or not row.get("report"):
            continue
        report = _read(Path(row["report"]))
        if (report_status(report) == "PASS" and report.get("draft")
                and report_matches_draft(report, Path(report["draft"]))):
            passed.add(str(row.get("draft")))
    delivered = {str(row.get("draft_name")) for row in receipt.get("distribution_receipts", [])
                 if isinstance(row, dict) and row.get("draft_name")
                 and row.get("status") == "VERIFIED" and row.get("nas_target")
                 and row.get("shared_media_references_verified") is True}
    return len(passed & delivered & {str(name) for name in receipt.get("drafts", [])})


def _qc_pending(task: dict, work: Path) -> bool:
    done = _read(work / task["record_id"] / "done.json")
    if done.get("resume_stage") == "qc":
        return True
    if done.get("drafts"):
        reports = {row.get("draft"): row for row in done.get("qc_reports", []) if isinstance(row, dict)}
        for name in done["drafts"]:
            row = reports.get(name) or {}
            report = _read(Path(row["report"])) if row.get("report") else {}
            if (row.get("status") != "PASS" or report_status(report) != "PASS"
                    or not report.get("draft") or not report_matches_draft(report, Path(report["draft"]))):
                return True
    if any(row.get("status") not in {"PASS", "PASS_WITH_GAPS", "PASS_WITH_DEGRADED"}
           for row in done.get("qc_reports", []) if isinstance(row, dict)):
        return bool(done.get("drafts"))
    return any(_read(path).get("resume_stage") == "qc"
               for path in (work / task["record_id"] / "report").glob("**/qc_stage.json"))


def _child_env(policy_file: Path, policy: RuntimePolicy, task: dict | None = None) -> dict:
    env = dict(os.environ, PYTHONUTF8="1",
        JY_CONCURRENCY_POLICY_FILE=str(policy_file), JY_SLOT_DIR=policy.slot_dir,
        JY_VISION_TEXT_PASS="1", JY_VISION_RETRIES="2",
        JY_VISION_SLOTS=str(policy.vision_slots),
        JY_VISION_BATCH_WORKERS=str(policy.api_workers),
        JY_VISION_PREP_WORKERS=str(policy.prep_workers),
        JY_VISION_PREP_SLOTS=str(policy.prep_slots),
        JY_VISION_TIMEOUT=_string_seconds(policy.request_timeout),
        JY_POST_QC_TIMEOUT_S=_string_seconds(policy.request_timeout),
        JY_POST_QC_STAGE_TIMEOUT_S=str(policy.qc_stage_timeout),
        JY_TASK_BUDGET_S=str(int(policy.task_timeout)), JY_TASK_BUDGET_OVERRIDE="1",
        JY_TASK_DEADLINE_EPOCH=str(time.time() + policy.task_timeout))
    if task:
        env["JY_TASK_RECORD_ID"] = task["record_id"]
    env.pop("JY_STAGE_DEADLINE_TS", None)
    env.pop("JY_STAGE_DEADLINE_EPOCH", None)
    env["JY_TASK_DEADLINE_TS"] = env["JY_TASK_DEADLINE_EPOCH"]
    return env


def prepare_product(task: dict, product_dir: Path, env: dict) -> str:
    """Validate hot caches or prepare a cold product through the canonical CLI."""
    import batch_worker
    sources = task.get("material_sources") or batch_worker.video_files(str(task.get("material_dir") or ""))
    if not sources:
        raise ValueError("MATERIAL_PREP_NO_SOURCES")
    manifest = dict(task.get("manifest") or {})
    manifest.update(material_sources=sources)
    for key in ("product", "product_key", "sku", "category"):
        if key in task:
            manifest[key] = task[key]
    product_dir.mkdir(parents=True, exist_ok=True)
    path = product_dir / "manifest.json"
    atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2))
    proc = subprocess.run([sys.executable, "-m", "orchestrator.cli", "analyze-shots",
        "--input", str(path), "--report-dir", str(product_dir)], cwd=str(SKILL),
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=max(1, float(env["JY_TASK_DEADLINE_EPOCH"]) - time.time()) + 30)
    output = product_dir / "shot_candidates.json"
    payload = _read(output)
    if proc.returncode or not payload.get("material_cache", {}).get("ready"):
        raise RuntimeError("MATERIAL_PREP_INCOMPLETE: " + (proc.stderr or proc.stdout or "not ready")[-1200:])
    return str(output)


def run_task(task: dict, stage: str, env: dict, work: Path) -> dict:
    task = {key: value for key, value in task.items() if not key.startswith("_dispatch_")}
    task_dir = work / task["record_id"]
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / "task.json"
    prior_deliveries = formal_delivery_count(_read(task_dir / "done.json"))
    atomic_write_text(path, json.dumps(task, ensure_ascii=False, indent=2))
    env = dict(env, JY_WORK_ROOT=str(work))
    proc = subprocess.run([sys.executable, "-m", "tools.batch_dispatch", "_worker",
        "--task-file", str(path), "--stage", stage], cwd=str(ROOT), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=max(1, float(env["JY_TASK_DEADLINE_EPOCH"]) - time.time()) + 30)
    atomic_write_text(task_dir / "dispatch_worker.log", (proc.stdout or "") + "\n" + (proc.stderr or ""))
    receipt = _read(task_dir / "done.json")
    return {"record_id": task["record_id"], "stage": stage, "rc": proc.returncode,
            "formal_deliveries": max(0, formal_delivery_count(receipt) - prior_deliveries) if proc.returncode == 0 else 0,
            "status": receipt.get("status", "ERROR" if proc.returncode else "UNCERTIFIED")}


def submit(tasks: list[dict], *, work: Path, policy: RuntimePolicy,
           resume_stage: str | None = None) -> Path:
    """Durable local submission; the current supervisor absorbs this spool."""
    spool = Path(policy.slot_dir) / "batch_submissions"
    spool.mkdir(parents=True, exist_ok=True)
    path = spool / (str(time.time_ns()) + "-" + uuid.uuid4().hex + ".json")
    with file_lock(Path(policy.slot_dir) / "batch_dispatch_lifecycle"):
        atomic_write_text(path, json.dumps({"tasks": tasks, "work_root": str(work.resolve()),
                          "resume_stage": resume_stage}, ensure_ascii=False, indent=2))
    return path


@contextmanager
def _supervisor(policy: RuntimePolicy):
    lease = ExitStack()
    try:
        lease.enter_context(file_lock(Path(policy.slot_dir) / "batch_dispatch", timeout=0))
    except TimeoutError:
        yield False
        return
    try:
        yield lease
    finally:
        lease.close()


def dispatch(tasks: list[dict], *, work: Path, policy: RuntimePolicy | None = None,
             preparer: Callable = prepare_product, runner: Callable = run_task,
             dry_run: bool = False, resume_stage: str | None = None,
             _pending_submission: tuple | None = None) -> list[dict]:
    policy = policy or load_policy()
    started_at = time.time()
    unique, seen = [], set()
    for task in tasks:
        task = dict(task)
        rid = str(task.get("record_id") or "").strip()
        if not rid or Path(rid).name != rid or rid in {".", ".."}:
            raise ValueError("record_id must be one directory name")
        if rid not in seen:
            seen.add(rid)
            task["record_id"] = rid
            unique.append(task)
    pending = [(task, "qc" if resume_stage == "qc" or
                (not resume_stage and _qc_pending(task, work)) else "build") for task in unique]
    pending.sort(key=lambda job: 0 if job[1] == "qc" else 1)  # stable FIFO
    if dry_run:
        return [{"record_id": task["record_id"], "stage": "material" if resume_stage == "material" else stage,
                 "product_key": product_key(task)} for task, stage in pending]
    work.mkdir(parents=True, exist_ok=True)
    policy_file = work / "batch_dispatch_policy.json"
    # One supervisor covers all queues on this account/host, not merely this
    # queue file. A second entry fails rather than creating another 4 workers.
    with _supervisor(policy) as owner:
        if not owner:
            if _pending_submission is None:
                submission = submit(unique, work=work, policy=policy, resume_stage=resume_stage)
                # The previous owner may have stopped while we wrote the
                # submission. Reacquire once, or leave it with that live owner.
                return dispatch([], work=work, policy=policy, preparer=preparer, runner=runner,
                    resume_stage=resume_stage, _pending_submission=(submission, unique))
            submission, submitted_tasks = _pending_submission
            return [{"record_id": task["record_id"], "status": "SUBMITTED",
                     "submission": str(submission), "formal_deliveries": 0} for task in submitted_tasks]
        atomic_write_text(policy_file, json.dumps(policy.to_dict(), indent=2))
        progress = work / "batch_dispatch_progress.jsonl"
        atomic_write_text(progress, "")
        results, ready, preparing, failed = [], {}, set(), {}
        submitted_files = set()
        run_file = work / "dispatch_runs" / (str(time.time_ns()) + "-" + uuid.uuid4().hex[:8] + ".json")
        atomic_write_text(run_file, json.dumps({"policy": policy.to_dict(),
            "started_at": started_at, "finished_at": None, "results": []}, indent=2))

        def timed_runner(task, stage, env, task_work):
            before = time.time()
            outcome = runner(task, stage, env, task_work)
            return dict(outcome, elapsed_s=time.time() - before,
                        queue_wait_s=before - started_at)

        def timed_preparer(task, directory, env):
            before = time.time()
            output = preparer(task, directory, env)
            return output, time.time() - before

        def record(row):
            results.append(row)
            with progress.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(dict(row, at=time.time()), ensure_ascii=False) + "\n")

        with ThreadPoolExecutor(max_workers=policy.prep_workers) as prep_pool, \
                ThreadPoolExecutor(max_workers=policy.task_workers) as task_pool:
            futures = {}
            while True:
                for path in sorted((Path(policy.slot_dir) / "batch_submissions").glob("*.json")):
                    if path in submitted_files:
                        continue
                    submission = _read(path)
                    for task in submission.get("tasks", []):
                        rid = str(task.get("record_id") or "")
                        if not rid or Path(rid).name != rid or rid in seen:
                            continue
                        task = dict(task, _dispatch_work_root=submission["work_root"],
                                    _dispatch_resume_stage=submission.get("resume_stage"))
                        seen.add(rid)
                        stage = "qc" if submission.get("resume_stage") == "qc" or _qc_pending(task, Path(submission["work_root"])) else "build"
                        pending.append((task, stage))
                    submitted_files.add(path)
                pending.sort(key=lambda job: 0 if job[1] == "qc" else 1)
                if not pending and not futures:
                    # Submission writes and shutdown share this short lock.
                    # No submission can land between the final scan and lease
                    # release: a late submitter takes over ownership itself.
                    with file_lock(Path(policy.slot_dir) / "batch_dispatch_lifecycle"):
                        if any(path not in submitted_files for path in
                               (Path(policy.slot_dir) / "batch_submissions").glob("*.json")):
                            continue
                        for path in submitted_files:
                            path.unlink(missing_ok=True)
                        owner.close()
                    break
                for task, stage in list(pending):
                    key = product_key(task)
                    task_work = Path(task.get("_dispatch_work_root") or work)
                    requested_stage = task.get("_dispatch_resume_stage") or resume_stage
                    if not requested_stage:
                        previous = _read(task_work / task["record_id"] / "done.json")
                        if formal_delivery_count(previous) >= max(1, int(task.get("count") or 1)):
                            pending.remove((task, stage))
                            record({"record_id": task["record_id"], "status": "ALREADY_DELIVERED",
                                    "stage": "complete", "formal_deliveries": 0})
                            continue
                    if stage == "qc":
                        if sum(kind == "task" for kind, *_ in futures.values()) >= policy.task_workers:
                            continue
                    elif key in failed:
                        pending.remove((task, stage))
                        record({"record_id": task["record_id"], "stage": "material",
                                "status": "BLOCKED", "formal_deliveries": 0,
                                "error": failed[key]})
                        continue
                    elif key not in ready:
                        if key not in preparing and len(preparing) < policy.prep_workers:
                            preparing.add(key)
                            env = _child_env(policy_file, policy)
                            future = prep_pool.submit(timed_preparer, task, work / "products" / key, env)
                            futures[future] = ("prep", key, task)
                        continue
                    elif requested_stage == "material":
                        pending.remove((task, stage))
                        record({"record_id": task["record_id"], "stage": "material",
                                "status": "READY", "analysis_file": ready[key], "formal_deliveries": 0})
                        continue
                    elif sum(kind == "task" for kind, *_ in futures.values()) >= policy.task_workers:
                        continue
                    env = _child_env(policy_file, policy, task)
                    if key in ready:
                        env["JY_SHARED_ANALYSIS_FILE"] = ready[key]
                    future = task_pool.submit(timed_runner, task, stage, env, task_work)
                    futures[future] = ("task", key, task)
                    pending.remove((task, stage))
                if not futures:
                    continue
                poll_interval = float(os.environ.get("JY_DISPATCH_POLL_INTERVAL_S", "1.0"))
                completed, _ = wait(futures, timeout=max(0.01, poll_interval), return_when=FIRST_COMPLETED)
                if not completed:
                    # Timed out with no task completed; continue the loop to absorb new submissions
                    # and to fill any freed worker slots.
                    continue
                for future in completed:
                    kind, key, task = futures.pop(future)
                    try:
                        outcome = future.result()
                        if kind == "prep":
                            output, elapsed = outcome
                            ready[key] = str(output)
                            preparing.remove(key)
                            record({"record_id": task["record_id"], "stage": "material",
                                    "status": "READY", "elapsed_s": elapsed,
                                    "analysis_file": str(output), "formal_deliveries": 0})
                        else:
                            record(outcome)
                    except Exception as exc:
                        if kind == "prep":
                            preparing.discard(key)
                            failed[key] = str(exc)
                        else:
                            record({"record_id": task["record_id"], "status": "ERROR",
                                    "error": str(exc), "formal_deliveries": 0})
        atomic_write_text(run_file, json.dumps({"policy": policy.to_dict(),
            "started_at": started_at, "finished_at": time.time(), "results": results,
            "request_events": [str(path) for path in Path(policy.slot_dir).glob("vision_requests_*.jsonl")]},
            ensure_ascii=False, indent=2))
        return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "resume", "submit"):
        sub = subs.add_parser(name)
        sub.add_argument("--queue", type=Path)
        sub.add_argument("--work-root", type=Path)
        sub.add_argument("--dry-run", action="store_true")
        if name == "resume":
            sub.add_argument("--record-id", required=True)
            sub.add_argument("--stage", choices=("qc", "build", "material"), required=True)
    worker = subs.add_parser("_worker", help=argparse.SUPPRESS)
    worker.add_argument("--task-file", type=Path, required=True)
    worker.add_argument("--stage", choices=("qc", "build"), required=True)
    args = parser.parse_args(argv)
    if args.command == "_worker":
        import batch_worker
        task = _read(args.task_file)
        policy = load_policy()
        with slot("production_task", policy.task_workers, slot_dir=Path(policy.slot_dir), timeout=0) as admission:
            if not admission.acquired:
                raise RuntimeError("DISPATCH_TASK_CAPACITY_EXCEEDED")
            if args.stage == "qc":
                batch_worker.resume_task_qc(task)
            else:
                task.update(retry_deferred=True, retry_error=True)
                batch_worker.process_task(task, retry_partial=True, retry_error=True, admitted=True)
        return 0
    queue_path = args.queue or Path(os.environ.get("JY_WORK_ROOT", str(ROOT / "work"))) / "batch_queue.json"
    work = (args.work_root or queue_path.parent).resolve()
    tasks = json.loads(queue_path.read_text(encoding="utf-8-sig")) if queue_path.is_file() else []
    if isinstance(tasks, dict):
        tasks = tasks.get("tasks", [])
    if args.command == "resume":
        if Path(args.record_id).name != args.record_id or args.record_id in {".", ".."}:
            parser.error("record_id must be one directory name")
        selected = [task for task in tasks if str(task.get("record_id")) == args.record_id]
        tasks = selected or [_read(work / args.record_id / "task.json")]
        if not tasks[0] or len(tasks) != 1:
            parser.error("resume requires exactly one known record_id")
    result = dispatch(tasks, work=work, dry_run=args.dry_run,
                      resume_stage=getattr(args, "stage", None))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(any(row.get("rc", 0) != 0 or row.get("status") in {
        "ERROR", "BLOCKED", "PARTIAL", "UNCERTIFIED", "UNCERTIFIED_TIMEOUT"} for row in result))


if __name__ == "__main__":
    raise SystemExit(main())
