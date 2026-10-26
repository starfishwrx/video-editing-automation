"""Batch multi-timestamp frame extraction tests."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import frame_quality, shot_analyzer, vision_analyzer  # noqa: E402


def _make_test_video(path: Path, seconds: float = 3.0, color: str = "blue") -> Path:
    """Synthetic mp4 via ffmpeg color source — no NAS dependency."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", f"color=c={color}:s=320x180:d={seconds}:r=25",
        "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


class ExtractFramesBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="frame_batch_")
        cls.root = Path(cls.tmp.name)
        try:
            cls.video = _make_test_video(cls.root / "clip.mp4", seconds=4.0)
        except (OSError, subprocess.SubprocessError) as exc:
            raise unittest.SkipTest(f"ffmpeg unavailable for synthetic clip: {exc}") from exc

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"JY_FRAME_BATCH": "1"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_batch_writes_all_requested_outputs(self):
        out_dir = self.root / "batch_ok"
        out_dir.mkdir(exist_ok=True)
        reqs = [(0.2, out_dir / "a.jpg"), (1.5, out_dir / "b.jpg"), (3.2, out_dir / "c.jpg")]
        flags = shot_analyzer.extract_frames_batch(self.video, reqs)
        self.assertEqual(flags, [True, True, True])
        for _, path in reqs:
            self.assertTrue(path.is_file() and path.stat().st_size > 0)

    def test_disable_falls_back_to_single_extract(self):
        os.environ["JY_FRAME_BATCH"] = "0"
        out_dir = self.root / "batch_off"
        out_dir.mkdir(exist_ok=True)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            # Emulate success by touching the output path (last arg).
            Path(cmd[-1]).write_bytes(b"jpg")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        reqs = [(0.5, out_dir / "x.jpg"), (1.5, out_dir / "y.jpg")]
        with mock.patch.object(shot_analyzer, "run_ffmpeg_decode", side_effect=fake_run):
            flags = shot_analyzer.extract_frames_batch(self.video, reqs)
        self.assertEqual(flags, [True, True])
        # One ffmpeg invocation per timestamp when batch is off.
        self.assertEqual(len(calls), 2)

    def test_batch_uses_single_ffmpeg_for_many_timestamps(self):
        out_dir = self.root / "batch_once"
        out_dir.mkdir(exist_ok=True)
        reqs = [(0.4, out_dir / "1.jpg"), (1.2, out_dir / "2.jpg"), (2.0, out_dir / "3.jpg")]
        real_run = shot_analyzer.run_ffmpeg_decode
        calls = []

        def counting_run(cmd, **kwargs):
            calls.append(cmd)
            return real_run(cmd, **kwargs)

        with mock.patch.object(shot_analyzer, "run_ffmpeg_decode", side_effect=counting_run):
            flags = shot_analyzer.extract_frames_batch(self.video, reqs)
        self.assertTrue(all(flags))
        # One batch decode; no per-frame fallback expected on a clean synthetic clip.
        self.assertEqual(len(calls), 1)
        self.assertTrue(any("select=" in str(part) for part in calls[0]))


