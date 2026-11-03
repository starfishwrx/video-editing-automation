import json
from pathlib import Path
import tempfile
import unittest

from tools.throughput_calibration import compare, summarize


class ThroughputCalibrationTests(unittest.TestCase):
    def test_rejects_faster_error_prone_policy_and_unconfirmed_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for slots, elapsed, errors in ((20, 100, 0), (40, 50, 1), (80, 25, 0)):
                path = Path(tmp) / f"{slots}.json"
                path.write_text(json.dumps({"policy": {"vision_slots": slots, "prep_slots": 2},
                    "started_at": 1, "finished_at": elapsed + 1,
                    "results": [{"formal_deliveries": 1, "status": "PASS"},
                                {"formal_deliveries": 0, "status": "ERROR" if errors else "PASS"}]}))
                paths.append(path)
            self.assertEqual(compare(paths)["selected_policy"]["vision_slots"], 20)
            self.assertIsNone(compare(paths)["next_api_step"])
            self.assertEqual(compare(paths[:2], confirmed_account_capacity=40)["selected_policy"]["vision_slots"], 20)

    def summarize_rows(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"
            path.write_text(json.dumps({"policy": {"vision_slots": 20, "prep_slots": 2},
                "started_at": 1, "finished_at": 101, "results": rows}))
            return summarize(path)

    def test_legacy_draft_receipts_do_not_imply_verified_mp4(self):
        result = self.summarize_rows([{"formal_deliveries": 2, "status": "OK"}])
        self.assertEqual(result["verified_draft_deliveries"], 2)
        self.assertEqual(result["qualified_per_hour"], result["verified_drafts_per_hour"])
        self.assertEqual(result["verified_drafts_per_hour"], 72)
        self.assertIsNone(result["final_mp4_deliveries"])
        self.assertIsNone(result["final_mp4_per_hour"])
        self.assertIsNone(result["phase_timings"]["build"])
        self.assertIsNone(result["wait_timings"]["queue_wait_s"])

    def test_explicit_draft_metric_wins_and_unverified_mp4_claims_are_not_counted(self):
        result = self.summarize_rows([{"verified_draft_deliveries": 1, "formal_deliveries": 9,
                                      "final_mp4_deliveries": 99, "status": "OK"}])
        self.assertEqual(result["verified_draft_deliveries"], 1)
        self.assertIsNone(result["final_mp4_deliveries"])

    def test_measured_phase_and_wait_samples_do_not_invent_missing_timings(self):
        result = self.summarize_rows([
            {"stage": "material", "status": "ERROR", "elapsed_s": 3,
             "timing_scope": "product_preparation"},
            {"stage": "material", "status": "BLOCKED"},
            {"stage": "build", "status": "OK", "elapsed_s": 4, "queue_wait_s": 7,
             "material_wait_s": 2, "worker_queue_wait_s": 5},
            {"stage": "build", "status": "ERROR", "elapsed_s": 8},
            {"stage": "qc", "status": "OK", "elapsed_s": float("nan")},
        ])
        self.assertEqual(result["phase_timings"]["build"],
                         {"samples": 2, "total_s": 12, "p50_s": 4, "p95_s": 8})
        self.assertEqual(result["wait_timings"]["queue_wait_s"]["samples"], 1)
        self.assertEqual(result["phase_timings"]["material"]["total_s"], 3)
        self.assertIsNone(result["phase_timings"]["qc"])
        # Shared preparation failure is measured once, not another failed task.
        self.assertEqual(result["task_count"], 4)
        self.assertEqual(result["task_failures"], 2)

    def test_equal_throughput_prefers_lower_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for slots in (40, 20):
                path = Path(tmp) / f"{slots}.json"
                path.write_text(json.dumps({"policy": {"vision_slots": slots, "prep_slots": 2},
                    "started_at": 1, "finished_at": 101,
                    "results": [{"formal_deliveries": 1, "status": "OK"}]}))
                paths.append(path)
            self.assertEqual(compare(paths, confirmed_account_capacity=40)["selected_policy"]["vision_slots"], 20)


if __name__ == "__main__":
    unittest.main()
