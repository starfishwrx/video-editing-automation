"""Upright working copies use a capped high-quality rate, never lossless QP 0."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.media import video_encode


class UprightQualityEncodeArgsTests(unittest.TestCase):
    def run_plan(self, encoders, pixel_format="yuv420p"):
        calls = []

        def attempt(name, cmd, timeout):
            calls.append((name, cmd))
            Path(cmd[-1]).write_bytes(b"x")
            return subprocess.CompletedProcess(cmd, 0, "", ""), None

        @contextmanager
        def slot(kind):
            yield types.SimpleNamespace(waited=0.0, acquired=True)

        plan = [video_encode.EncodeAttempt("ffmpeg", name, []) for name in encoders]
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(video_encode, "encoder_plan", return_value=plan), \
                patch.object(video_encode, "_attempt", side_effect=attempt), \
                patch.object(video_encode, "_gpu_skip_reason", return_value=None), \
                patch.object(video_encode.gpu_runtime, "gpu_slot", slot), \
                patch.object(video_encode.gpu_runtime, "record_encode"):
            name, _ = video_encode.run_h264_encode("ffmpeg", ["-y", "-i", "in.mp4"], Path(temp) / "out.mp4",
                quality_bitrate=24_000_000, quality_maxrate=30_000_000, pixel_format=pixel_format)
        return name, calls

    def test_nvenc_uses_constant_quality_with_source_relative_cap(self):
        name, calls = self.run_plan(["h264_nvenc", "libx264"])
        cmd = calls[0][1]
        self.assertEqual(name, "h264_nvenc")
        self.assertNotIn("lossless", cmd)
        self.assertNotIn("constqp", cmd)
        self.assertEqual(cmd[cmd.index("-rc") + 1], "vbr")
        self.assertEqual(cmd[cmd.index("-cq") + 1], str(video_encode.QUALITY_NVENC_CQ))
        self.assertEqual(cmd[cmd.index("-maxrate") + 1], "30000000")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p")

    def test_libx264_uses_crf_with_cap_and_keeps_10bit(self):
        name, calls = self.run_plan(["h264_nvenc", "libx264"], pixel_format="yuv420p10le")
        self.assertEqual(name, "libx264")  # NVENC H.264 cannot carry 10-bit; skipped.
        cmd = calls[0][1]
        self.assertEqual(cmd[cmd.index("-crf") + 1], str(video_encode.QUALITY_X264_CRF))
        self.assertNotIn("-qp", cmd)
        self.assertEqual(cmd[cmd.index("-maxrate") + 1], "30000000")
        self.assertEqual(cmd[cmd.index("-pix_fmt") + 1], "yuv420p10le")

    def test_rate_capped_copy_never_falls_back_to_uncontrolled_encoders(self):
        with self.assertRaises(Exception):
            self.run_plan(["h264_mf", "mpeg4"])


if __name__ == "__main__":
    unittest.main()
