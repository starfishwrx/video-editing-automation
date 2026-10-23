"""Append private feedback candidates; never promote rules or approve reviews."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import production_eval as pe

STATES = ('needs_review', 'reviewed', 'regression_verified', 'released')


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    if path.is_symlink():
        raise ValueError('LEDGER_SYMLINK_REJECTED')
    rows = []
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        body = dict(row)
        supplied = body.pop('entry_sha256', None)
        if supplied != pe.digest(body):
            raise ValueError('LEDGER_ENTRY_HASH_MISMATCH')
        rows.append(row)
    return rows


def append_once(path: Path, entries: list[dict]) -> dict:
    """Serialize appends and deduplicate retries without rewriting old entries."""
    path = path.absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ValueError('LEDGER_BUSY_OR_STALE_LOCK') from exc
    try:
        os.close(fd)
        existing = read_rows(path)
        by_id = {row['entry_id']: row for row in existing}
        additions = []
        for entry in entries:
            body = dict(entry)
            body['entry_sha256'] = pe.digest(body)
            previous = by_id.get(body['entry_id'])
            if previous:
                if previous != body:
                    raise ValueError('LEDGER_ENTRY_ID_COLLISION')
                continue
            additions.append(body)
            by_id[body['entry_id']] = body
        if additions:
            # A crash can leave a partial last line; the next read fails closed.
            with path.open('a', encoding='utf-8', newline='\n') as stream:
                for row in additions:
                    stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                            allow_nan=False) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
        return {'path': str(path), 'appended': len(additions),
                'entry_ids': [entry['entry_id'] for entry in entries]}
    finally:
        lock.unlink()


def evidence_refs(raw: object, *, required: bool = False, resolver=None) -> list[dict]:
    if not isinstance(raw, list) or (required and not raw):
        raise ValueError('FEEDBACK_EVIDENCE_REQUIRED')
    refs = []
    for ref in raw:
        if not isinstance(ref, dict) or not isinstance(ref.get('path'), str):
            raise ValueError('FEEDBACK_EVIDENCE_INVALID')
        path = resolver(ref['path']) if resolver else Path(ref['path'])
        actual = pe.file_hash(path)
        if ref.get('sha256') != actual:
            raise ValueError('FEEDBACK_EVIDENCE_HASH_MISMATCH')
        refs.append({'path': str(path.absolute()), 'sha256': actual})
    return refs


def collect(task_dir: Path, ledger: Path) -> dict:
    task = pe.read(task_dir / 'task.json')
    contract = pe.read(task_dir / 'eval' / 'active_contract.json')
    pe.verify_contract(contract)
    if str(task.get('task_id') or task.get('record_id')) != contract['task_id']:
        raise ValueError('FEEDBACK_TASK_BINDING_MISMATCH')
    if pe.digest(task) != contract.get('task_sha256'):
        raise ValueError('FEEDBACK_TASK_CONTENT_DRIFT')
    resolution_path = task_dir / 'feedback_resolution.json'
    resolution = pe.read(resolution_path)
    issue = str(task.get('issue') or '')
    if not issue.strip():
        raise ValueError('FEEDBACK_ORIGINAL_ISSUE_REQUIRED')
    issue_hash = hashlib.sha256(issue.encode('utf-8')).hexdigest()
    if any(resolution.get(key) != value for key, value in {
            'task_id': contract['task_id'], 'run_id': contract['run_id'],
            'issue_sha256': issue_hash}.items()):
        raise ValueError('FEEDBACK_RESOLUTION_BINDING_MISMATCH')
    refs = evidence_refs(resolution.get('evidence', []))
    done_path = task_dir / 'done.json'
    done = pe.read(done_path) if done_path.is_file() else {}
    receipts = [{'path': str(path.absolute()), 'sha256': pe.file_hash(path)}
                for path in (resolution_path, task_dir / 'eval' / 'active_contract.json', done_path)
                if path.is_file()]
    for qc in done.get('qc_reports') or []:
        path = Path(str(qc.get('report') or ''))
        if path.is_file():
            receipts.append({'path': str(path.absolute()), 'sha256': pe.file_hash(path)})
    entry = {'schema_version': 1, 'kind': 'feedback_candidate',
             'task_id': contract['task_id'], 'run_id': contract['run_id'],
             'contract_hash': contract['contract_hash'], 'task_sha256': contract['task_sha256'],
             'issue': issue, 'issue_sha256': issue_hash,
             'resolution_sha256': pe.file_hash(resolution_path),
             'resolution_status': str(resolution.get('status') or 'unresolved'),
             'proposed_rule': str(resolution.get('proposed_rule') or resolution.get('rule_text') or ''),
             'rule_key': str(resolution.get('rule_key') or task.get('feedback_rule_key') or ''),
             'repair': resolution.get('repair', {}), 'evidence': refs, 'receipts': receipts,
             'review_status': 'needs_review', 'reviewed_by': '', 'approved': False,
             'regression_case': None, 'release': None}
    # Identity excludes collection time so rerunning the collector is a no-op.
    entry['entry_id'] = pe.digest(entry)
    result = append_once(ledger, [entry])
    return {'status': 'FEEDBACK_CANDIDATE_COLLECTED', **result,
            'review_status': 'needs_review', 'rules_modified': False}


def candidate_state(rows: list[dict], entry_id: str) -> dict:
    candidates = [row for row in rows if row.get('entry_id') == entry_id
                  and row.get('kind') == 'feedback_candidate']
    if len(candidates) != 1:
        raise ValueError('FEEDBACK_CANDIDATE_NOT_FOUND_OR_AMBIGUOUS')
    base = candidates[0]
    events = [row for row in rows if row.get('kind') == 'feedback_candidate_state'
              and row.get('candidate_entry_id') == entry_id]
    status = 'needs_review'
    for event in events:
        if event.get('previous_status') != status or event.get('status') not in STATES[1:]:
            raise ValueError('FEEDBACK_STATE_CHAIN_INVALID')
        status = event['status']
    return {'entry_id': entry_id, 'task_id': base['task_id'], 'run_id': base['run_id'],
            'contract_hash': base['contract_hash'], 'task_sha256': base['task_sha256'],
            'status': status, 'events': events}


def transition(ledger: Path, entry_id: str, status: str, reviewer: str,
               evidence: list[dict], *, regression_case: str = '', release: str = '') -> dict:
    if status not in STATES[1:] or not reviewer.strip():
        raise ValueError('FEEDBACK_REVIEWER_AND_VALID_TARGET_STATE_REQUIRED')
    rows = read_rows(ledger)
    current = candidate_state(rows, entry_id)
    expected = {'reviewed': 'needs_review', 'regression_verified': 'reviewed', 'released': 'regression_verified'}[status]
    if current['status'] != expected:
        raise ValueError('FEEDBACK_STATE_PREREQUISITE_REQUIRED:' + expected)
    refs = evidence_refs(evidence, required=True)
    if status == 'regression_verified' and not regression_case.strip():
        raise ValueError('FEEDBACK_REGRESSION_CASE_REQUIRED')
    if status == 'released' and not release.strip():
        raise ValueError('FEEDBACK_RELEASE_REFERENCE_REQUIRED')
    event = {'schema_version': 1, 'kind': 'feedback_candidate_state', 'entry_id': entry_id,
             'task_id': current['task_id'], 'run_id': current['run_id'],
             'contract_hash': current['contract_hash'], 'task_sha256': current['task_sha256'],
             'previous_status': current['status'],
             'status': status, 'reviewer': reviewer.strip(), 'evidence': refs,
             'regression_case': regression_case.strip() or None, 'release': release.strip() or None}
    event['entry_id_state'] = pe.digest(event)
    # append_once keys on entry_id, so state events use their own stable event identity.
    event['entry_id'] = event['entry_id_state']
    event['candidate_entry_id'] = entry_id
    result = append_once(ledger, [event])
    return {'status': 'FEEDBACK_CANDIDATE_STATE_RECORDED', **result,
            'candidate': candidate_state(read_rows(ledger), entry_id), 'rules_modified': False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    collect_parser = sub.add_parser('collect')
    collect_parser.add_argument('--task-dir', required=True, type=Path)
    transition_parser = sub.add_parser('transition')
    transition_parser.add_argument('--entry-id', required=True)
    transition_parser.add_argument('--status', required=True, choices=STATES[1:])
    transition_parser.add_argument('--reviewer', required=True)
    transition_parser.add_argument('--evidence', required=True, type=Path,
                                   help='JSON array of evidence refs with verified SHA256')
    transition_parser.add_argument('--regression-case', default='')
    transition_parser.add_argument('--release', default='')
    status_parser = sub.add_parser('status')
    status_parser.add_argument('--entry-id', required=True)
    for command_parser in (collect_parser, transition_parser, status_parser):
        command_parser.add_argument('--ledger', required=True, type=Path,
                                    help='explicit private append-only JSONL path')
    args = parser.parse_args()
    if args.command == 'collect':
        result = collect(args.task_dir, args.ledger)
    elif args.command == 'transition':
        result = transition(args.ledger, args.entry_id, args.status, args.reviewer,
                            pe.read(args.evidence), regression_case=args.regression_case,
                            release=args.release)
    else:
        result = candidate_state(read_rows(args.ledger), args.entry_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
