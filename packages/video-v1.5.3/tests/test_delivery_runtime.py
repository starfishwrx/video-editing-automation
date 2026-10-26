import json
from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
