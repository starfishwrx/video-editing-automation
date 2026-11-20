"""Measure an enrolled batch against its first editable output and verified formal delivery."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path, PureWindowsPath


def revision(text):
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def read(path, fallback=None):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else fallback


def queue_identity(tasks):
    rows = sorted(({"record_id": str(t.get("record_id") or ""), "task_id": str(t.get("task_id") or ""),
                    "proc_key": str(t.get("proc_key") or ""),
                    "script_sha256": hashlib.sha256(str(t.get("script") or "").encode("utf-8")).hexdigest()}
                   for t in tasks), key=lambda r: (r["record_id"], r["task_id"], r["proc_key"], r["script_sha256"]))
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def visual_scope(task):
    from tools.queueing import contracts
    nested = task.get("manifest") if isinstance(task.get("manifest"), dict) else {}
    notes = contracts.task_shot_note_overrides(task, remark_field="画面备注", new_task=True)
    return {"task_id": task.get("task_id") or task.get("record_id"), "product": contracts.task_product_fields(task),
            "overrides": contracts.task_runtime_overrides(task), "notes": notes,
            "inputs": {k: task.get(k) or nested.get(k) for k in (
                "revision_feedback", "issue", "material_dir", "material_sources", "voice", "voice_tempo", "bgm")}}


def revision_work_root(task, work):
    """Give a changed script its own durable receipts and drafts under the existing record identity."""
    rid = task.get("record_id")
    script = task.get("script")
    if not isinstance(rid, str) or Path(rid).name != rid or rid in {"", ".", ".."} or not isinstance(script, str) or not script.strip():
        return work
    original = read(work / rid / "task.json", {})
    done = read(work / rid / "done.json", {})
    old = done.get("first_pass_script_sha256") or (revision(original["script"]) if original.get("script") else None)
    current = revision(script)
    scope = visual_scope(task)
    scope_changed = bool(original and visual_scope(original) != scope)
    if old and (current != old or scope_changed):
        identity = current
        if scope_changed:
            identity += "-" + hashlib.sha256(json.dumps(scope, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        return work / "script_revisions" / identity
    return work


def recovered_cohort(task, done):
    cohort = done.get("first_pass_cohort_id")
    if (cohort and done.get("run_id") and done.get("record_id") == task.get("record_id")
            and done.get("task_id") == (task.get("task_id") or task.get("record_id"))
            and done.get("first_pass_script_sha256") == revision(str(task.get("script") or ""))
            and done.get("first_pass_business_batch") == (task.get("batch_id") or task.get("batch_identity"))
            and done.get("terminal_state") != "DELIVERED" and not task.get("force_rebuild")):
        return cohort
    return None


def delivered(directory, rid, task_id, run_ids):
    done = read(directory / "done.json", {})
    table = read(directory / "table_update_receipt.json", {})
    if (done.get("record_id") != rid or done.get("task_id") != task_id or done.get("run_id") not in run_ids
            or done.get("terminal_state") != "DELIVERED" or done.get("delivery_mode") != "formal"
            or done.get("distribution_status") not in {"VERIFIED", "GROUPED_VERIFIED"} or table.get("task_id") != task_id
            or table.get("record_id") != rid or table.get("ok") is not True or table.get("write_acknowledged") is not True
            or table.get("readback_verified") is not True or table.get("status") != "TABLE_UPDATE_VERIFIED"):
        return None
    drafts = set(done.get("drafts") or [])
    qc = {q.get("draft") for q in done.get("qc_reports") or [] if q.get("status") == "PASS"}
    shipped = {s["draft_name"]: PureWindowsPath(s["nas_target"]) for s in done.get("distribution_receipts") or []
               if s.get("task_id") == task_id and s.get("run_id") == done["run_id"] and s.get("nas_target")
               and s.get("draft_name") and s.get("distribution_verified") is True}
    parent = done.get("nas_parent_folder")
    if not parent or (table.get("patch") or {}).get("草稿文件") != parent:
        return None
    path = PureWindowsPath(parent)
    if not drafts or not drafts <= qc or drafts != shipped.keys() or any(p.name != name for name, p in shipped.items()):
        return None
    if done["distribution_status"] == "VERIFIED":
        return done["run_id"] if len(drafts) == 1 and path in shipped.values() else None
    from tools.workflow.closed_loop import digest
    archive = read(directory / "distribution_receipt.json", {})
    body = {k: archive.get(k) for k in ("parent_folder", "versions", "source_links", "version_identities")}
    versions, identities = body["versions"] or [], body["version_identities"] or []
    links = [PureWindowsPath(v) for v in body["source_links"] or []]
    if (archive.get("record_id") == rid and archive.get("verified") is True and archive.get("self_contained_versions") is True
            and archive.get("receipt_hash") == digest(body) and body["parent_folder"] == parent
            and set(links) == set(shipped.values()) and len(links) == len(versions) == len(identities) == len(drafts)
            and all(PureWindowsPath(v) == path / ("版本%d" % i) for i, v in enumerate(versions, 1))
            and all(isinstance(i, dict) and i.get("sha256") and i.get("file_count", 0) > 0 for i in identities)):
        return done["run_id"]
    return None


def summarize(tasks, work, events, reviews):
    by_order = defaultdict(list)
    for event in events:
        by_order[event.get("record_id")].append(event)
    categories = defaultdict(Counter)
    rows, seen = [], {}
    snapshot = queue_identity(tasks)
    for task in tasks:
        rid = task.get("record_id") or task.get("task_id")
        if not isinstance(rid, str) or Path(rid).name != rid or rid in {".", "..", ""}:
            raise ValueError("INVALID_COHORT_RECORD_ID")
        token = task.get("script_sha256") or revision(task.get("script") or task.get("full_script") or "")
        if rid in seen:
            original = seen[rid]
            if token != original["script_sha256"] and token not in original["later_script_revisions"]:
                original["later_script_revisions"].append(token)
            continue
        category = task.get("category") or "unknown"
        counts = categories[category]
        counts["enrolled"] += 1
        task_id = task.get("task_id") or rid
        explicit = task.get("batch_id") or task.get("batch_identity") or task.get("worker_batch_identity")
        cohort = task.get("first_pass_cohort_id")
        owned = [e for e in by_order[rid] if cohort and e.get("first_pass_cohort_id") == cohort
                 and e.get("script_sha256") == token and (not explicit or e.get("batch_id") == explicit)]
        drafts = [e for e in owned if e.get("event") == "draft_written"]
        first = drafts[0] if drafts else {}
        fingerprint = first.get("draft_metadata_sha256")
        batch_id = first.get("batch_id") or explicit or snapshot
        machine = next((e.get("status") for e in owned if fingerprint and e.get("event") == "draft_qc"
                        and e.get("draft_metadata_sha256") == fingerprint
                        and e.get("run_id") == first["run_id"] and e.get("draft") == first["draft"]), None)
        audit = reviews.get(rid, {})
        if (not cohort or audit.get("first_pass_cohort_id") != cohort or audit.get("script_sha256") != token
                or audit.get("batch_id") != batch_id):
            audit = {}
        blind = audit.get("blind") if fingerprint and audit.get("draft_metadata_sha256") == fingerprint else None
        first_pass = blind == "PASS" and audit.get("manual_before_first") is False
        delivery_run = delivered(work / rid, rid, task_id, {e["run_id"] for e in owned if e.get("run_id")})
        audited_delivery = bool(delivery_run)
        manual_delivery = audit.get("manual_before_delivery") if delivery_run and audit.get("delivery_run_id") == delivery_run else None
        unattended = audited_delivery and manual_delivery is False and audit.get("manual_before_first") is False
        counts["first_business_pass"] += int(first_pass)
        counts["first_machine_pass"] += int(machine == "PASS")
        counts["unattended_delivery"] += int(unattended)
        counts["formal_delivery"] += int(audited_delivery)
        counts["blind_pending"] += int(blind not in {"PASS", "FAIL"})
        counts["false_release"] += int(machine == "PASS" and blind == "FAIL")
        counts["false_rejection"] += int(bool(machine) and machine != "PASS" and blind == "PASS")
        counts["manual_intervention"] += int(audit.get("manual_before_first") is True or manual_delivery is True)
        counts["unverified_intervention"] += int(type(manual_delivery) is not bool)
        row = {"record_id": rid, "category": category, "first_pass_cohort_id": cohort, "script_sha256": token,
               "recorded_worker_http_calls": None, "later_script_revisions": [],
               "first_draft": first, "first_qc": machine, "written_outputs": len(drafts), "blind": blind,
               "formal_delivery": audited_delivery, "first_business_pass": first_pass, "unattended_delivery": unattended,
               "first_draft_seconds": first.get("elapsed_seconds"), "draft_qc_checks": sum(e.get("event") == "draft_qc" for e in owned),
               "last_observed_seconds": max((e["elapsed_seconds"] for e in owned if type(e.get("elapsed_seconds")) in (int, float)), default=None),
               "model_calls": audit.get("model_calls") if type(audit.get("model_calls")) is int and audit["model_calls"] >= 0 and audit.get("call_count_source") else None}
        rows.append(row)
        seen[rid] = row
    for counts in categories.values():
        counts["first_business_rate"] = counts["first_business_pass"] / counts["enrolled"]
        counts["unattended_delivery_rate"] = counts["unattended_delivery"] / counts["enrolled"]
    return {"by_category": dict(categories), "orders": rows}


def attach_http_counts(report, database):
    import sqlite3
    from contextlib import closing
    if not database.is_file():
        return
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        for row in report["orders"]:
            cohort = row.get("first_pass_cohort_id")
            if not cohort:
                continue
            count = conn.execute(
                "SELECT count(*) FROM usage WHERE stage='model_http' AND json_extract(detail,'$.first_pass_cohort_id')=? "
                "AND json_extract(detail,'$.first_pass_record_id')=?", (cohort, row["record_id"])).fetchone()[0]
            if count:
                row["recorded_worker_http_calls"] = count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--usage-db", type=Path, help="Optional read-only material evidence ledger for worker HTTP counts")
    args = parser.parse_args()
    queue = read(args.queue)
    history = args.work / "batch_progress.jsonl"
    lines = history.read_text(encoding="utf-8-sig").splitlines() if history.is_file() else []
    report = summarize(queue["tasks"] if isinstance(queue, dict) else queue, args.work,
                       [json.loads(line) for line in lines if line.strip()], read(args.reviews, {}) if args.reviews else {})
    if args.usage_db:
        attach_http_counts(report, args.usage_db)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
