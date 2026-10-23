import copy
import json
import tempfile
import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch

from tools import nas_portable as n


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.draft = self.root / '编辑中的草稿'
        (self.draft / 'media').mkdir(parents=True)
        self.video = self.draft / 'media' / '画面.mov'
        self.video.write_bytes(b'video-source-contents')
        self.audio = self.root / '外部配音.ogg'
        self.audio.write_bytes(b'audio-source-contents')
        self.payload = {'id': 'project-id', 'name': self.draft.name, 'materials': {'videos': [{'id': 'video', 'path': str(self.video), 'media_path': '', 'type': 'video'}], 'audios': [{'id': 'audio', 'path': str(self.audio)}], 'effects': [{'id': 'effect', 'path': 'C:/Editor/Resources/combine_adjust'}]}, 'tracks': [{'type': 'video', 'name': 'Video', 'segments': [{'id': 'clip', 'material_id': 'video', 'target_timerange': {'start': 0, 'duration': 2000000}, 'source_timerange': {'start': 500000, 'duration': 2000000}}]}, {'type': 'audio', 'name': 'Narration', 'segments': [{'material_id': 'audio', 'target_timerange': {'start': 0, 'duration': 2000000}}]}]}
        self.save('draft_info.json', self.payload)
        self.save('draft_content.json', self.payload)
        self.save('draft_meta_info.json', {'draft_id': 'project-id', 'draft_name': self.draft.name})

    def tearDown(self):
        self.temp.cleanup()

    def save(self, name, value):
        n.write_json(self.draft / name, value)

    def assert_blocked(self, code, callable, *args):
        with self.assertRaises(n.TransportError) as caught:
            callable(*args)
        self.assertEqual(caught.exception.code, code)

    def test_changed_computer_path_and_renamed_folder_round_trip(self):
        before = n.digest(self.draft / 'draft_content.json')
        package = self.root / 'NAS' / '上传包改名后'
        n.package_draft(self.draft, package)
        destination = self.root / '另一台机器的库' / '任意新名字'
        receipt = n.import_package(package, destination)
        self.assertEqual(receipt['status'], 'LOCAL_MEDIA_VERIFIED')
        self.assertFalse(receipt['visual_verified'])
        restored = n.read_json(destination / 'draft_content.json')
        self.assertEqual(restored['tracks'], self.payload['tracks'])
        self.assertEqual(restored['materials']['effects'], self.payload['materials']['effects'])
        self.assertEqual(n.digest(self.draft / 'draft_content.json'), before)
        n.inspect(destination, require_local=True)
        self.assertEqual(n.read_json(destination / 'draft_meta_info.json')['draft_fold_path'], str(destination.resolve()))

    def test_current_encrypted_content_rejects_old_plain_info(self):
        (self.draft / 'draft_content.json').write_bytes(b'ciphertext-not-json')
        output = self.root / 'bad-package'
        self.assert_blocked('ENCRYPTED_DOCUMENT', n.package_draft, self.draft, output)
        self.assertFalse(output.exists())

    def test_encrypted_metadata_rejected(self):
        (self.draft / 'draft_meta_info.json').write_bytes(b'ciphertext')
        self.assert_blocked('ENCRYPTED_DOCUMENT', n.package_draft, self.draft, self.root / 'out')

    def test_empty_optional_config_is_not_misclassified_as_ciphertext(self):
        (self.draft / 'draft_biz_config.json').write_bytes(b'')
        n.inspect(self.draft)
        (self.draft / 'draft_content.json').write_bytes(b'encrypted-current')
        self.assert_blocked('ENCRYPTED_DOCUMENT', n.inspect, self.draft)

    def test_nested_current_timeline_is_checked_and_localized(self):
        self.save('Timelines/project.json', {'main_timeline_id': 'current', 'timelines': [{'id': 'current'}]})
        self.save('Timelines/current/draft_content.json', self.payload)
        package = self.root / 'pkg'
        n.package_draft(self.draft, package)
        dest = self.root / 'restore'
        n.import_package(package, dest)
        _, refs = n.inspect(dest, require_local=True)
        self.assertTrue(refs)
        restored = n.read_json(dest / 'Timelines/current/draft_content.json')
        self.assertEqual(restored['tracks'], self.payload['tracks'])

    def test_nested_encrypted_timeline_rejected_before_writes(self):
        self.save('Timelines/project.json', {'main_timeline_id': 'active'})
        path = self.draft / 'Timelines/active/draft_content.json'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'encrypted')
        self.assert_blocked('ENCRYPTED_DOCUMENT', n.package_draft, self.draft, self.root / 'out')

    def test_missing_media_blocks_export(self):
        self.video.unlink()
        self.assert_blocked('MEDIA_MISSING_OR_AMBIGUOUS', n.package_draft, self.draft, self.root / 'out')

    def test_incomplete_nas_download_blocks_import(self):
        package = self.root / 'pkg'
        n.package_draft(self.draft, package)
        next((package / 'media').glob('*.mov')).unlink()
        dest = self.root / 'restore'
        self.assert_blocked('PACKAGE_HASH_MISMATCH', n.import_package, package, dest)
        self.assertFalse(dest.exists())

    def test_damaged_media_blocks_import(self):
        package = self.root / 'pkg'
        n.package_draft(self.draft, package)
        next((package / 'media').glob('*.mov')).write_bytes(b'damaged-video-source-')
        self.assert_blocked('PACKAGE_HASH_MISMATCH', n.import_package, package, self.root / 'restore')

    def test_local_edits_are_never_overwritten(self):
        package = self.root / 'pkg'
        n.package_draft(self.draft, package)
        dest = self.root / 'restore'
        dest.mkdir()
        keep = dest / 'my-edit.txt'
        keep.write_text('keep me')
        self.assert_blocked('DESTINATION_EXISTS', n.import_package, package, dest)
        self.assertEqual(keep.read_text(), 'keep me')

    def test_manifest_traversal_rejected(self):
        package = self.root / 'pkg'
        n.package_draft(self.draft, package)
        manifest = n.read_json(package / n.MANIFEST)
        manifest['files']['../outside.mov'] = {'bytes': 0, 'sha256': ''}
        n.write_json(package / n.MANIFEST, manifest)
        self.assert_blocked('UNSAFE_PACKAGE_PATH', n.verify_package, package)

    def test_same_name_different_contents_preserved(self):
        extra = self.root / '另一组素材' / '画面.mov'
        extra.parent.mkdir()
        extra.write_bytes(b'different same-name video')
        payload = copy.deepcopy(self.payload)
        payload['materials']['videos'].append({'id': 'another', 'path': str(extra)})
        self.save('draft_content.json', payload)
        package = self.root / 'pkg'
        n.package_draft(self.draft, package)
        self.assertEqual(len(list((package / 'media').glob('*.mov'))), 2)

    def test_localize_stops_while_editor_running(self):
        before = n.digest(self.draft / 'draft_info.json')
        with patch.object(n, 'editor_running', return_value=True):
            self.assert_blocked('EDITOR_RUNNING', n.localize_draft, self.draft)
        self.assertEqual(n.digest(self.draft / 'draft_info.json'), before)

    def test_source_change_blocks_publication(self):
        original = n.shutil.copy2
        def changing_copy(source, target):
            result = original(source, target)
            if Path(source) == self.video:
                self.video.write_bytes(b'changed-during-copy')
            return result
        with patch.object(n.shutil, 'copy2', side_effect=changing_copy):
            self.assert_blocked('SOURCE_CHANGED', n.package_draft, self.draft, self.root / 'out')
        self.assertFalse((self.root / 'out').exists())

    def ship(self):
        path = Path(__file__).resolve().parents[1] / 'doubao-jianying-orchestrator/orchestrator/ship_distribute.py'
        spec = importlib.util.spec_from_file_location('nas_test_ship', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_pipeline_checks_current_encrypted_timeline_before_any_write(self):
        (self.draft / 'draft_content.json').write_bytes(b'encrypted-current')
        before = n.digest(self.draft / 'draft_info.json')
        self.assert_blocked('ENCRYPTED_DOCUMENT', self.ship().rewrite, str(self.draft), 'local')
        self.assertEqual(n.digest(self.draft / 'draft_info.json'), before)

    def test_pipeline_rewrites_all_plain_current_timelines(self):
        (self.draft / 'media' / self.audio.name).write_bytes(self.audio.read_bytes())
        self.save('Timelines/project.json', {'main_timeline_id': 'current'})
        self.save('Timelines/current/draft_content.json', self.payload)
        with patch.dict(n.os.environ, {'JY_NAS_SHARE': r'\\test-server\test-share'}):
            self.ship().rewrite(str(self.draft), 'unc')
        restored = n.read_json(self.draft / 'Timelines/current/draft_content.json')
        self.assertTrue(restored['materials']['videos'][0]['path'].startswith(r'\\test-server\test-share'))
        self.assertEqual(restored['tracks'], self.payload['tracks'])

    def test_new_pipeline_injects_verified_tools_instead_of_missing_v3_templates(self):
        injected = self.ship().inject_tools(str(self.draft))
        self.assertIn('nas_portable.py', injected)
        self.assertIn('一键取稿.bat', injected)
        self.assertIn('nas-tool.ps1', injected)
        self.assertTrue((self.draft / 'nas_portable.py').is_file())


if __name__ == '__main__':
    unittest.main()