class BatchVsyncRetryTests(unittest.TestCase):
    """Older ffmpeg rejects ``-fps_mode``. An empty work dir must still retry ``-vsync``."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="frame_vsync_")
        self.root = Path(self.tmp.name)
        self.video = self.root / "clip.mp4"
        self.video.write_bytes(b"not-a-real-video")
        self.env = mock.patch.dict(os.environ, {"JY_FRAME_BATCH": "1"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _run(self, reqs, fake_run):
        with mock.patch.object(shot_analyzer, "run_ffmpeg_decode", side_effect=fake_run):
            return shot_analyzer.extract_frames_batch(self.video, reqs)

    def test_fps_mode_failure_with_no_outputs_retries_vsync(self):
        out_dir = self.root / "retry"
        out_dir.mkdir()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "-fps_mode" in cmd:
                # No frames and a non-zero exit. The retry must not depend on
                # a particular stderr phrase: bool(Path.glob) used to skip it.
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="Conversion failed\n")
            if "-vsync" in cmd:
                work = Path(cmd[-1]).parent
                (work / "f_0001.jpg").write_bytes(b"\xff\xd8")
                (work / "f_0002.jpg").write_bytes(b"\xff\xd8")
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="", stderr="pts_time:0.4\npts_time:1.6\n")
            Path(cmd[-1]).write_bytes(b"\xff\xd8")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        reqs = [(0.4, out_dir / "a.jpg"), (1.6, out_dir / "b.jpg")]
        flags = self._run(reqs, fake_run)
        self.assertEqual(flags, [True, True])
        vfr = []
        for cmd in calls:
            if "-fps_mode" in cmd:
                vfr.append("fps_mode")
            elif "-vsync" in cmd:
                vfr.append("vsync")
            else:
                vfr.append("other")
        self.assertEqual(vfr, ["fps_mode", "vsync"])
        for _, path in reqs:
            self.assertTrue(path.is_file() and path.stat().st_size > 0)

    def test_fps_mode_with_outputs_does_not_retry_vsync(self):
        out_dir = self.root / "keep"
        out_dir.mkdir()
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            work = Path(cmd[-1]).parent
            (work / "f_0001.jpg").write_bytes(b"\xff\xd8")
            (work / "f_0002.jpg").write_bytes(b"\xff\xd8")
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="pts_time:0.4\npts_time:1.6\n")

        reqs = [(0.4, out_dir / "a.jpg"), (1.6, out_dir / "b.jpg")]
        flags = self._run(reqs, fake_run)
        self.assertEqual(flags, [True, True])
        self.assertEqual(len(calls), 1)
        self.assertIn("-fps_mode", calls[0])
        self.assertNotIn("-vsync", calls[0])


class SelectFramesBatchWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="select_batch_")
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(
            os.environ, {"JY_FRAME_QUALITY": "1", "JY_FRAME_BATCH": "1"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_select_frames_batches_centers_then_alts(self):
        batches = []

        def extract_batch(video, requests, ffmpeg=None):
            batches.append(list(requests))
            flags = []
            for ts, out in requests:
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                # First wave (centers): paint black so quality gate asks for alts.
                # Second wave (alts): paint textured so swap succeeds.
                from PIL import Image, ImageDraw
                if len(batches) == 1:
                    Image.new("RGB", (80, 45), (0, 0, 0)).save(out)
                else:
                    image = Image.new("RGB", (80, 45), (30, 30, 30))
                    draw = ImageDraw.Draw(image)
                    for x in range(0, 80, 4):
                        draw.line([(x, 0), (x, 45)], fill=(220, 220, 220))
                    image.save(out)
                flags.append(True)
            return flags

        def extract(video, ts, out, ffmpeg=None):
            return extract_batch(video, [(ts, out)], ffmpeg=ffmpeg)[0]

        jobs = [
            {"center": 1.0, "output": self.root / "a.jpg", "start": 0.0, "end": 3.0,
             "half_width": 1.0},
            {"center": 2.0, "output": self.root / "b.jpg", "start": 0.0, "end": 3.0,
             "half_width": 1.0},
        ]
        picked = frame_quality.select_frames(
            Path("video.mp4"), jobs, extract=extract, extract_batch=extract_batch)
        self.assertEqual(len(batches), 2)  # centers, then alts
        self.assertEqual(len(batches[0]), 2)
        self.assertTrue(all(row.get("ok") and row.get("replaced") for row in picked))

    def test_sample_frames_uses_batch_extract(self):
        video = self.root / "clip.mp4"
        video.write_bytes(b"x")
        frame_dir = self.root / "frames"
        frame_dir.mkdir()
        calls = {"batch": 0}

        def extract_batch(path, requests, ffmpeg=None):
            calls["batch"] += 1
            flags = []
            for ts, out in requests:
                from PIL import Image, ImageDraw
                image = Image.new("RGB", (80, 45), (40, 40, 40))
                draw = ImageDraw.Draw(image)
                for x in range(0, 80, 3):
                    draw.line([(x, 0), (x, 45)], fill=(200, 200, 200))
                Path(out).parent.mkdir(parents=True, exist_ok=True)
                image.save(out)
                flags.append(True)
            return flags

        def extract(path, ts, out, ffmpeg=None):
            return extract_batch(path, [(ts, out)], ffmpeg=ffmpeg)[0]

        shot = {"shot_id": "s1", "video": str(video),
                "source_start": 0.0, "source_end": 6.0}
        with mock.patch.object(vision_analyzer, "sample_times", return_value=[0.5, 2.0, 4.0]), \
             mock.patch("orchestrator.shot_analyzer.extract_frames_batch", side_effect=extract_batch), \
             mock.patch("orchestrator.shot_analyzer.extract_frame", side_effect=extract):
            frames = vision_analyzer._sample_frames(shot, frame_dir, ffmpeg=None)
        self.assertGreaterEqual(len(frames), 2)
        self.assertGreaterEqual(calls["batch"], 1)


if __name__ == "__main__":
    unittest.main()
