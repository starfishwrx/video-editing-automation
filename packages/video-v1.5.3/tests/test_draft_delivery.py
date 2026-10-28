import copy
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import subprocess
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
        before = tree_identity(staging)
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
        self.assertEqual(before, tree_identity(staging))
        self.assertEqual(receipt['staged_tree_sha256'], tree_identity(Path(receipt['shared_reference_root']))['sha256'])

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

    def test_closed_loop_publishes_success_only_after_archive_and_table(self):
        import batch_worker as worker
        tool = self.base / 'closed_loop.py'
        policy = self.base / 'policy.json'
        tool.touch()
        policy.touch()
        for failure in ('archive-parent', 'table-update', 'invalid-receipt', None):
            with self.subTest(failure=failure):
                task_dir = self.base / ('task-' + str(failure))
                task_dir.mkdir()
                receipt = {'draft_name': self.source.name, 'nas_target': 'verified-target'}

                def run(command, **kwargs):
                    authoritative = n.read_json(task_dir / 'done.json')
                    self.assertEqual(authoritative['status'], 'PREVIEW_READY')
                    candidate_path = Path(command[command.index('--done-json') + 1])
                    self.assertEqual(candidate_path.name, 'closed_loop_candidate.json')
                    self.assertEqual(n.read_json(candidate_path)['status'], 'OK')
                    stage = command[2]
                    if stage == failure:
                        return subprocess.CompletedProcess(command, 1, '', 'failed')
                    if stage == 'archive-parent':
                        payload = n.read_json(candidate_path)
                        payload['nas_parent_folder'] = 'verified-parent'
                        n.write_json(candidate_path, payload)
                    if stage == 'table-update' and failure == 'invalid-receipt':
                        return subprocess.CompletedProcess(command, 0, '{}', '')
                    return subprocess.CompletedProcess(command, 0, '{"ok": true}', '')

                with patch.object(worker, 'DRAFTS', self.source.parent), \
                        patch.object(worker, 'MAC_REVIEW_REQUIRED', False), \
                        patch.object(worker, 'CLOSED_LOOP_TOOL', tool), \
                        patch.object(worker, 'CLOSED_LOOP_POLICY', policy), \
                        patch.dict(worker.os.environ, {'JY_EVAL_MODE': 'observe'}), \
                        patch.object(worker, 'ship_one', return_value={'rc': 0, 'receipt': receipt}), \
                        patch.object(worker.subprocess, 'run', side_effect=run):
                    result = worker.finalize_closed_loop(task_dir, {}, [self.source.name], [])
                done = n.read_json(task_dir / 'done.json')
                if failure:
                    self.assertFalse(result['ok'])
                    self.assertEqual(done['status'], 'PREVIEW_READY')
                else:
                    self.assertTrue(result['ok'])
                    self.assertEqual(done['status'], 'OK')
                    self.assertEqual(done['nas_parent_folder'], 'verified-parent')

    def test_partial_copy_is_not_visible_as_final_delivery(self):
        target = self.base / 'published' / 'final-draft'
        def incomplete_copy(source, staging):
            Path(staging).mkdir()
            shutil.copy2(Path(source) / 'draft_info.json', Path(staging) / 'draft_info.json')
            return 1  # A successful copy exit code is insufficient.
        with self.assertRaisesRegex(RuntimeError, 'COPY_HASH_MISMATCH'):
            delivery.publish_tree(self.source, target, tree_identity(self.source), copier=incomplete_copy)
        self.assertFalse(target.exists())
        self.assertEqual(len(list(target.parent.glob('.publishing-*'))), 1)

    def test_existing_delivery_is_preserved_and_identical_retry_is_idempotent(self):
        target = self.base / 'published' / 'final-draft'
        identity = tree_identity(self.source)
        self.assertTrue(delivery.publish_tree(self.source, target, identity))
        self.assertFalse(delivery.publish_tree(self.source, target, identity))
        (target / 'manual-edit.txt').write_text('keep edits')
        before = tree_identity(target)
        with self.assertRaisesRegex(RuntimeError, 'IDENTITY_CONFLICT'):
            delivery.publish_tree(self.source, target, identity)
        self.assertEqual(tree_identity(target), before)

    def test_archive_rebases_every_timeline_and_survives_old_folder_move(self):
        n.write_json(self.source / 'Timelines/project.json', {'main_timeline_id': 'active'})
        n.write_json(self.source / 'Timelines/active/draft_content.json', self.payload)
        target = self.base / 'archive' / '版本1'
        before = tree_identity(self.source)
        result = delivery.archive_shared_copy(self.source, target)
        self.assertEqual(result['source_tree_sha256'], before['sha256'])
        self.assertEqual(tree_identity(self.source), before)
        self.source.rename(self.source.with_name('old-folder-moved'))
        self.assertEqual(delivery.verify_shared(target, target)['status'], 'SHARED_MEDIA_VERIFIED')
        self.assertEqual(n.read_json(target / 'Timelines/active/draft_content.json')['tracks'], self.payload['tracks'])
        self.assertFalse(list(target.parent.glob('.nas-package-archive-*')))

    def test_normal_worker_collects_external_same_name_media_before_real_ship(self):
        import batch_worker as worker
        ship = delivery.ship_module()
        payload = copy.deepcopy(self.payload)
        payload['materials']['audios'] = []
        for index, content in enumerate((b'first narration', b'distinct music')):
            path = self.base / str(index) / 'same-name.wav'
            path.parent.mkdir()
            path.write_bytes(content)
            payload['materials']['audios'].append({'id': str(index), 'path': str(path)})
        self.save(payload)
        before = tree_identity(self.source)
        share = self.base / 'nas'
        def run_ship(command, **kwargs):
            output = io.StringIO()
            with patch.object(sys, 'argv', command[1:]), contextlib.redirect_stdout(output):
                code = ship.main()
            return subprocess.CompletedProcess(command, code, output.getvalue(), '')
        with patch.object(worker, 'SKILL', delivery.PACKAGE_ROOT / 'doubao-jianying-orchestrator'), \
                patch.dict(ship.os.environ, {'JY_NAS_SHARE': str(share)}), \
                patch.object(ship, 'mount_nas', return_value=0), \
                patch.object(ship, 'robocopy', side_effect=lambda a,b: (shutil.copytree(a,b),1)[1]), \
                patch.object(worker.subprocess, 'run', side_effect=run_ship):
            result = worker.ship_one(self.source, self.base / 'task')
        self.assertEqual(result['rc'], 0, result.get('out'))
        target = Path(result['receipt']['nas_target'])
        _, references = n.inspect(target)
        self.assertEqual({path.read_bytes() for path in references.values()},
                         {b'first narration', b'distinct music'})
        self.assertTrue(result['receipt']['shared_media_references_verified'])
        self.assertEqual(tree_identity(self.source), before)

    def test_missing_ship_dependency_blocks_before_any_mutation(self):
        ship = delivery.ship_module()
        before = tree_identity(self.source)
        with patch.object(ship, 'PIPE_ROOT', self.base / 'partial-install'), \
                patch.object(sys, 'argv', ['ship', '--draft', str(self.source)]), \
                patch.object(ship, 'rewrite') as rewrite, patch.object(ship, 'mount_nas') as mount, \
                patch('builtins.print'):
            self.assertEqual(ship.main(), 11)
        rewrite.assert_not_called()
        mount.assert_not_called()
        self.assertEqual(tree_identity(self.source), before)

    def test_source_edit_during_direct_ship_is_preserved_and_blocks_success(self):
        ship = delivery.ship_module()
        def copy_and_edit(source, target):
            shutil.copytree(source, target)
            changed = n.read_json(self.source / 'draft_info.json')
            changed['tracks'] = [{'id': 'new-user-edit'}]
            n.write_json(self.source / 'draft_info.json', changed)
            return 1
        with patch.dict(ship.os.environ, {'JY_NAS_SHARE': str(self.base / 'nas')}), \
                patch.object(ship, 'mount_nas', return_value=0), \
                patch.object(ship, 'robocopy', side_effect=copy_and_edit), \
                patch.object(sys, 'argv', ['ship', '--draft', str(self.source), '--work', str(self.base / 'private-work')]), \
                patch('builtins.print') as output:
            self.assertEqual(ship.main(), 12)
        self.assertEqual(json.loads(output.call_args.args[0])['status'], 'FAIL')
        self.assertEqual(n.read_json(self.source / 'draft_info.json')['tracks'], [{'id': 'new-user-edit'}])

    def test_rewrite_rejects_document_drift_instead_of_restoring_old_snapshot(self):
        ship = delivery.ship_module()
        original_inspect = n.inspect
        def changed_after_read(*args, **kwargs):
            result = original_inspect(*args, **kwargs)
            changed = copy.deepcopy(self.payload)
            changed['tracks'] = [{'id': 'manual-change'}]
            n.write_json(self.source / 'draft_info.json', changed)
            return result
        with patch.object(n, 'inspect', side_effect=changed_after_read):
            with self.assertRaisesRegex(RuntimeError, 'DRAFT_CHANGED_DURING_REWRITE'):
                ship.rewrite(str(self.source), 'local')
        self.assertEqual(n.read_json(self.source / 'draft_info.json')['tracks'], [{'id': 'manual-change'}])

    def test_both_archive_entrypoints_publish_independent_versions(self):
        import closed_loop as lightweight
        from tools import closed_loop as audited
        second = self.source.with_name('second-draft')
        shutil.copytree(self.source, second)
        docs, _ = n.inspect(second)
        n.set_registration(docs, second)
        docs = {name: n.map_values(value, {n.normalized(str(self.media)): str(second / 'media' / self.media.name)})
                for name, value in docs.items()}
        for name, value in docs.items():
            n.write_json(second / name, value)
        policy = self.base / 'policy.json'
        n.write_json(policy, {'policy_id': 'synthetic', 'policy_version': 'fixture'})
        published = []
        for index, module in enumerate((lightweight, audited)):
            with self.subTest(entrypoint=module.__name__):
                work = self.base / str(index) / 'task'
                n.write_json(work / 'task.json', {'record_id': 'synthetic', 'title': 'synthetic'})
                n.write_json(work / 'done.json', {'nas_links': [str(self.source), str(second)]})
                with patch.dict(module.os.environ, {'JY_NAS_DISTRIBUTION_ROOT': str(work / 'nas')}):
                    receipt = module.archive_parent(work / 'task.json', work / 'done.json', policy)
                self.assertTrue(receipt['self_contained_versions'])
                self.assertEqual(len(receipt['versions']), 2)
                published.extend(Path(path) for path in receipt['versions'])
        self.source.rename(self.source.with_name('original-moved'))
        second.rename(second.with_name('second-moved'))
        for target in published:
            self.assertEqual(delivery.verify_shared(target, target)['status'], 'SHARED_MEDIA_VERIFIED')

    def test_single_archive_cannot_certify_missing_literal_reference(self):
        import closed_loop as lightweight
        from tools import closed_loop as audited
        wrong = copy.deepcopy(self.payload)
        wrong['materials']['audios'][0]['path'] = str(self.base / 'missing' / self.media.name)
        self.save(wrong)
        ship = delivery.ship_module()
        ship.inject_tools(str(self.source))
        policy = self.base / 'policy.json'
        n.write_json(policy, {'policy_id': 'synthetic', 'policy_version': 'fixture'})
        for index, module in enumerate((lightweight, audited)):
            work = self.base / str(index)
            n.write_json(work / 'task.json', {'record_id': 'synthetic'})
            n.write_json(work / 'done.json', {'nas_links': [str(self.source)]})
            with self.assertRaisesRegex(RuntimeError, 'OUTSIDE_TARGET'):
                module.archive_parent(work / 'task.json', work / 'done.json', policy)
            self.assertNotIn('distribution_status', n.read_json(work / 'done.json'))


if __name__ == '__main__':
    unittest.main()
