"""Private sentence labels and deterministic selector replay, without production writes.

Labels are evaluation data only. They never override formal evidence gates.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools import production_eval as pe
from tools.feedback_candidates import append_once, evidence_refs, read_rows

REASONS = {'wrong_selection', 'false_block', 'true_material_gap',
           'needs_observation', 'acceptable'}


def _sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
        raise ValueError('SENTENCE_SOURCE_HASH_REQUIRED')
    return value


def _window(raw: object) -> dict:
    if not isinstance(raw, dict) or set(raw) != {'start', 'duration'}:
        raise ValueError('SENTENCE_WINDOW_US_REQUIRED')
    if any(type(raw[k]) is not int for k in raw) or raw['start'] < 0 or raw['duration'] <= 0:
        raise ValueError('SENTENCE_WINDOW_US_INVALID')
    return dict(raw)


def normalize_reviews(raw: dict, binding: dict, *, source_hashes: set[str] | None = None,
                      resolver=None, script_text: str | None = None) -> list[dict]:
    if any(raw.get(key) != value for key, value in binding.items()):
        raise ValueError('SENTENCE_REVIEW_BINDING_MISMATCH')
    reviews = raw.get('sentence_reviews')
    if not isinstance(reviews, list):
        raise ValueError('SENTENCE_REVIEW_LIST_REQUIRED')
    normalized, indices = [], set()
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError('SENTENCE_REVIEW_OBJECT_REQUIRED')
        index = review.get('index')
        if type(index) is not int or index < 1 or index in indices:
            raise ValueError('SENTENCE_REVIEW_INDEX_INVALID_OR_DUPLICATE')
        indices.add(index)
        text = review.get('text')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('SENTENCE_TEXT_REQUIRED')
        context = review.get('context', {})
        if not isinstance(context, dict) or any(not isinstance(context.get(key, ''), str)
                                                for key in ('before', 'after')):
            raise ValueError('SENTENCE_CONTEXT_INVALID')
        if script_text is not None:
            position = script_text.find(text)
            if position < 0:
                raise ValueError('SENTENCE_TEXT_NOT_IN_BOUND_TASK')
            before, after = context.get('before', ''), context.get('after', '')
            if before and script_text.rfind(before, 0, position) < 0:
                raise ValueError('SENTENCE_CONTEXT_BEFORE_MISMATCH')
            if after and script_text.find(after, position + len(text)) < 0:
                raise ValueError('SENTENCE_CONTEXT_AFTER_MISMATCH')
        label_status = review.get('label_status', 'unknown')
        if label_status not in {'reviewed', 'unknown'}:
            raise ValueError('SENTENCE_LABEL_STATUS_INVALID')
        split = review.get('split')
        if split not in {'debug', 'holdout'}:
            raise ValueError('SENTENCE_SPLIT_REQUIRED')
        reason = review.get('reason_code')
        if reason not in REASONS:
            raise ValueError('SENTENCE_REASON_CODE_REQUIRED')
        reviewer = str(review.get('reviewed_by') or '').strip()
        refs = evidence_refs(review.get('evidence', []), required=label_status == 'reviewed', resolver=resolver)
        if label_status == 'reviewed' and not reviewer:
            raise ValueError('SENTENCE_HUMAN_REVIEWER_REQUIRED')
        sets = {}
        for name in ('accepted', 'rejected'):
            values = review.get(name, [])
            if not isinstance(values, list):
                raise ValueError('SENTENCE_CANDIDATE_SET_INVALID')
            sets[name] = []
            for candidate in values:
                if not isinstance(candidate, dict) or not str(candidate.get('shot_id') or '').strip():
                    raise ValueError('SENTENCE_SHOT_ID_REQUIRED')
                sha = _sha(candidate.get('source_sha256'))
                if source_hashes is not None and sha not in source_hashes:
                    raise ValueError('SENTENCE_SOURCE_OUTSIDE_CONTRACT')
                sets[name].append({'shot_id': str(candidate['shot_id']), 'source_sha256': sha,
                                   'source_window_us': _window(candidate.get('source_window_us'))})
        if label_status == 'unknown' and any(sets.values()):
            raise ValueError('UNKNOWN_LABEL_CANNOT_ASSERT_CANDIDATES')
        for accepted in sets['accepted']:
            for rejected in sets['rejected']:
                if (accepted['shot_id'], accepted['source_sha256']) == (rejected['shot_id'], rejected['source_sha256']):
                    a, b = accepted['source_window_us'], rejected['source_window_us']
                    if max(a['start'], b['start']) < min(a['start'] + a['duration'], b['start'] + b['duration']):
                        raise ValueError('SENTENCE_ACCEPT_REJECT_WINDOW_CONFLICT')
        row = {'schema_version': 1, 'kind': 'sentence_review', **binding,
               'index': index, 'text': text, 'context': context,
               'original_decision': review.get('original_decision', {}),
               'split': split, 'label_status': label_status, 'reason_code': reason,
               'notes': str(review.get('notes') or ''), 'reviewed_by': reviewer,
               'evidence': refs, **sets}
        row['entry_id'] = pe.digest(row)
        normalized.append(row)
    return normalized


def collect(raw: dict, binding: dict, ledger: Path, *, source_hashes=None, resolver=None,
            script_text: str | None = None) -> dict:
    rows = normalize_reviews(raw, binding, source_hashes=source_hashes, resolver=resolver,
                             script_text=script_text)
    return {'status': 'SENTENCE_REVIEWS_COLLECTED', **append_once(ledger, rows),
            'reviews': rows, 'production_rules_modified': False}


def freeze(contract_path: Path, manifest_path: Path, analysis_path: Path,
           semantic_path: Path, timing_path: Path, output: Path,
           draft_paths: list[Path] | None = None) -> dict:
    contract = pe.read(contract_path)
    pe.verify_contract(contract)
    paths = {'manifest': manifest_path, 'analysis': analysis_path,
             'semantic': semantic_path, 'timing': timing_path}
    bundle = {'schema_version': 1, 'task_id': contract['task_id'], 'run_id': contract['run_id'],
              'task_sha256': contract['task_sha256'],
              'contract_hash': contract['contract_hash'],
              'output_sha256': pe.output_identity(draft_paths)['sha256'] if draft_paths else None,
              'source_identity': contract['inputs'],
              'inputs': {name: pe.read(path) for name, path in paths.items()},
              'input_refs': {name: {'path': str(path.absolute()), 'sha256': pe.file_hash(path)}
                             for name, path in paths.items()},
              'scope': 'deterministic selector only; historical observations and semantic responses fixed'}
    bundle['frozen_sha256'] = pe.digest(bundle)
    pe.write_once(output, bundle)
    return {'status': 'SELECTOR_INPUT_FROZEN', 'path': str(output), 'frozen_sha256': bundle['frozen_sha256']}


def _verify_frozen(bundle: dict) -> None:
    body = dict(bundle)
    supplied = body.pop('frozen_sha256', None)
    if supplied != pe.digest(body):
        raise ValueError('FROZEN_INPUT_HASH_MISMATCH')
    for source in bundle['source_identity']:
        _sha(source['sha256'])
    _sha(bundle.get('task_sha256'))
    _sha(bundle.get('output_sha256'))
    for ref in bundle.get('input_refs', {}).values():
        path = Path(ref['path'])
        if not path.is_file() or pe.file_hash(path) != ref.get('sha256'):
            raise ValueError('FROZEN_INPUT_SOURCE_DRIFT')


def _normalized_path(value: str) -> str:
    return str(value).replace('\\', '/').casefold()


def replay(bundle: dict, skill_root: Path) -> dict:
    """Replay the selected matcher in a guarded child process."""
    _verify_frozen(bundle)
    skill_root = skill_root.resolve()
    package_root = skill_root.parent
    inputs = [package_root / 'VERSION', package_root / 'WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json']
    files = sorted(path for path in (skill_root / 'orchestrator').rglob('*.py') if '__pycache__' not in path.parts)
    files.extend(path for path in inputs if path.is_file())
    if not files:
        raise ValueError('SELECTOR_PACKAGE_MISSING')
    package_hash = pe.digest([{'path': path.relative_to(package_root).as_posix(), 'sha256': pe.file_hash(path)}
                              for path in files])
    with tempfile.TemporaryDirectory(prefix='sentence-replay-') as temporary:
        temporary_path = Path(temporary)
        payload_path = temporary_path / 'input.json'
        output_path = temporary_path / 'result.json'
        pe.write_once(payload_path, {'bundle': bundle, 'skill_root': str(skill_root),
                                    'temporary': str(temporary_path), 'output': str(output_path)})
        worker = r'''import json, os, sys
from pathlib import Path
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
tmp = Path(payload["temporary"]).resolve()
media = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".wav", ".mp3", ".m4a", ".png", ".jpg", ".jpeg"}
def inside(path):
    try: Path(path).resolve().relative_to(tmp); return True
    except (ValueError, OSError, TypeError): return False
def guard(event, args):
    if event == "open":
        path = args[0]
        if isinstance(path, (str, bytes, os.PathLike)):
            path = os.fsdecode(path)
            mode = args[1] if len(args) > 1 else "r"
            flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or bool(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND))
            if writing and not inside(path): raise RuntimeError("REPLAY_WRITE_OUTSIDE_TEMP_BLOCKED")
            if not writing and Path(path).suffix.lower() in media: raise RuntimeError("REPLAY_MEDIA_ACCESS_BLOCKED")
    if event in {"subprocess.Popen", "os.system", "socket.connect", "socket.bind", "socket.sendto"}:
        raise RuntimeError("REPLAY_EXTERNAL_SIDE_EFFECT_BLOCKED:" + event)
    if event in {"os.remove", "os.unlink", "os.mkdir", "os.rmdir", "os.chmod", "os.chown", "os.truncate"} and args and not inside(args[0]):
        raise RuntimeError("REPLAY_WRITE_OUTSIDE_TEMP_BLOCKED")
    if event in {"os.rename", "os.replace"} and any(not inside(x) for x in args[:2]):
        raise RuntimeError("REPLAY_WRITE_OUTSIDE_TEMP_BLOCKED")
sys.addaudithook(guard)
sys.path.insert(0, payload["skill_root"])
from orchestrator import shot_analyzer
bundle = payload["bundle"]
manifest, analysis = bundle["inputs"]["manifest"], bundle["inputs"]["analysis"]
analysis["semantic_matches"] = bundle["inputs"]["semantic"].get("matches", [])
ledger = tmp / "empty-usage.json"
ledger.write_text(json.dumps({"schema":"shot-usage-v1","shots":{},"drafts":{}}), encoding="utf-8")
manifest["usage_ledger_path"] = str(ledger)
result = shot_analyzer.match_manifest(manifest, analysis, topk=int(manifest.get("shot_topk",3) or 3), auto_accept_top1=bool(manifest.get("auto_accept_top1",True)), narration_timing=bundle["inputs"]["timing"])
Path(payload["output"]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
'''
        env = os.environ.copy()
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        # Isolated mode ignores PYTHON* environment variables, so disable bytecode
        # explicitly before importing the matcher under the write guard.
        completed = subprocess.run([sys.executable, '-I', '-B', '-c', worker, str(payload_path)],
                                   cwd=temporary_path, env=env, capture_output=True, text=True, timeout=90)
        if completed.returncode:
            raise ValueError('OFFLINE_REPLAY_FAILED:' + (completed.stderr[-2000:] or str(completed.returncode)))
        result = pe.read(output_path)
    sources = {_normalized_path(row['path']): row['sha256'] for row in bundle['source_identity']}
    available = list(result.get('segments') or [])
    rows = []
    for recommendation in result.get('recommendations') or []:
        text, index = recommendation.get('text'), recommendation['index']
        # Matching repeated sentence text consumes rows in order, as selector does.
        position = next((p for p, segment in enumerate(available) if segment.get('text') == text), None)
        segment = available.pop(position) if position is not None else {}
        pending = [item for item in result.get('pending_items') or [] if item.get('index') == index]
        window = segment.get('window_solution') or {}
        start = round(float(window.get('source_start', segment.get('source_start', 0))) * 1_000_000)
        end = round(float(window.get('source_end', segment.get('source_end', 0))) * 1_000_000)
        duration = max(0, end - start)
        rows.append({'index': index, 'text': text, 'shot_id': segment.get('temporary_shot_id'),
                     'source_sha256': sources.get(_normalized_path(segment.get('video', ''))),
                     'source_window_us': {'start': int(start), 'duration': int(duration)},
                     'blocked': bool(pending or segment.get('formal_rejected') or not segment or segment.get('visual_missing')),
                     'pending': pending, 'multi_shot': bool(segment.get('multi_shot'))})
    receipt = {'schema_version': 1, 'task_id': bundle['task_id'], 'run_id': bundle['run_id'],
            'contract_hash': bundle['contract_hash'], 'task_sha256': bundle['task_sha256'],
            'output_sha256': bundle['output_sha256'], 'frozen_sha256': bundle['frozen_sha256'],
            'package_sha256': package_hash, 'rows': rows,
            'scope': bundle['scope'], 'scene_planning_applied': False, 'empty_usage_ledger': True}
    receipt['result_sha256'] = pe.digest(receipt)
    return receipt


def _matches(choice: dict, allowed: dict, *, reject=False) -> bool:
    if choice.get('multi_shot') or any(choice.get(key) != allowed.get(key) for key in ('shot_id', 'source_sha256')):
        return False
    a, b = choice['source_window_us'], allowed['source_window_us']
    if reject:
        return max(a['start'], b['start']) < min(a['start'] + a['duration'], b['start'] + b['duration'])
    return a['start'] >= b['start'] and a['start'] + a['duration'] <= b['start'] + b['duration']


def evaluate(labels: list[dict], result: dict) -> dict:
    choices = {row['index']: row for row in result['rows']}
    groups = {split: {'sentences': 0, 'unknown_label': 0, 'unknown_choice': 0,
                      'top1_accepted': 0, 'top1_rejected': 0, 'blocked_with_acceptable': 0,
                      'has_acceptable': 0} for split in ('debug', 'holdout')}
    details, seen = [], set()
    for label in labels:
        if (label.get('task_id'), label.get('run_id')) != (result['task_id'], result['run_id']):
            continue
        if any(label.get(key) != result.get(key) for key in
               ('contract_hash', 'task_sha256', 'output_sha256')):
            raise ValueError('EVALUATION_IDENTITY_DRIFT')
        if label['index'] in seen:
            raise ValueError('MULTIPLE_SENTENCE_LABELS_REQUIRE_EXPLICIT_ADJUDICATION')
        seen.add(label['index'])
        count = groups[label['split']]
        count['sentences'] += 1
        choice = choices.get(label['index'])
        if choice is not None and choice.get('text') != label['text']:
            raise ValueError('EVALUATION_SENTENCE_TEXT_DRIFT')
        if label['label_status'] != 'reviewed':
            verdict = 'unknown_label'
        else:
            has_acceptable = bool(label['accepted'])
            count['has_acceptable'] += int(has_acceptable)
            if not choice or choice.get('blocked'):
                verdict = 'blocked_with_acceptable' if has_acceptable else 'unknown_choice'
            elif any(_matches(choice, item) for item in label['accepted']):
                verdict = 'top1_accepted'
            elif any(_matches(choice, item, reject=True) for item in label['rejected']):
                verdict = 'top1_rejected'
            else:
                verdict = 'unknown_choice'
        count[verdict] += 1
        details.append({'index': label['index'], 'split': label['split'], 'verdict': verdict,
                        'review_entry_id': label['entry_id'], 'choice': choice})
    for count in groups.values():
        denominator = count['top1_accepted'] + count['top1_rejected'] + count['blocked_with_acceptable']
        count['top1_acceptance'] = {'numerator': count['top1_accepted'], 'denominator': denominator,
                                   'rate': count['top1_accepted'] / denominator if denominator else None}
        denominator = count['has_acceptable']
        count['false_block'] = {'numerator': count['blocked_with_acceptable'], 'denominator': denominator,
                               'rate': count['blocked_with_acceptable'] / denominator if denominator else None}
    return {'task_id': result['task_id'], 'run_id': result['run_id'], 'groups': groups, 'sentences': details,
            'unlabeled_result_rows': sorted(set(choices) - seen),
            'unlabeled_result_count': len(set(choices) - seen),
            'metric_scope': 'human-labeled single-window choices only; unknown excluded and counted separately'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    collect_parser = sub.add_parser('collect')
    collect_parser.add_argument('--input', type=Path, required=True)
    collect_parser.add_argument('--contract', type=Path, required=True)
    collect_parser.add_argument('--task', type=Path, required=True,
                                help='actual task JSON whose digest is frozen in the contract')
    collect_parser.add_argument('--ledger', type=Path, required=True)
    collect_parser.add_argument('--draft', type=Path, action='append', required=True,
                                help='actual output draft directories, repeated for multi-draft output')
    frozen = sub.add_parser('freeze')
    for name in ('contract', 'manifest', 'analysis', 'semantic', 'timing', 'output'):
        frozen.add_argument('--' + name, type=Path, required=True)
    frozen.add_argument('--draft', type=Path, action='append', required=True,
                        help='actual output draft directory, repeated for multi-draft output')
    replay_parser = sub.add_parser('replay')
    for name in ('frozen', 'skill-root', 'output'):
        replay_parser.add_argument('--' + name, type=Path, required=True)
    compare = sub.add_parser('compare')
    for name in ('ledger', 'baseline', 'current', 'output'):
        compare.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.command == 'collect':
        contract = pe.read(args.contract)
        pe.verify_contract(contract)
        task = pe.read(args.task)
        if pe.digest(task) != contract['task_sha256']:
            raise ValueError('SENTENCE_TASK_CONTENT_DRIFT')
        raw = pe.read(args.input)
        binding = {key: contract[key] for key in ('task_id', 'run_id', 'contract_hash', 'task_sha256')}
        binding['output_sha256'] = pe.output_identity(args.draft)['sha256']
        result = collect(raw, binding, args.ledger,
                         source_hashes={row['sha256'] for row in contract['inputs']},
                         script_text=str(task.get('script') or ''))
    elif args.command == 'freeze':
        result = freeze(args.contract, args.manifest, args.analysis, args.semantic,
                        args.timing, args.output, draft_paths=args.draft)
    elif args.command == 'replay':
        result = replay(pe.read(args.frozen), args.skill_root)
        pe.write_once(args.output, result)
    else:
        baseline, current = pe.read(args.baseline), pe.read(args.current)
        for receipt in (baseline, current):
            body = dict(receipt)
            supplied = body.pop('result_sha256', None)
            if supplied != pe.digest(body):
                raise ValueError('SELECTOR_RESULT_HASH_MISMATCH')
        if baseline.get('frozen_sha256') != current.get('frozen_sha256'):
            raise ValueError('COMPARISON_REQUIRES_IDENTICAL_FROZEN_INPUTS')
        labels = read_rows(args.ledger)
        result = {'baseline': evaluate(labels, baseline), 'current': evaluate(labels, current),
                  'frozen_sha256': current['frozen_sha256'],
                  'evidence': {name: {'path': str(path.absolute()), 'sha256': pe.file_hash(path)}
                               for name, path in [('labels', args.ledger), ('baseline', args.baseline),
                                                  ('current', args.current)]}}
        pe.write_once(args.output, result)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
