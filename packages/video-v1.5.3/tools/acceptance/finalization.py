"""Explicit task-level acceptance and authorized delivery coordination."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import pathlib
import time

from . import evaluation as production_eval, delivery_gate


@dataclass(frozen=True)
class DeliveryContext:
    draft_root: pathlib.Path
    workflow_root: pathlib.Path
    skill_root: pathlib.Path
    work_root: pathlib.Path
    runtime_root: pathlib.Path
    closed_loop_tool: pathlib.Path
    closed_loop_policy: pathlib.Path
    python: str


def finalize_closed_loop(task_dir: pathlib.Path, task: dict, drafts: list[str],
                         qc_reports: list[dict], delivery_receipts: list[dict] | None = None, *,
                         context: DeliveryContext, read_json, evaluate, validate_package,
                         ship, run, verify_shared, tree_identity) -> dict:
    """Evaluate first, then ship hash-verified copies and write back the table."""
    done_path = task_dir / "done.json"
    existing_done = read_json(done_path)
    provisional = dict(existing_done)
    provisional.update({"record_id": task.get("record_id"), "status": "PREVIEW_READY",
        "drafts": list(drafts), "local_only_drafts": list(drafts),
        "qc_reports": qc_reports, "nas_links": [],
        "delivery_mode": task.get("delivery_mode", "preview"),
        "distribution_status": "PENDING_ACCEPTANCE",
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
    delivery_started = False

    def blocked(reason: str, **extra) -> dict:
        done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"ok": False, "status": "BLOCKED", "reason": reason,
                "delivery_started": delivery_started, **extra}

    pre_delivery = delivery_gate.issues(task_dir, task, provisional, context.draft_root)
    if pre_delivery:
        return blocked(pre_delivery[0], issues=pre_delivery)
    if not context.closed_loop_tool.is_file() or not context.closed_loop_policy.is_file():
        return blocked("CLOSED_LOOP_TOOL_OR_POLICY_MISSING")
    delivery_policy = read_json(context.closed_loop_policy)
    if not isinstance(delivery_policy, dict) or not delivery_policy.get("policy_id"):
        return blocked("CLOSED_LOOP_POLICY_INVALID")
    package_checks = [validate_package(context.draft_root / name) for name in drafts]
    if not package_checks or any(not check.get("ok") for check in package_checks):
        return blocked("PACKAGE_INCOMPLETE", checks=package_checks)
    frozen_path = task_dir / "eval" / "active_contract.json"
    frozen = None
    eval_result = None
    if frozen_path.is_file():
        frozen = production_eval.read(frozen_path)
        result = evaluate(task_dir, task, drafts, qc_reports, frozen)
        eval_result = result
        provisional["eval_receipt"] = result
        eval_issues = delivery_gate.issues(task_dir, task, provisional, context.draft_root,
                                           evaluation=result)
        if eval_issues:
            return blocked(eval_issues[0], issues=eval_issues, eval=result)
        if result["status"] != "PASS":
            done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "EVAL_DELIVERY_GATE", "eval": result}
    else:
        done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "EVAL_CONTRACT_MISSING"}

    receipts = list(delivery_receipts or existing_done.get('distribution_receipts') or [])
    by_name = {str(row.get('draft_name') or ''): row for row in receipts if isinstance(row, dict)}
    if frozen is None:
        frozen = {}
    provisional['eval_receipt'] = eval_result or provisional.get('eval_receipt')
    provisional['distribution_receipts'] = []
    done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
    for name in drafts:
        source = context.draft_root / name
        current_identity = production_eval.output_identity([source])
        receipt = dict(by_name[name]) if name in by_name else None
        if receipt:
            if (receipt.get('status') != 'VERIFIED'
                    or receipt.get('source_output_sha256') != current_identity['sha256']
                    or receipt.get('draft_name') != name
                    or (frozen.get('task_id') and receipt.get('task_id') != frozen['task_id'])
                    or (frozen.get('run_id') and receipt.get('run_id') != frozen['run_id'])
                    or (frozen.get('contract_hash')
                        and receipt.get('contract_hash') != frozen['contract_hash'])):
                return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "DISTRIBUTION_SOURCE_DRIFT", "draft": name}
            target = pathlib.Path(str(receipt.get('nas_target') or ''))
            try:
                target_identity = tree_identity(target)
            except (OSError, ValueError):
                return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "DISTRIBUTION_READBACK_MISSING", "draft": name}
            if target_identity['sha256'] != receipt.get('transformed_tree_sha256'):
                return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "DISTRIBUTION_READBACK_DRIFT", "draft": name}
            # A legacy hash receipt may certify equally broken copies. Resume
            # must recheck the shared files too, rather than trust old flags.
            try:
                reference_root = target.parent.parent / name
                verify_shared(target, reference_root)
            except (RuntimeError, OSError, ValueError) as exc:
                return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "DISTRIBUTION_MEDIA_REFERENCE_INVALID",
                        "draft": name, "detail": str(exc)}
            receipt['shared_media_references_verified'] = True
            receipt['shared_reference_root'] = str(reference_root)
        else:
            delivery_started = True
            shipped = ship(source, task_dir=task_dir, frozen=frozen)
            if shipped.get('rc') != 0:
                provisional['distribution_status'] = 'PENDING_SHIP_VERIFICATION'
                done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
                return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "SHIP_OR_READBACK_FAILED",
                        "draft": name, "detail": shipped.get('out')}
            receipt = shipped['receipt']
        if frozen.get('contract_hash'):
            receipt['eval_receipt_sha256'] = (eval_result or {}).get('receipt_sha256')
            receipt['eval_status'] = (eval_result or {}).get('status', 'UNCERTIFIED')
        receipt['status'] = 'VERIFIED'
        receipt.pop('receipt_path', None)
        receipt_body = dict(receipt)
        receipt_body.pop('receipt_sha256', None)
        receipt['receipt_sha256'] = production_eval.digest(receipt_body)
        receipt_dir = task_dir / 'eval' / (frozen.get('run_id') or 'legacy')
        receipt_path = receipt_dir / ('distribution-' + receipt['receipt_sha256'] + '.json')
        production_eval.write_once(receipt_path, receipt)
        bound = dict(receipt, receipt_path=str(receipt_path))
        provisional['distribution_receipts'].append(bound)
        provisional['nas_links'].append(receipt['nas_target'])
        provisional['distribution_status'] = 'SHIPPED_VERIFIED'
        done_path.write_text(json.dumps(provisional, ensure_ascii=False, indent=2), encoding="utf-8")
    if len(provisional['distribution_receipts']) != len(drafts):
        return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "DISTRIBUTION_RECEIPT_COUNT_MISMATCH"}
    if not context.closed_loop_tool.is_file() or not context.closed_loop_policy.is_file():
        return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "context.closed_loop_tool_OR_POLICY_MISSING"}
    # The closed-loop tool needs an eligible input, but the authoritative
    # done.json must remain pending until archive and table update succeed.
    candidate_path = task_dir / "closed_loop_candidate.json"
    candidate = dict(provisional, status='OK', local_only_drafts=[])
    candidate_path.write_text(json.dumps(candidate, ensure_ascii=False, indent=2), encoding="utf-8")
    common = [context.python, str(context.closed_loop_tool), "--task-json", str(task_dir / "task.json"), "--done-json", str(candidate_path), "--policy", str(context.closed_loop_policy)]
    delivery_env = dict(os.environ, JY_EVAL_RUNTIME_ROOT=str(context.runtime_root),
                        JY_DRAFT_ROOT=str(context.draft_root))
    delivery_started = True
    # archive-parent re-reads every shipped byte over the NAS share (audit readback,
    # package check, tree identity). Headless drafts with upright-normalized media are
    # several GB, so a fixed 300 s budget cannot cover the verification it must perform.
    archive_timeout = float(os.environ.get("JY_ARCHIVE_PARENT_TIMEOUT_S", "1200"))
    archive = run(common[:2] + ["archive-parent"] + common[2:], cwd=str(context.workflow_root), env=delivery_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=archive_timeout)
    if archive.returncode != 0:
        return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "ARCHIVE_PARENT_FAILED", "output": (archive.stdout or archive.stderr)[-2000:]}
    update_timeout = float(os.environ.get("JY_TABLE_UPDATE_TIMEOUT_S", "1200"))
    update = run(common[:2] + ["table-update"] + common[2:], cwd=str(context.workflow_root), env=delivery_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=update_timeout)
    if update.returncode != 0:
        return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "TABLE_UPDATE_FAILED", "output": (update.stdout or update.stderr)[-2000:]}
    try:
        receipt = json.loads(update.stdout)
    except (ValueError, TypeError):
        return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "TABLE_UPDATE_RECEIPT_INVALID"}
    if not isinstance(receipt, dict) or receipt.get('ok') is not True:
        return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "TABLE_UPDATE_RECEIPT_INVALID"}
    completed = read_json(candidate_path)
    if not completed or completed.get('status') != 'OK':
        return {"delivery_started": delivery_started, "ok": False, "status": "BLOCKED", "reason": "CLOSED_LOOP_CANDIDATE_INVALID"}
    done_path.write_text(json.dumps(completed, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"delivery_started": delivery_started, "ok": True, "status": "CLOSED_LOOP_COMPLETE", "archive": archive.stdout[-2000:], "table": receipt}
