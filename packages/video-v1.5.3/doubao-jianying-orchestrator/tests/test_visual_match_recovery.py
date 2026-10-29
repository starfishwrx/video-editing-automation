"""Cached visual observations can outlive their temporary frame artifacts."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from orchestrator import visual_match


class VisualFrameRecoveryTests(unittest.TestCase):
    def test_missing_cached_frame_is_reextracted_at_audited_timestamp(self):
        with tempfile.TemporaryDirectory() as root:
            report = Path(root)
            segment = {"text": "湿水不破", "video": "source.mp4", "evidence_intervals": [
                {"start": 1.0, "end": 3.0, "frame_time": 2.5, "frame_path": str(report / "old.jpg")}]}
            def extract(_ffmpeg, _video, _timestamp, output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"frame")
                return True
            with mock.patch.object(visual_match, "_extract_frame", side_effect=extract) as call:
                result = visual_match.validate_source_intervals([segment], probe=lambda _: {"duration": 4},
                                                               report_dir=report, ffmpeg="ffmpeg")
            self.assertTrue(result["ok"], result)
            self.assertEqual(call.call_args.args[2], 2.5)
            self.assertTrue(result["records"][0]["evidence_intervals"][0]["frame_recreated"])

    def test_invalid_interval_does_not_trigger_extraction(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(visual_match, "_extract_frame") as extract:
            segment = {"text": "湿水不破", "video": "source.mp4", "evidence_intervals": [
                {"start": 1.0, "end": 9.0, "frame_path": "missing.jpg"}]}
            result = visual_match.validate_source_intervals([segment], probe=lambda _: {"duration": 4},
                                                           report_dir=Path(root), ffmpeg="ffmpeg")
        self.assertFalse(result["ok"])
        extract.assert_not_called()


if __name__ == "__main__":
    unittest.main()
