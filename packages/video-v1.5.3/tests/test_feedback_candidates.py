import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'doubao-jianying-orchestrator'))
from orchestrator import feedback_policy
from tools import feedback_candidates as fc, production_eval as pe


class FeedbackCandidateTests(unittest.TestCase):
    def test_opposite_subtitle_feedback_is_not_an_approved_rule(self):
        yes = feedback_policy.classify_feedback('以后字幕都加粗')
        no = feedback_policy.classify_feedback('以后字幕不要加粗')
        self.assertEqual(yes['directions']['subtitle_weight'], 'requested')
        self.assertEqual(no['directions']['subtitle_weight'], 'opposed')
        for row in (yes, no):
            self.assertEqual(row['proposal_status'], 'needs_review')
            self.assertEqual(row['rule_text'], '')
            self.assertIn('"font_weight": "bold"', row['category_guidance'])
            self.assertNotIn('非粗体', row['category_guidance'])

    def test_continuous_experiment_and_meaningless_repeat_are_distinct(self):
        row = feedback_policy.classify_feedback('允许同源连续实验，但不要重复镜头')
        self.assertEqual(row['category'], 'duplicate_shots')
        self.assertEqual(row['directions']['same_source_continuity'], 'requested')
        self.assertEqual(row['directions']['meaningless_repeat'], 'opposed')
        self.assertEqual(feedback_policy.classify_feedback('不允许同源')['directions']['same_source_continuity'], 'opposed')

    def test_conflict_and_raw_original_are_preserved(self):
        text = '以后字幕加粗。\n但字幕不要加粗。'
        row = feedback_policy.classify_feedback(text)
        self.assertEqual(row['original_text'], text)
        self.assertIn('subtitle_weight', row['conflicts'])
        self.assertEqual(row['proposal_status'], 'needs_review')

    def test_fluorescence_and_playback_have_searchable_categories(self):
        self.assertEqual(feedback_policy.classify_feedback('荧光没有选到')['category'], 'audio_visual_match')
        self.assertEqual(feedback_policy.classify_feedback('别用50倍速')['category'], 'playback_rhythm')

    def _task(self, root):
        task_dir = root / 'private-task'
        task_dir.mkdir()
        source = root / 'source.mp4'
        source.write_bytes(b'synthetic-source')
        task = {'task_id': 'task-fixture', 'record_id': 'record-fixture',
                'script': '合成文案', 'issue': '以后字幕加粗'}
        pe.write_once(task_dir / 'task.json', task)
        runtime = {'tool_version': '1.5.6', 'visual_policy_version': 'fixture',
                   'package_sha256': 'a' * 64, 'visual_policy_sha256': 'b' * 64}
        contract = pe.freeze(task, 'run-fixture', [source], runtime, pe.read(ROOT / 'gold-reference.json'))
        pe.write_once(task_dir / 'eval' / 'active_contract.json', contract)
        proof = root / 'repair.txt'
        proof.write_text('synthetic repair evidence')
        resolution = {'task_id': task['task_id'], 'run_id': contract['run_id'],
                      'issue_sha256': hashlib.sha256(task['issue'].encode()).hexdigest(),
                      'status': 'resolved', 'approved': True,
                      'proposed_rule': '待审核的建议', 'reviewed_by': 'agent',
                      'evidence': [{'path': str(proof), 'sha256': pe.file_hash(proof)}]}
        pe.write_once(task_dir / 'feedback_resolution.json', resolution)
        return task_dir, contract

    def test_actual_collection_is_append_only_idempotent_and_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_dir, _ = self._task(root)
            ledger = root / 'private-ledger.jsonl'
            first = fc.collect(task_dir, ledger)
            before = ledger.read_bytes()
            self.assertEqual(first['appended'], 1)
            self.assertEqual(fc.collect(task_dir, ledger)['appended'], 0)
            self.assertEqual(ledger.read_bytes(), before)
            row = fc.read_rows(ledger)[0]
            self.assertEqual(row['review_status'], 'needs_review')
            self.assertFalse(row['approved'])
            self.assertEqual(row['reviewed_by'], '')
            self.assertEqual(row['issue'], '以后字幕加粗')

    def test_review_regression_release_are_explicit_append_only_transitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_dir, _ = self._task(root)
            ledger = root / 'private-ledger.jsonl'
            fc.collect(task_dir, ledger)
            candidate = next(row for row in fc.read_rows(ledger) if row['kind'] == 'feedback_candidate')
            evidence = root / 'approval.txt'
            evidence.write_text('human review proof')
            refs = [{'path': str(evidence), 'sha256': pe.file_hash(evidence)}]
            with self.assertRaisesRegex(ValueError, 'PREREQUISITE'):
                fc.transition(ledger, candidate['entry_id'], 'regression_verified', 'reviewer', refs,
                              regression_case='case-1')
            fc.transition(ledger, candidate['entry_id'], 'reviewed', 'reviewer', refs)
            self.assertEqual(fc.candidate_state(fc.read_rows(ledger), candidate['entry_id'])['status'], 'reviewed')
            fc.transition(ledger, candidate['entry_id'], 'regression_verified', 'reviewer', refs,
                          regression_case='case-1')
            with self.assertRaisesRegex(ValueError, 'RELEASE_REFERENCE'):
                fc.transition(ledger, candidate['entry_id'], 'released', 'reviewer', refs)
            fc.transition(ledger, candidate['entry_id'], 'released', 'release-owner', refs,
                          release='1.5.7')
            self.assertEqual(fc.candidate_state(fc.read_rows(ledger), candidate['entry_id'])['status'], 'released')
            self.assertEqual(len(fc.read_rows(ledger)), 4)

    def test_stale_resolution_or_modified_proof_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_dir, _ = self._task(root)
            path = task_dir / 'feedback_resolution.json'
            raw = pe.read(path)
            raw['run_id'] = 'old-run'
            path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, 'BINDING_MISMATCH'):
                fc.collect(task_dir, root / 'ledger.jsonl')
            raw['run_id'] = 'run-fixture'
            path.write_text(json.dumps(raw))
            Path(raw['evidence'][0]['path']).write_text('changed')
            with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
                fc.collect(task_dir, root / 'ledger.jsonl')

    def test_ledger_corruption_or_lock_cannot_be_silently_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / 'ledger.jsonl'
            fc.append_once(ledger, [{'entry_id': 'fixture', 'review_status': 'needs_review'}])
            data = ledger.read_text().replace('needs_review', 'released')
            ledger.write_text(data)
            with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
                fc.append_once(ledger, [])
            ledger.with_name(ledger.name + '.lock').write_text('busy')
            with self.assertRaisesRegex(ValueError, 'LEDGER_BUSY'):
                fc.append_once(ledger, [])


if __name__ == '__main__':
    unittest.main()
