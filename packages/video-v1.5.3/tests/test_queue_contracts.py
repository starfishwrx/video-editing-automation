"""Queue decisions remain usable without initializing the production worker."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.queueing import contracts


class QueueContractTests(unittest.TestCase):
    def test_import_does_not_initialize_worker_or_orchestrator(self):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c",
             "import json,sys; from tools.queueing import contracts; "
             "print(json.dumps([n for n in sys.modules "
             "if n == 'batch_worker' or n.startswith('orchestrator')]))"],
            cwd=ROOT, text=True, encoding="utf-8", capture_output=True,
            check=True)
        self.assertEqual(json.loads(result.stdout), [])

    def test_credentials_override_retryable_network_signal(self):
        classified = contracts.classify_build_failure(
            "VISION_CALL_FAILED: 503 network; unauthorized invalid api key")
        self.assertFalse(classified["transient"])
        self.assertTrue(classified["deterministic_block"])
        self.assertIn("503", classified["markers"])

    def test_network_retry_does_not_certify_task(self):
        classified = contracts.classify_build_failure(
            "VISION_CALL_FAILED: 503 network timeout")
        self.assertTrue(classified["transient"])
        result = contracts.batch_result_contract(
            "ERROR", pipeline_version="test-runtime", run_id="run-test",
            failures=[{"type": "transient", "error": "VISION_CALL_FAILED 503 network timeout"}])
        self.assertEqual(result["terminal_state"], "ERROR")
        self.assertEqual(result["failure_class"], "vision")
        self.assertTrue(result["retryable"])
        self.assertFalse(result["safe_to_continue"])

    def test_preview_and_partial_never_become_success(self):
        for status in ("PREVIEW_READY", "PARTIAL"):
            with self.subTest(status=status):
                result = contracts.batch_result_contract(
                    status, pipeline_version="test-runtime", run_id="run-test")
                self.assertEqual(result["pipeline_version"], "test-runtime")
                self.assertEqual(result["terminal_state"], "UNCERTIFIED")
                self.assertEqual(result["next_action"], "manual_review")
                self.assertFalse(result["safe_to_continue"])

    def test_deterministic_visual_failure_requires_review(self):
        result = contracts.batch_result_contract(
            "ERROR", pipeline_version="test-runtime", run_id="run-test",
            error="VISUAL_MATCH_BLOCKED: missing evidence")
        self.assertEqual(result["terminal_state"], "BLOCKED")
        self.assertEqual(result["failure_class"], "semantic")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["safe_to_continue"])

    def test_queue_target_requires_unique_record_and_does_not_mutate(self):
        queue = [{"record_id": "first"}, {"record_id": "second"}]
        original = copy.deepcopy(queue)
        selected = contracts.select_queue_for_single_run(
            queue, record_id="second", force_rebuild=True)
        self.assertEqual(selected, [{"record_id": "second", "force_rebuild": True}])
        self.assertEqual(queue, original)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            contracts.select_queue_for_single_run(
                [{"record_id": "same"}, {"record_id": "same"}], record_id="same")
        with self.assertRaisesRegex(ValueError, "exact record_id"):
            contracts.select_queue_for_single_run(queue, force_rebuild=True)

    def test_partial_resume_is_explicit_input(self):
        selected = contracts.select_queue_for_single_run(
            [{"record_id": "first", "retry_error": True}],
            record_id="first", force_rebuild=True, resume_partial=True)
        self.assertEqual(selected, [{"record_id": "first", "retry_error": False,
                                     "force_rebuild": False, "retry_deferred": True}])

    def test_parallel_rows_must_have_distinct_titles_and_materials(self):
        left = {"title": "Paper", "material_dir": "pool-a"}
        self.assertFalse(contracts.parallel_pair_safe(
            left, {"title": " paper ", "material_dir": "pool-b"}))
        self.assertFalse(contracts.parallel_pair_safe(
            left, {"title": "Tissue", "material_dir": "POOL-A"}))
        self.assertFalse(contracts.parallel_pair_safe(left, {"title": "Tissue"}))
        self.assertTrue(contracts.parallel_pair_safe(
            left, {"title": "Tissue", "material_dir": "pool-b"}))

    def test_explicit_bgm_input_never_turns_into_random_selection(self):
        self.assertEqual(contracts.meeting_bgm_spec("song-id"), {"music_id": "song-id"})
        self.assertEqual(contracts.meeting_bgm_spec(r"C:\audio\song.mp3"),
                         {"path": r"C:\audio\song.mp3"})
        value = {"music_id": "song-id"}
        self.assertEqual(contracts.meeting_bgm_spec(value), value)
        self.assertIsNot(contracts.meeting_bgm_spec(value), value)

    def test_completed_negative_observation_is_reusable_but_not_invented(self):
        rejected = {"description": "No usable action", "visual_tags": ["paper"],
                    "evidence_tags": [], "analysis_confidence": 0.9,
                    "visual_observation_complete": True, "action_complete": False,
                    "status": "pending_analysis"}
        self.assertTrue(contracts.is_reusable_analysis_row(rejected))
        self.assertFalse(contracts.is_reusable_analysis_row(
            {**rejected, "visual_observation_complete": False}))

    def test_worker_wrapper_preserves_runtime_and_marker_patch_points(self):
        import batch_worker
        with patch.object(batch_worker, "_pipeline_version", return_value="patched-runtime"):
            result = batch_worker._batch_result_contract("OK", run_id="run-test")
        self.assertEqual(result["pipeline_version"], "patched-runtime")
        with patch.object(batch_worker, "TRANSIENT_BUILD_MARKERS", ("custom",)):
            result = batch_worker.classify_build_failure("custom network timeout")
        self.assertEqual(result["markers"], ["custom"])
        self.assertTrue(result["transient"])
        with patch.dict(os.environ, {"JY_TASK_RESUME_PARTIAL": "1"}):
            selected = batch_worker.select_queue_for_single_run(
                [{"record_id": "first"}], record_id="first", force_rebuild=True)
        self.assertTrue(selected[0]["retry_deferred"])
        self.assertFalse(selected[0]["force_rebuild"])


if __name__ == "__main__":
    unittest.main()
