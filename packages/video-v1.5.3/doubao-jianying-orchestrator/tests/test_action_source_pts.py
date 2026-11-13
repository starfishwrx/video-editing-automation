import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.media.action_frames import monotonic_pts
from orchestrator.vision.experiment_event_review import chain_format_error


class ActionSourcePtsTests(unittest.TestCase):
    def test_equal_timestamps_from_rounding_stay_in_order(self):
        self.assertEqual(monotonic_pts([0, 33333, 33333, 66667]), [0, 33333, 33334, 66667])

    def test_one_frame_reversal_is_repaired(self):
        self.assertEqual(monotonic_pts([0, 16667, 16666]), [0, 16667, 16668])

    def test_large_reversal_stays_an_error(self):
        with self.assertRaisesRegex(ValueError, "ACTION_SOURCE_PTS_NOT_STRICTLY_INCREASING"):
            monotonic_pts([0, 100_000, 50_000])

    def test_event_chain_format_errors_are_reaskable(self):
        self.assertTrue(chain_format_error(ValueError("EXPERIMENT_EVENTS_COVERAGE_INVALID")))
        self.assertTrue(chain_format_error(ValueError("EXPERIMENT_EVENTS_CONTACT_RELEASE_MARKER_REQUIRED")))
        self.assertFalse(chain_format_error(ValueError("EXPERIMENT_RESULT_WINDOW_TOO_SHORT")))


if __name__ == "__main__":
    unittest.main()
