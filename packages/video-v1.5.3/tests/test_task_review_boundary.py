"""Windows validation services use explicit paths and preserve worker adapters."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import production_eval as pe
from tools.acceptance import task_review


class TaskReviewBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="task review ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.task_dir = self.root / "work" / "record-fixture"
        self.task_dir.mkdir(parents=True)
        self.drafts = self.root / "drafts"
        self.draft = self.drafts / "draft-fixture"
        media = self.draft / "media" / "clip.mp4"
        media.parent.mkdir(parents=True)
        media.write_bytes(b"synthetic media")
        (self.draft / "draft_info.json").write_text(json.dumps({
            "id": "draft-id-fixture",
            "tracks": [{"name": "Video", "segments": [{"material_id": "clip"}]}],
            "materials": {"videos": [{"id": "clip", "path": str(media)}]},
        }), encoding="utf-8")
        self.task = {"record_id": "record-fixture", "task_id": "task-fixture", "title": "Fixture",
                     "script": "Synthetic script", "category": "纸品", "sku": "fixture-sku"}


    def test_import_does_not_initialize_worker(self):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c",
             "import sys; from tools.acceptance import task_review; "
             "assert 'batch_worker' not in sys.modules"],
            cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


    def test_package_check_and_worker_extension_patch_still_block(self):
        import batch_worker as worker
        self.assertTrue(task_review.validate_draft_package(
            self.draft, video_extensions=(".mp4", ".mov"))["ok"])
        with patch.object(worker, "VIDEO_EXTS", (".mov",)):
            result = worker.validate_draft_package(self.draft)
        self.assertFalse(result["ok"])
        self.assertIn("VIDEO_MEDIA_RESOLVED_ZERO", result["issues"])
        missing = task_review.validate_draft_package(
            self.root / "missing", video_extensions=(".mp4",))
        self.assertEqual(missing, {"ok": False, "issues": ["DRAFT_INFO_MISSING"]})

    def test_worker_evaluation_uses_live_validator_runtime_and_uncertified_qc(self):
        import batch_worker as worker
        source = self.root / "input.mp4"
        source.write_bytes(b"input fixture")
        runtime = {"tool_version": "test-version", "visual_policy_version": "test-policy",
                   "package_sha256": "a" * 64, "visual_policy_sha256": "b" * 64}
        frozen = pe.freeze(self.task, "run-fixture", [source], runtime)
        report = self.root / "old-qc.json"
        report.write_text('{"status":"PASS"}', encoding="utf-8")
        runtime_root = self.root / "runtime-fixture"
        with patch.object(worker, "DRAFTS", self.drafts), \
             patch.object(worker, "validate_draft_package", return_value={"ok": False, "issues": ["fixture failure"]}) as validator, \
             patch.object(pe, "runtime_identity", return_value=runtime) as read_runtime, \
             patch.dict(os.environ, {"JY_EVAL_RUNTIME_ROOT": str(runtime_root)}):
            result = worker.evaluate_task(
                self.task_dir, self.task, [self.draft.name],
                [{"draft": self.draft.name, "report": str(report)}], frozen)
        validator.assert_called_once_with(self.draft)
        read_runtime.assert_called_once_with(runtime_root)
        statuses = {row["id"]: row["status"] for row in result["criteria"]}
        self.assertEqual(statuses["DRAFT-PACKAGE"], "FAIL")
        self.assertEqual(statuses["VISUAL-QC"], "UNCERTIFIED")
        self.assertEqual(result["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
