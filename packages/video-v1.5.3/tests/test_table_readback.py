"""Successful HTTP/CLI acknowledgement is not a delivery receipt."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.workflow import closed_loop as m


class TableReadbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.task = self.root / 'task.json'
        self.done = self.root / 'done.json'
        self.policy = self.root / 'policy.json'
        self.write(self.task, {'record_id': 'rec-test'})
        self.write(self.done, {'drafts': ['fixture'], 'nas_parent_folder': r'\\server\share\draft'})
        self.write(self.policy, {'policy_id': 'fixture', 'table_write': {'initial_success_status': 'custom-first',
                                               'revision_status': 'custom-revised'}})
        for p in (patch.dict(os.environ, {'LARK_BASE_TOKEN': 'secret-token', 'LARK_TABLE_ID': 'tbl-test',
                                          'LARK_ACTOR': 'user'}),
                  patch.object(m, 'audit', return_value={'eligible': True}),
                  patch.object(m, 'lark_cli_executable', return_value='fake-lark'),
                  patch('tools.native_drafts.bgm_title', return_value='Track'),
                  patch.object(m.time, 'sleep')):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def write(path, obj):
        path.write_text(json.dumps(obj), encoding='utf-8')

    @staticmethod
    def response(value, rc=0):
        return subprocess.CompletedProcess([], rc, json.dumps(value), '')

    def cells(self, **changes):
        fields = {'草稿文件': r'\\server\share\draft', '任务状态': ['已完成'],
                  '成片状态': ['custom-first'], 'BGM名称': 'Track'}
        fields.update(changes)
        return {'ok': True, 'data': {'record_id_list': ['rec-test'], 'fields': list(fields),
                                    'data': [list(fields.values())], 'has_more': False}}

    def run_update(self, outputs):
        with patch.object(m.subprocess, 'run', side_effect=outputs) as run:
            result = m.table_update(self.task, self.done, self.policy)
        return result, run

    def test_full_success_verifies_all_requested_fields(self):
        result, run = self.run_update([self.response({'ok': True}), self.response(self.cells())])
        self.assertTrue(result['ok'])
        self.assertTrue(result['readback_verified'])
        self.assertEqual(run.call_count, 2)
        self.assertIn('+record-get', run.call_args.args[0])
        self.assertEqual(result['patch']['成片状态'], ['custom-first'])

    def test_ack_with_omitted_bgm_is_not_success(self):
        cells = self.cells()
        cells['data']['fields'].pop()
        cells['data']['data'][0].pop()
        result, _ = self.run_update([self.response({'ok': True})] + [self.response(cells)] * 3)
        self.assertFalse(result['ok'])
        self.assertEqual(result['mismatched_fields'], ['BGM名称'])
        self.assertTrue(result['write_acknowledged'])

    def test_wrong_status_is_not_success(self):
        result, _ = self.run_update([self.response({'ok': True})] +
                                   [self.response(self.cells(**{'成片状态': ['wrong']}))] * 3)
        self.assertEqual(result['status'], 'TABLE_READBACK_MISMATCH')
        self.assertFalse(result['ok'])

    def test_failed_readback_resumes_only_read_without_rewriting(self):
        result, _ = self.run_update([self.response({'ok': True})] +
                                   [subprocess.TimeoutExpired(['secret-token'], 20)] * 3)
        self.assertEqual(result['status'], 'TABLE_READBACK_FAILED')
        self.assertEqual(result['resume_stage'], 'qc')
        self.assertEqual(result['phase'], 'delivery')
        result, run = self.run_update([self.response(self.cells())])
        self.assertTrue(result['ok'])
        self.assertEqual(run.call_count, 1)
        self.assertIn('+record-get', run.call_args.args[0])
        self.assertNotIn('secret-token', (self.root / 'table_update_receipt.json').read_text())

    def test_readback_delayed_until_second_read(self):
        result, run = self.run_update([self.response({'ok': True}), self.response(self.cells(**{'任务状态': ['制作中']})),
                                      self.response(self.cells())])
        self.assertTrue(result['ok'])
        self.assertEqual(run.call_count, 3)

    def test_previous_ack_is_not_reused_for_changed_input_or_target(self):
        self.run_update([self.response({'ok': True}), self.response(self.cells())])
        receipt_path = self.root / 'table_update_receipt.json'
        original = receipt_path.read_bytes()
        for key in ('patch', 'record_id', 'target_identity', 'task_identity'):
            with self.subTest(key=key):
                receipt = json.loads(original)
                receipt[key] = 'different'
                self.write(receipt_path, receipt)
                result, run = self.run_update([self.response({'ok': True}), self.response(self.cells())])
                self.assertTrue(result['ok'])
                self.assertIn('+record-batch-update', run.call_args_list[0].args[0])
        receipt_path.write_bytes(original)
        self.write(self.task, {'record_id': 'rec-test', 'script': 'changed input'})
        result, run = self.run_update([self.response({'ok': True}), self.response(self.cells())])
        self.assertTrue(result['ok'])
        self.assertEqual(run.call_count, 2)

    def test_wrong_record_and_duplicate_fields_rejected(self):
        for mutate in ('record', 'fields'):
            with self.subTest(mutate=mutate):
                cells = self.cells()
                if mutate == 'record':
                    cells['data']['record_id_list'] = ['other']
                else:
                    cells['data']['fields'][-1] = '任务状态'
                self.assertEqual(m._table_readback_cells(cells, 'rec-test'), {})

    def test_markdown_unc_requires_matching_target(self):
        path = r'\\server\share\draft'
        self.assertTrue(m._table_cell_matches('草稿文件', f'[{path}](http://{path})', path))
        self.assertFalse(m._table_cell_matches('草稿文件', f'[{path}](http://wrong)', path))

    def test_revision_and_packaged_default(self):
        self.write(self.task, {'record_id': 'rec-test', 'rev': True})
        result, _ = self.run_update([self.response({'ok': True}),
                                    self.response(self.cells(**{'成片状态': ['custom-revised']}))])
        self.assertTrue(result['ok'])
        defaults = json.loads(m.DEFAULT_POLICY.read_text(encoding='utf-8-sig'))
        self.assertEqual(defaults['table_write']['initial_success_status'], '一次成片')

    def test_failed_write_never_reads_or_persists_provider_echo(self):
        result, run = self.run_update([self.response({'ok': False, 'message': 'secret-token'}, 1)])
        self.assertFalse(result['ok'])
        self.assertEqual(run.call_count, 1)
        self.assertNotIn('secret-token', (self.root / 'table_update_receipt.json').read_text())


if __name__ == '__main__':
    unittest.main()
