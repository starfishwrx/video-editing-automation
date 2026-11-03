"""The media adapter is independently usable and preserves legacy patch points."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))

from orchestrator.media import frames
from orchestrator.selection import shot_analyzer


class MediaBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="media_boundary_")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_adapter_import_does_not_load_selection_vision_or_application(self):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c",
             "import json,sys; from orchestrator.media import frames; "
             "print(json.dumps([n for n in sys.modules if "
             "n.startswith(('orchestrator.selection', 'orchestrator.vision', "
             "'orchestrator.application'))]))"],
            cwd=SKILL, text=True, encoding="utf-8", capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), [])

    def test_probe_uses_injected_runner_and_ffmpeg_fallback(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if command[0] == "probe-test":
                raise subprocess.CalledProcessError(1, command)
            return subprocess.CompletedProcess(command, 1, "", "Duration: 00:01:02.750")

        duration = frames.probe_duration(
            Path("source.mp4"), ffprobe="probe-test", ffmpeg="ffmpeg-test", runner=runner)
        self.assertEqual(duration, 62.75)
        self.assertEqual([command[0] for command in calls], ["probe-test", "ffmpeg-test"])
        with mock.patch.object(shot_analyzer.subprocess, "run", side_effect=runner):
            legacy = shot_analyzer.probe_duration(
                Path("source.mp4"), ffprobe="probe-test", ffmpeg="ffmpeg-test")
        self.assertEqual(legacy, duration)

    def test_scene_detection_matches_legacy_and_deduplicates_pts(self):
        def decoder(command, **kwargs):
            return subprocess.CompletedProcess(
                command, 0, "", "pts_time:1.250\npts_time:0\npts_time:1.25\npts_time:2.5")

        direct = frames.detect_scene_boundaries(Path("source.mp4"), decoder=decoder)
        with mock.patch.object(shot_analyzer, "run_ffmpeg_decode", side_effect=decoder):
            legacy = shot_analyzer.detect_scene_boundaries(Path("source.mp4"))
        self.assertEqual(direct, [1.25, 2.5])
        self.assertEqual(legacy, direct)

    def test_single_frame_matches_legacy_decoder_and_output_bytes(self):
        calls = []

        def decoder(command, **kwargs):
            calls.append(command)
            Path(command[-1]).write_bytes(b"frame-fixture")
            return subprocess.CompletedProcess(command, 0)

        direct = self.root / "direct.jpg"
        legacy = self.root / "legacy.jpg"
        self.assertTrue(frames.extract_frame(Path("source.mp4"), 4.25, direct, decoder=decoder))
        with mock.patch.object(shot_analyzer, "run_ffmpeg_decode", side_effect=decoder):
            self.assertTrue(shot_analyzer.extract_frame(Path("source.mp4"), 4.25, legacy))
        self.assertEqual(direct.read_bytes(), legacy.read_bytes())
        self.assertEqual(calls[0][:-1], calls[1][:-1])

    def test_batch_preserves_source_pts_and_duplicate_outputs(self):
        def decoder(command, **kwargs):
            seek = float(command[command.index("-ss") + 1])
            self.assertAlmostEqual(seek, 89.95)
            work = Path(command[-1]).parent
            (work / "f_0001.jpg").write_bytes(b"first")
            (work / "f_0002.jpg").write_bytes(b"second")
            return subprocess.CompletedProcess(command, 0, "", "pts_time:0.25\npts_time:0.85")

        direct_requests = [(90.2, self.root / "direct-a.jpg"),
                           (90.8, self.root / "direct-b.jpg"),
                           (90.2, self.root / "direct-c.jpg")]
        legacy_requests = [(timestamp, self.root / f"legacy-{index}.jpg")
                           for index, (timestamp, _) in enumerate(direct_requests)]
        direct = frames.extract_frames_batch(
            Path("source.mp4"), direct_requests, decoder=decoder, batch_enabled=True)
        with mock.patch.object(shot_analyzer, "run_ffmpeg_decode", side_effect=decoder), \
             mock.patch.object(shot_analyzer, "batch_extract_enabled", return_value=True), \
             mock.patch.object(shot_analyzer, "extract_frame") as fallback:
            legacy = shot_analyzer.extract_frames_batch(Path("source.mp4"), legacy_requests)
        self.assertEqual(direct, [True, True, True])
        self.assertEqual(legacy, direct)
        fallback.assert_not_called()
        self.assertEqual([path.read_bytes() for _, path in direct_requests],
                         [path.read_bytes() for _, path in legacy_requests])
        self.assertEqual(direct_requests[0][1].read_bytes(), direct_requests[2][1].read_bytes())
        self.assertFalse(list(self.root.glob(".batch_*")))

    def test_disabled_batch_uses_injected_single_frame_without_decoder(self):
        calls = []

        def single(source, timestamp, output, **kwargs):
            calls.append((source, timestamp, output))
            return timestamp == 1.0

        requests = [(1.0, self.root / "first.jpg"), (2.0, self.root / "second.jpg")]
        with mock.patch.object(frames, "run_ffmpeg_decode") as decoder:
            flags = frames.extract_frames_batch(
                Path("source.mp4"), requests, single_frame=single, batch_enabled=False)
        self.assertEqual(flags, [True, False])
        self.assertEqual([call[1] for call in calls], [1.0, 2.0])
        decoder.assert_not_called()
        with mock.patch.object(shot_analyzer, "batch_extract_enabled", return_value=False), \
             mock.patch.object(shot_analyzer, "extract_frame", side_effect=single) as legacy:
            flags = shot_analyzer.extract_frames_batch(Path("source.mp4"), requests)
        self.assertEqual(flags, [True, False])
        self.assertEqual(legacy.call_count, 2)


if __name__ == "__main__":
    unittest.main()
