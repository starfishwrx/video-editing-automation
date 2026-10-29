import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'doubao-jianying-orchestrator'))
from tools import eval_stage, feedback_candidates as fc, production_eval as pe, sentence_review as sr
import batch_worker
from orchestrator.task_inputs import normalize_meeting_task


class SentenceReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.task_dir = self.root / 'private-task'
        self.source = self.root / 'fixture.mp4'
        self.source.write_bytes(b'synthetic-source')
        self.source_sha = pe.file_hash(self.source)
        self.task = {'task_id': 'task-fixture', 'record_id': 'fixture', 'script': '推荐一款宝藏好物'}
        pe.write_once(self.task_dir / 'task.json', self.task)
        runtime = {'tool_version': '1.5.6', 'visual_policy_version': 'fixture',
                   'package_sha256': 'a' * 64, 'visual_policy_sha256': 'b' * 64}
        self.contract = pe.freeze(self.task, 'run-fixture', [self.source], runtime,
                                  pe.read(ROOT / 'gold-reference.json'))
        pe.write_once(self.task_dir / 'eval' / 'active_contract.json', self.contract)
        self.drafts = self.root / 'drafts'
        pe.write_once(self.drafts / 'draft-fixture' / 'draft_info.json', {'id': 'draft-fixture'})
        pe.write_once(self.task_dir / 'done.json', {'drafts': ['draft-fixture'], 'status': 'PREVIEW_READY'})
        self.identity = pe.output_identity([self.drafts / 'draft-fixture'])
        self.binding = {key: self.contract[key] for key in ('task_id', 'run_id', 'contract_hash', 'task_sha256')}
        self.binding['output_sha256'] = self.identity['sha256']
        self.proof = self.root / 'human-review.txt'
        self.proof.write_text('synthetic human decision and source window')
        self.candidate = {'shot_id': 'fixture-shot', 'source_sha256': self.source_sha,
                          'source_window_us': {'start': 0, 'duration': 12_000_000}}
        self.review = {'index': 1, 'text': self.task['script'], 'context': {'before': '', 'after': ''},
                       'split': 'debug', 'label_status': 'reviewed', 'reason_code': 'acceptable',
                       'reviewed_by': 'synthetic-reviewer', 'notes': 'synthetic fixture only',
                       'evidence': [{'path': str(self.proof), 'sha256': pe.file_hash(self.proof)}],
                       'accepted': [self.candidate], 'rejected': []}

    def tearDown(self):
        self.temporary.cleanup()

    def _raw(self, reviews=None):
        return {**self.binding, 'sentence_reviews': reviews if reviews is not None else [self.review]}

    def test_windows_mode_forces_local_handoff_even_when_ship_requested(self):
        env = dict(os.environ, JY_REVIEW_SURFACE='windows_manual', JY_SKIP_SHIP='0', JY_MAC_REVIEW_REQUIRED='1')
        code = 'import json,batch_worker as w;print(json.dumps([w.SKIP_SHIP,w.MAC_REVIEW_REQUIRED,w.WINDOWS_MANUAL_REVIEW]))'
        run = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout), [True, False, True])
        env['JY_REVIEW_SURFACE'] = 'mac'
        run = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                             capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout), [False, True, False])

    def test_windows_handoff_is_hash_bound_and_does_not_approve_or_ship(self):
        with mock.patch.object(batch_worker, 'DRAFTS', self.drafts), \
                mock.patch.object(batch_worker, 'WINDOWS_MANUAL_REVIEW', True), \
                mock.patch.object(batch_worker, 'ship_one') as ship:
            path = batch_worker.prepare_windows_review(self.task_dir, self.task, ['draft-fixture'], [])
            self.assertEqual(path, batch_worker.prepare_windows_review(self.task_dir, self.task, ['draft-fixture'], []))
            package = pe.read(path)
            self.assertEqual(package['status'], 'AWAITING_WINDOWS_REVIEW')
            self.assertFalse(package['approved'])
            self.assertEqual(package['output_sha256'], self.identity['sha256'])
            self.assertEqual(package['contract_hash'], self.contract['contract_hash'])
            self.assertEqual(batch_worker.finalize_closed_loop(self.task_dir, self.task, ['draft-fixture'], [])['reason'],
                             'WINDOWS_MANUAL_REVIEW_PENDING')
            ship.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'WINDOWS_REVIEW_TASK_MISMATCH'):
                batch_worker.prepare_windows_review(self.task_dir, {**self.task, 'script': '改动'}, ['draft-fixture'], [])
            with self.assertRaisesRegex(ValueError, 'WINDOWS_REVIEW_TASK_MISMATCH'):
                batch_worker.prepare_windows_review(self.task_dir, self.task, ['../draft-fixture'], [])
            (self.task_dir / 'eval' / 'active_contract.json').write_text(
                json.dumps({**self.contract, 'run_id': 'tampered'}), encoding='utf-8')
            with self.assertRaises(ValueError):
                batch_worker.prepare_windows_review(self.task_dir, self.task, ['draft-fixture'], [])

    def test_normalized_frozen_task_snapshot_prepares_real_review_input(self):
        task = normalize_meeting_task({**self.task, 'retry_deferred': True, 'retry_error': True})
        contract = pe.freeze(task, 'run-normalized', [self.source], self.contract['runtime'],
                             pe.read(ROOT / 'gold-reference.json'))
        self.assertNotEqual(pe.digest(self.task), contract['task_sha256'])
        batch_worker._save_frozen_task(self.task_dir, task, contract)
        (self.task_dir / 'eval' / 'active_contract.json').write_text(json.dumps(contract), encoding='utf-8')
        prepared = eval_stage.prepare(self.task_dir, self.drafts)
        self.assertEqual(prepared['status'], 'REVIEW_INPUTS_READY')
        self.assertFalse(pe.read(Path(prepared['mac_review_input']))['approved'])
        with self.assertRaisesRegex(ValueError, 'TASK_CONTRACT_CONTENT_MISMATCH'):
            batch_worker._save_frozen_task(self.task_dir, {**task, 'script': 'changed'}, contract)
        self.assertEqual(pe.digest(pe.read(self.task_dir / 'task.json')), contract['task_sha256'])

    def test_record_id_fallback_matches_frozen_identity(self):
        task = normalize_meeting_task({'record_id': 'fixture', 'script': '口播'})
        contract = pe.freeze(task, 'run-fallback', [self.source], self.contract['runtime'],
                             pe.read(ROOT / 'gold-reference.json'))
        batch_worker._save_frozen_task(self.task_dir, task, contract)
        (self.task_dir / 'eval' / 'active_contract.json').write_text(json.dumps(contract), encoding='utf-8')
        self.assertEqual(eval_stage.prepare(self.task_dir, self.drafts)['status'], 'REVIEW_INPUTS_READY')

    def test_eval_collect_preserves_labels_without_approving_gold_or_mac(self):
        prepared = eval_stage.prepare(self.task_dir, self.drafts)
        gold_path, mac_path = Path(prepared['gold_review_input']), Path(prepared['mac_review_input'])
        gold, mac = pe.read(gold_path), pe.read(mac_path)
        gold.update(reviewed_by='synthetic-reviewer', sentence_reviews=[self.review])
        mac['reviewed_by'] = 'synthetic-reviewer'
        gold_path.write_text(json.dumps(gold))
        mac_path.write_text(json.dumps(mac))
        ledger = self.root / 'private-labels.jsonl'
        result = eval_stage.collect(self.task_dir, self.drafts, sentence_ledger=ledger)
        self.assertEqual(result['gold_status'], 'UNCERTIFIED')
        self.assertEqual(result['mac_status'], 'UNCERTIFIED')
        self.assertIsNone(result['approval_file'])
        self.assertEqual(result['sentence_review_collection']['appended'], 1)
        receipt = pe.read(Path(result['gold_receipt']))
        self.assertEqual(receipt['sentence_reviews'][0]['accepted'], [self.candidate])
        self.assertEqual(fc.read_rows(ledger)[0]['run_id'], 'run-fixture')
        self.assertEqual(eval_stage.collect(self.task_dir, self.drafts,
                                           sentence_ledger=ledger)['sentence_review_collection']['appended'], 0)

    def test_opposite_windows_multiple_allowed_and_unknown_are_explicit(self):
        review = copy.deepcopy(self.review)
        review['accepted'].append({**self.candidate, 'shot_id': 'second-valid-shot'})
        result = sr.collect(self._raw([review]), self.binding, self.root / 'labels.jsonl',
                            source_hashes={self.source_sha})
        self.assertEqual(len(result['reviews'][0]['accepted']), 2)
        review['rejected'] = [self.candidate]
        with self.assertRaisesRegex(ValueError, 'WINDOW_CONFLICT'):
            sr.normalize_reviews(self._raw([review]), self.binding)
        review = {**self.review, 'label_status': 'unknown'}
        with self.assertRaisesRegex(ValueError, 'UNKNOWN_LABEL'):
            sr.normalize_reviews(self._raw([review]), self.binding)

    def test_standalone_collect_binds_actual_task_script_and_current_output(self):
        raw_path, ledger = self.root / 'review.json', self.root / 'cli-labels.jsonl'
        raw_path.write_text(json.dumps(self._raw()))
        argv = ['sentence_review', 'collect', '--input', str(raw_path),
                '--contract', str(self.task_dir / 'eval' / 'active_contract.json'),
                '--task', str(self.task_dir / 'task.json'),
                '--draft', str(self.drafts / 'draft-fixture'), '--ledger', str(ledger)]
        with mock.patch.object(sys, 'argv', argv), mock.patch('builtins.print'):
            self.assertEqual(sr.main(), 0)
            self.assertEqual(fc.read_rows(ledger)[0]['task_sha256'], self.contract['task_sha256'])
            raw = self._raw()
            raw.pop('task_sha256')
            raw_path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, 'BINDING_MISMATCH'):
                sr.main()
            raw = copy.deepcopy(self._raw())
            raw['sentence_reviews'][0]['text'] = 'unrelated script'
            raw_path.write_text(json.dumps(raw))
            with self.assertRaisesRegex(ValueError, 'TEXT_NOT_IN_BOUND_TASK'):
                sr.main()
            raw_path.write_text(json.dumps(self._raw()))
            draft = self.drafts / 'draft-fixture' / 'draft_info.json'
            original = draft.read_bytes()
            draft.write_text('{"id":"changed"}')
            with self.assertRaisesRegex(ValueError, 'BINDING_MISMATCH'):
                sr.main()
            draft.write_bytes(original)
            (self.task_dir / 'task.json').write_text(json.dumps({**self.task, 'script': 'changed'}))
            with self.assertRaisesRegex(ValueError, 'TASK_CONTENT_DRIFT'):
                sr.main()
        self.assertEqual(len(fc.read_rows(ledger)), 1)

    def test_binding_source_hash_window_and_review_evidence_fail_closed(self):
        changed = self._raw()
        changed['run_id'] = 'other-run'
        with self.assertRaisesRegex(ValueError, 'BINDING_MISMATCH'):
            sr.normalize_reviews(changed, self.binding)
        with self.assertRaisesRegex(ValueError, 'OUTSIDE_CONTRACT'):
            sr.normalize_reviews(self._raw(), self.binding, source_hashes={'f' * 64})
        self.review['accepted'][0]['source_window_us']['duration'] = -1
        with self.assertRaisesRegex(ValueError, 'WINDOW_US_INVALID'):
            sr.normalize_reviews(self._raw(), self.binding)
        self.review['accepted'][0]['source_window_us']['duration'] = 12_000_000
        self.proof.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'HASH_MISMATCH'):
            sr.normalize_reviews(self._raw(), self.binding)

    def test_metrics_count_unknown_and_separate_holdout_from_debug(self):
        review = copy.deepcopy(self.review)
        review['rejected'] = [{**self.candidate, 'shot_id': 'wrong-shot'}]
        labels = sr.normalize_reviews(self._raw([review]), self.binding)
        result = {'task_id': self.contract['task_id'], 'run_id': self.contract['run_id'],
                  'contract_hash': self.contract['contract_hash'],
                  'task_sha256': self.contract['task_sha256'], 'output_sha256': self.identity['sha256'], 'rows': [
                      {'index': 1, 'text': self.task['script'], **self.candidate, 'blocked': False}]}
        accepted = sr.evaluate(labels, result)['groups']['debug']
        self.assertEqual(accepted['top1_acceptance'], {'numerator': 1, 'denominator': 1, 'rate': 1.0})
        result['rows'][0]['shot_id'] = 'wrong-shot'
        self.assertEqual(sr.evaluate(labels, result)['groups']['debug']['top1_rejected'], 1)
        result['rows'][0]['shot_id'] = 'unreviewed-shot'
        unknown = sr.evaluate(labels, result)['groups']['debug']
        self.assertEqual(unknown['unknown_choice'], 1)
        self.assertIsNone(unknown['top1_acceptance']['rate'])
        result['rows'][0]['blocked'] = True
        blocked = sr.evaluate(labels, result)['groups']['debug']
        self.assertEqual(blocked['false_block'], {'numerator': 1, 'denominator': 1, 'rate': 1.0})
        unknown_review = {**self.review, 'index': 2, 'split': 'holdout', 'label_status': 'unknown',
                          'accepted': [], 'rejected': [], 'reviewed_by': '', 'evidence': []}
        labels += sr.normalize_reviews(self._raw([unknown_review]), self.binding)
        grouped = sr.evaluate(labels, result)['groups']
        self.assertEqual(grouped['holdout']['unknown_label'], 1)
        self.assertIsNone(grouped['holdout']['top1_acceptance']['rate'])

    def test_source_window_drift_and_multiple_labels_are_not_scored_as_acceptance(self):
        labels = sr.normalize_reviews(self._raw(), self.binding)
        result = {'task_id': self.contract['task_id'], 'run_id': self.contract['run_id'],
                  'contract_hash': self.contract['contract_hash'],
                  'task_sha256': self.contract['task_sha256'], 'output_sha256': self.identity['sha256'], 'rows': [
                      {'index': 1, 'text': self.task['script'], **self.candidate,
                       'source_window_us': {'start': 11_000_000, 'duration': 2_000_000}, 'blocked': False}]}
        self.assertEqual(sr.evaluate(labels, result)['groups']['debug']['unknown_choice'], 1)
        with self.assertRaisesRegex(ValueError, 'EXPLICIT_ADJUDICATION'):
            sr.evaluate(labels + labels, result)

    def test_frozen_replay_calls_real_matcher_using_actual_solved_window(self):
        source = str(self.source)
        manifest = {'task_id': self.task['task_id'], 'category': '纸品', 'product': '纯水湿厕纸',
                    'delivery_mode': 'formal', 'head_trim_s': 0, 'tail_trim_s': 0, 'tail_pad_s': 0,
                    'fps': 30, 'segments': [{'text': self.task['script'],
                                            'audio_duration_us': 2_000_000, 'timing_certainty': 'audio'}]}
        analysis = {'shots': [{'shot_id': 'fixture-shot', 'video': source,
            'source_start': 0, 'source_end': 12, 'duration': 12, 'material_duration_us': 12_000_000,
            'status': 'ready_for_matching', 'action_complete': True, 'has_subject': True,
            'role': 'product_display', 'evidence_strength': 'B', 'action_phase': 'static',
            'head_waste': 0, 'tail_waste': 0, 'action': '举起', 'object': '产品包装',
            'description': '手持产品入画展示', 'visual_tags': ['包装展示'], 'evidence_tags': ['包装展示'],
            'evidence_intervals': [{'start': 4, 'end': 6, 'frame_time': 5}]}]}
        semantic = {'matches': [{'index': 1, 'top3': [{'shot_id': 'fixture-shot', 'score': 99,
                                                       'matched_claims': ['手持产品展示']}]}]}
        paths = []
        for name, value in [('manifest', manifest), ('analysis', analysis), ('semantic', semantic), ('timing', {})]:
            path = self.root / (name + '.json')
            pe.write_once(path, value)
            paths.append(path)
        frozen_path = self.root / 'frozen.json'
        sr.freeze(self.task_dir / 'eval' / 'active_contract.json', *paths, frozen_path,
                  draft_paths=[self.drafts / 'draft-fixture'])
        bundle = pe.read(frozen_path)
        result = sr.replay(bundle, ROOT / 'doubao-jianying-orchestrator')
        self.assertEqual(result['rows'][0]['shot_id'], 'fixture-shot')
        self.assertEqual(result['rows'][0]['source_sha256'], self.source_sha)
        self.assertFalse(result['rows'][0]['blocked'])
        self.assertEqual(result['rows'][0]['source_window_us']['duration'], 2_000_000)
        self.assertEqual(result['task_sha256'], self.contract['task_sha256'])
        self.assertEqual(result['output_sha256'], self.identity['sha256'])
        skill_copy = self.root / 'skill-copy'
        skill_copy.mkdir()
        shutil.copytree(ROOT / 'doubao-jianying-orchestrator' / 'orchestrator',
                        skill_copy / 'orchestrator', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(ROOT / 'VERSION', skill_copy / 'VERSION')
        shutil.copy2(ROOT / 'WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json',
                     skill_copy / 'WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json')
        (skill_copy / 'VERSION').write_text('changed-version')
        changed_package = sr.replay(bundle, skill_copy)
        self.assertFalse((skill_copy / 'orchestrator' / '__pycache__').exists())
        self.assertNotEqual(result['package_sha256'], changed_package['package_sha256'])
        labels = sr.normalize_reviews(self._raw(), self.binding)
        self.assertEqual(sr.evaluate(labels, result)['groups']['debug']['top1_accepted'], 1)
        paths[-1].write_text('{} ')
        with self.assertRaisesRegex(ValueError, 'FROZEN_INPUT_SOURCE_DRIFT'):
            sr.replay(bundle, ROOT / 'doubao-jianying-orchestrator')
        bundle['inputs']['manifest']['segments'][0]['text'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'FROZEN_INPUT_HASH_MISMATCH'):
            sr.replay(bundle, ROOT / 'doubao-jianying-orchestrator')


if __name__ == '__main__':
    unittest.main()
