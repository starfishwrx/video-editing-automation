import json
from pathlib import Path
import tempfile
import unittest

from tools.throughput_calibration import compare


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
