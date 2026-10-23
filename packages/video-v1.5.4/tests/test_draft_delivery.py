import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import draft_delivery as delivery
from tools import nas_portable as n
from tools.delivery_identity import tree_identity


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / 'shared' / '独立草稿'
        (self.source / 'media').mkdir(parents=True)
        self.media = self.source / 'media' / '配音.ogg'
        self.media.write_bytes(b'collected narration')
        self.payload = {'id': 'generated-id', 'name': self.source.name,
                        'duration': 1000000, 'canvas_config': {'width': 1080, 'height': 1920},
                        'tracks': [], 'materials': {'audios': [{'id': 'a', 'path': str(self.media)}]}}
        self.save(self.payload)
        n.write_json(self.source / 'draft_meta_info.json', {
            'draft_id': self.payload['id'], 'draft_name': self.source.name,
            'draft_fold_path': str(self.source), 'draft_root_path': str(self.source.parent),
            'draft_json_file': str(self.source / 'draft_info.json')})

    def save(self, payload):
        for name in ('draft_info.json', 'draft_content.json'):
            n.write_json(self.source / name, payload)

    def evidence(self):
        task, result = self.base / 'task.json', self.base / 'result.json'
        n.write_json(task, {'manifest': {'draft_name': self.source.name}})
        n.write_json(result, {'status': 'SUCCESS', 'draft_name': self.source.name, 'total_s': 1})
        return task, result

    def test_shared_recipient_copy_needs_no_path_rewrite(self):
        receiver = self.base / 'other-machine' / self.source.name
        shutil.copytree(self.source, receiver)
        check = delivery.verify_shared(receiver, self.source)
        self.assertEqual(check['literal_references'], 1)
        self.assertFalse(check['desktop_playback_verified'])

    def test_relative_transport_is_not_a_runnable_shared_draft(self):
        package = self.base / 'transport'
        n.package_draft(self.source, package)
        with self.assertRaisesRegex(RuntimeError, 'NOT_ABSOLUTE'):
            delivery.verify_shared(package, self.source)

    def test_basename_fallback_cannot_hide_a_missing_literal_reference(self):
        wrong = copy.deepcopy(self.payload)
        wrong['materials']['audios'][0]['path'] = str(self.base / 'old-computer' / self.media.name)
        self.save(wrong)
        # Existing transport resolver can locate the collected basename.
        _, refs = n.inspect(self.source)
        self.assertEqual(next(iter(refs.values())).resolve(), self.media.resolve())
        with self.assertRaisesRegex(RuntimeError, 'OUTSIDE_TARGET'):
            delivery.verify_shared(self.source, self.source)

    def test_missing_shared_file_cannot_be_hidden_by_recipient_copy(self):
        receiver = self.base / 'receiver'
        shutil.copytree(self.source, receiver)
        self.media.unlink()
        with self.assertRaisesRegex(RuntimeError, 'LITERAL_MEDIA_MISSING'):
            delivery.verify_shared(receiver, self.source)

    def test_collected_and_shared_media_must_have_identical_bytes(self):
        receiver = self.base / 'receiver'
        shutil.copytree(self.source, receiver)
        (receiver / 'media' / self.media.name).write_bytes(b'wrong content')
        with self.assertRaisesRegex(RuntimeError, 'BYTES_DIFFER'):
            delivery.verify_shared(receiver, self.source)

    def test_registration_paths_are_checked(self):
        meta = n.read_json(self.source / 'draft_meta_info.json')
        meta['draft_fold_path'] = str(self.base / 'old computer')
        n.write_json(self.source / 'draft_meta_info.json', meta)
        with self.assertRaisesRegex(RuntimeError, 'REGISTRATION_PATH_MISMATCH'):
            delivery.verify_shared(self.source, self.source)

    def test_encrypted_active_timeline_blocks_shared_verification(self):
        n.write_json(self.source / 'Timelines/project.json', {'main_timeline_id': 'active'})
        timeline = self.source / 'Timelines/active/draft_content.json'
        timeline.parent.mkdir()
        timeline.write_bytes(b'encrypted editing session')
        before = tree_identity(self.source)
        with self.assertRaises(n.TransportError):
            delivery.verify_shared(self.source, self.source)
        self.assertEqual(before, tree_identity(self.source))

    def test_restore_requires_confirmation_before_reading_or_writing(self):
        target = self.base / 'restored'
        with self.assertRaisesRegex(RuntimeError, 'EXPLICIT_CONFIRMATION'):
            delivery.restore_generated(self.source, Path('absent'), Path('absent'), target)
        self.assertFalse(target.exists())

    def test_restore_rejects_wrong_success_record_without_creating_draft(self):
        task, result = self.evidence()
        data = n.read_json(result)
        data['draft_name'] = 'another project'
        n.write_json(result, data)
        target = self.base / 'restored'
        with self.assertRaisesRegex(RuntimeError, 'EVIDENCE_MISMATCH'):
            delivery.restore_generated(self.source, task, result, target, confirmed=True)
        self.assertFalse(target.exists())

    def test_restore_does_not_overwrite_existing_edits(self):
        task, result = self.evidence()
        target = self.base / 'edited'
        target.mkdir()
        keep = target / 'keep.txt'
        keep.write_text('manual changes')
        with self.assertRaisesRegex(RuntimeError, 'DESTINATION_EXISTS'):
            delivery.restore_generated(self.source, task, result, target, confirmed=True)
        self.assertEqual(keep.read_text(), 'manual changes')

    def test_restore_preserves_all_material_groups(self):
        original = copy.deepcopy(self.payload)
        restored = copy.deepcopy(original)
        restored['id'] = 'fresh'
        delivery.compare_generated(original, restored)
        restored['materials']['effects'] = [{'id': 'new'}]
        with self.assertRaisesRegex(RuntimeError, 'MATERIALS_CHANGED'):
            delivery.compare_generated(original, restored)

    def test_pipeline_delivery_checks_both_mirrors_and_keeps_contract(self):
        ship = delivery.ship_module()
        share = self.base / 'nas'
        staging = self.base / 'staging' / self.source.name
        shutil.copytree(self.source, staging)
        def copy_to_nas(src, dst):
            shutil.copytree(src, dst)
            return 1
        with patch.dict(ship.os.environ, {'JY_NAS_SHARE': str(share)}), \
                patch.object(ship, 'mount_nas', return_value=0), \
                patch.object(ship, 'robocopy', side_effect=copy_to_nas), \
                patch.object(sys, 'argv', ['ship', '--draft', str(staging)]), \
                patch('builtins.print') as output:
            self.assertEqual(ship.main(), 0)
        receipt = json.loads(output.call_args.args[0])
        self.assertTrue(receipt['shared_media_references_verified'])
        self.assertTrue(receipt['nas_root_verified'])
        self.assertTrue(receipt['distribution_verified'])
        self.assertEqual(receipt['staged_tree_sha256'], tree_identity(staging)['sha256'])

    def test_pipeline_fails_when_literal_verification_fails(self):
        ship = delivery.ship_module()
        staging = self.base / 'staging' / self.source.name
        shutil.copytree(self.source, staging)
        with patch.dict(ship.os.environ, {'JY_NAS_SHARE': str(self.base / 'nas')}), \
                patch.object(ship, 'mount_nas', return_value=0), \
                patch.object(ship, 'robocopy', side_effect=lambda a, b: (shutil.copytree(a, b), 1)[1]), \
                patch.object(sys, 'argv', ['ship', '--draft', str(staging)]), \
                patch.object(delivery, 'verify_shared', side_effect=RuntimeError('literal failure')), \
                patch('builtins.print'):
            self.assertEqual(ship.main(), 10)

    def test_resumed_hash_receipt_cannot_bypass_literal_path_check(self):
        import batch_worker as worker
        task_dir = self.base / 'task'
        task_dir.mkdir()
        target = self.base / 'nas' / '测试产出' / '分发草稿' / self.source.name
        shutil.copytree(self.source, target)
        receipt = {'status': 'VERIFIED', 'draft_name': self.source.name,
                   'source_output_sha256': worker.production_eval.output_identity([self.source])['sha256'],
                   'nas_target': str(target),
                   'transformed_tree_sha256': tree_identity(target)['sha256']}
        n.write_json(task_dir / 'done.json', {'distribution_receipts': [receipt]})
        with patch.object(worker, 'DRAFTS', self.source.parent), \
                patch.object(worker, 'MAC_REVIEW_REQUIRED', False), \
                patch.dict(worker.os.environ, {'JY_EVAL_MODE': 'observe'}), \
                patch.object(worker, 'ship_one') as ship:
            result = worker.finalize_closed_loop(task_dir, {}, [self.source.name], [])
        self.assertEqual(result['reason'], 'DISTRIBUTION_MEDIA_REFERENCE_INVALID')
        ship.assert_not_called()


if __name__ == '__main__':
    unittest.main()
