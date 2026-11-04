import json
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import delivery_runtime as runtime


class DeliveryRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.package = self.base / "candidate"
        self.pipeline = self.base / "pipeline"
        self.skills = self.base / "skills"
        self._seed_package()
        self._seed_runtime(self.pipeline, runtime.PIPELINE_FILES)
        self._seed_runtime(self.skills, runtime.SHIP_FILES)

    def _seed_package(self):
        self.package.mkdir()
        (self.package / "VERSION").write_text("1.5.6\n", encoding="utf-8")
        files = []
        for relative in sorted(set(runtime.PIPELINE_FILES) | set(runtime.SHIP_FILES)):
            path = self.package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            content = ("candidate:" + relative).encode("utf-8")
            path.write_bytes(content)
            files.append({"path": relative, "size": len(content)})
        (self.package / "release-manifest.json").write_text(
            json.dumps({"version": "1.5.6", "files": files}), encoding="utf-8")

    def _seed_runtime(self, root, required):
        root.mkdir()
        (root / "VERSION").write_text("1.5.6\n", encoding="utf-8")
        for relative in required:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("candidate:" + relative).encode("utf-8"))

    def test_all_required_files_match(self):
        result = runtime.diagnose(self.package, self.pipeline, self.skills)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["issues"], [])

    def test_missing_single_module_blocks_same_version_runtime(self):
        (self.pipeline / "tools" / "draft_delivery.py").unlink()
        result = runtime.diagnose(self.package, self.pipeline, self.skills)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertIn("PIPELINE_MISSING:tools/draft_delivery.py", result["issues"])

    def test_old_same_version_source_is_reported_as_drift(self):
        path = self.skills / "tools" / "draft_delivery.py"
        path.write_bytes(b"old implementation, same VERSION")
        result = runtime.diagnose(self.package, self.pipeline, self.skills)
        self.assertEqual(result["runtimes"]["skills"]["version"], "1.5.6")
        self.assertIn("SKILLS_CONTENT_MISMATCH:tools/draft_delivery.py", result["issues"])

    def test_runtime_comparison_uses_current_source_without_frozen_hashes(self):
        path = self.package / "tools" / "draft_delivery.py"
        path.write_bytes(b"candidate modified after manifest")
        result = runtime.diagnose(self.package, self.pipeline, self.skills)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertNotIn("PACKAGE_CONTENT_MISMATCH:tools/draft_delivery.py", result["issues"])
        self.assertIn("PIPELINE_CONTENT_MISMATCH:tools/draft_delivery.py", result["issues"])
        self.assertIn("SKILLS_CONTENT_MISMATCH:tools/draft_delivery.py", result["issues"])

    def test_ship_dependency_guard_uses_only_the_requested_root(self):
        runtime.require_ship_dependencies(self.skills)
        (self.skills / "tools" / "draft_delivery.py").unlink()
        with self.assertRaisesRegex(RuntimeError, "SHIP_DEPENDENCIES_MISSING:.*tools/draft_delivery.py"):
            runtime.require_ship_dependencies(self.skills)

    def _full_install(self):
        relative = 'doubao-jianying-orchestrator/orchestrator/selection/ai_editor.py'
        manifest = json.loads((self.package / 'release-manifest.json').read_text('utf-8'))
        manifest['files'].append({'path': relative})
        manifest['file_count'] = len(manifest['files'])
        (self.package / 'release-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        for root in (self.package, self.pipeline, self.skills):
            for row in manifest['files']:
                path = root / row['path']
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(('candidate:' + row['path']).encode())
        return relative, manifest

    def test_full_inventory_same_version_drift_on_both_faces(self):
        relative, _ = self._full_install()
        self.assertEqual(runtime.diagnose(self.package, self.pipeline, self.skills,
                                        all_declared=True)['status'], 'OK')
        for root in (self.pipeline, self.skills):
            (root / relative).write_bytes(b'local patch with unchanged VERSION')
        self.assertEqual(runtime.diagnose(self.package, self.pipeline, self.skills)['status'], 'OK')
        result = runtime.diagnose(self.package, self.pipeline, self.skills, all_declared=True)
        for face in ('PIPELINE', 'SKILLS'):
            self.assertIn(face + '_CONTENT_MISMATCH:' + relative, result['issues'])

    def test_full_inventory_missing_candidate_cannot_pass(self):
        relative, _ = self._full_install()
        (self.package / relative).unlink()
        result = runtime.diagnose(self.package, self.pipeline, self.skills, all_declared=True)
        self.assertIn('PACKAGE_MISSING:' + relative, result['issues'])
        self.assertEqual(result['status'], 'BLOCKED')

    def test_full_inventory_rejects_invalid_paths_and_counts(self):
        _, manifest = self._full_install()
        for value in ('../outside', '/absolute', 'C:/outside', 'C:relative', '//server/file',
                      'dir\\file', 'dir/../file', 'dir//file', './file', 'dir/file.',
                      'dir/file:stream', 'NUL.txt', '', None, 12):
            with self.subTest(path=value):
                invalid = dict(manifest, files=manifest['files'] + [{'path': value}],
                               file_count=manifest['file_count'] + 1)
                (self.package / 'release-manifest.json').write_text(json.dumps(invalid), encoding='utf-8')
                result = runtime.diagnose(self.package, self.pipeline, self.skills, all_declared=True)
                self.assertEqual(result['status'], 'BLOCKED')
                self.assertIn('PACKAGE_UNSAFE_OR_INVALID_MANIFEST_PATH', result['issues'])
        for files, count in (([], 0), (manifest['files'], 1),
                             (manifest['files'] + [manifest['files'][0]], manifest['file_count'] + 1),
                             (manifest['files'] + [{'path': manifest['files'][0]['path'].upper()}],
                              manifest['file_count'] + 1)):
            (self.package / 'release-manifest.json').write_text(
                json.dumps(dict(manifest, files=files, file_count=count)), encoding='utf-8')
            self.assertEqual(runtime.diagnose(self.package, self.pipeline, self.skills,
                                             all_declared=True)['status'], 'BLOCKED')

    def test_full_cli_returns_nonzero_for_noncritical_drift(self):
        relative, _ = self._full_install()
        (self.skills / relative).write_bytes(b'drift')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runtime.main(['--package', str(self.package), '--pipeline', str(self.pipeline),
                                 '--skills', str(self.skills), '--all-declared'])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue())['scope'], 'all-declared')

    def test_full_inventory_refuses_resolved_escape_on_either_face(self):
        relative, _ = self._full_install()
        original_resolve = Path.resolve
        for root in (self.package, self.pipeline, self.skills):
            with self.subTest(root=root):
                escaped = root.resolve() / relative
                outside = self.base.resolve() / 'outside.py'
                def resolve(path, *args, **kwargs):
                    if path == escaped:
                        return outside
                    return original_resolve(path, *args, **kwargs)
                with mock.patch.object(Path, 'resolve', resolve):
                    result = runtime.diagnose(self.package, self.pipeline, self.skills, all_declared=True)
                self.assertEqual(result['status'], 'BLOCKED')
                self.assertTrue(any('PATH_ESCAPE_OR_SYMLINK:' + relative in item
                                    for item in result['issues']))


if __name__ == "__main__":
    unittest.main()
