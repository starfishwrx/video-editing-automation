"""Frame facts cannot acquire a duration goal or invent an unseen source index."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.media import action_frames
from orchestrator.selection import ai_editor, paper_action_events as events
from orchestrator.vision import vision_analyzer

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

class EventContractTests(unittest.TestCase):
    def setUp(self):
        self.frames = [{"index": i, "pts_us": i * 10000} for i in (10, 12, 14, 16)]
        self.observation = {"verdict": "observed", "dry_rubbing": True,
            "observation": "The paper is folded and rubbed; the result is outside this view.",
            "events": [{"name": "onset", "frame_lower": 10, "frame_upper": 12, "fact": "begins folding"},
                       {"name": "first_rub", "frame_lower": 14, "frame_upper": 16, "fact": "relative friction"}]}

    def test_partial_observation_does_not_invent_completion(self):
        row = events.validate_events(self.observation, self.frames)
        self.assertFalse(any(e["name"] == "unfold_complete" for e in row["events"]))

    def test_unseen_and_boolean_frame_indices_rejected(self):
        for index in (11, True, -1, 1000):
            row = deepcopy(self.observation)
            row["events"][0]["frame_lower"] = index
            with self.assertRaisesRegex(ValueError, "FRAME_REFERENCE"):
                events.validate_events(row, self.frames)

    def test_reversed_physical_events_rejected(self):
        row = deepcopy(self.observation)
        row["events"][0].update(frame_lower=16, frame_upper=16)
        row["events"][1].update(frame_lower=10, frame_upper=12)
        with self.assertRaisesRegex(ValueError, "ORDER_REVERSED"):
            events.validate_events(row, self.frames)

    def test_unknown_duplicate_empty_or_untyped_facts_rejected(self):
        for change in ("unknown", "duplicate", "empty", "state"):
            row = deepcopy(self.observation)
            if change == "unknown": row["events"][0]["name"] = "passed"
            if change == "duplicate": row["events"].append(row["events"][0])
            if change == "empty": row["events"][0]["fact"] = ""
            if change == "state": row["dry_rubbing"] = 1
            with self.assertRaises(ValueError): events.validate_events(row, self.frames)

    def test_review_never_sends_timing_goal_or_prior_verdict(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.mp4"
            source.write_bytes(b"a source identity fixture")
            frame = root / "frame.jpg"
            Image.new("RGB", (90, 160), "white").save(frame)
            frames = [dict(f, path=str(frame)) for f in self.frames]
            metadata = {"source_identity": action_frames.identity(source)}
            shot = {"shot_id": "fixture", "video": str(source), "old_verdict": "PASS"}
            manifest = {"product": "paper", "target_duration_us": 1116959, "speed": 3.14}
            with mock.patch.object(ai_editor, "_call", return_value=self.observation) as call:
                row = events.review(manifest, shot, metadata, frames, root / "review", scope="onset")
            prompt, _, sheet = call.call_args.args
            self.assertNotIn("1116959", prompt)
            self.assertNotIn("3.14", prompt)
            self.assertNotIn("old_verdict", prompt)
            self.assertNotIn("target_duration_us", prompt)
            self.assertIn('"index": 10', prompt)
            self.assertTrue(sheet.is_file())
            self.assertEqual(row["reviewed_frames"], self.frames)
            self.assertEqual(row["events"][0]["source_interval_us"], {"start": 100000, "end": 120000})
            self.assertTrue(row["events"][0]["left_censored"])

    def test_source_change_during_review_cannot_be_reused(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); source = root / "source.mp4"; source.write_bytes(b"source")
            frame = root / "frame.jpg"; Image.new("RGB", (90, 160)).save(frame)
            frames = [dict(f, path=str(frame)) for f in self.frames]
            metadata = {"source_identity": action_frames.identity(source)}
            def change(*args):
                source.write_bytes(b"different source content")
                return self.observation
            with mock.patch.object(ai_editor, "_call", side_effect=change):
                with self.assertRaisesRegex(ValueError, "SOURCE_CHANGED"):
                    events.review({}, {"video": str(source), "shot_id": "fixture"}, metadata, frames, root, scope="onset")


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg tools unavailable")
class ActualFrameCoordinateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source pts 中文 ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "sixty fps.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=120x160:rate=60:duration=0.4",
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-y", str(self.source)],
            capture_output=True, check=True, timeout=30)

    def test_real_sixty_fps_source_retains_indices_and_pts(self):
        metadata = action_frames.probe(self.source, self.root / "cache", ffprobe=FFPROBE)
        self.assertEqual(len(metadata["frames"]), 24)
        self.assertEqual(metadata["frames"][6], {"index": 6, "pts_us": 100000})
        frames = action_frames.extract(self.source, metadata, [0, 6, 13, 23], self.root / "cache", ffmpeg=FFMPEG)
        self.assertEqual([f["index"] for f in frames], [0, 6, 13, 23])
        self.assertEqual([f["pts_us"] for f in frames], [0, 100000, 216667, 383333])
        self.assertTrue(all(Path(f["path"]).is_file() for f in frames))
        with mock.patch.object(action_frames, "run_ffmpeg_decode", side_effect=AssertionError("cache missed")):
            self.assertEqual(action_frames.extract(self.source, metadata, [0, 6, 13, 23], self.root / "cache", ffmpeg=FFMPEG), frames)

    def test_incorrect_timestamp_cannot_be_attached_to_decoded_frame(self):
        metadata = action_frames.probe(self.source, self.root / "cache", ffprobe=FFPROBE)
        metadata["frames"][6]["pts_us"] += 30000
        with self.assertRaisesRegex(ValueError, "PTS_MISMATCH"):
            action_frames.extract(self.source, metadata, [6], self.root / "new", ffmpeg=FFMPEG)

    def test_nonzero_source_pts_are_explicitly_normalized(self):
        shifted = self.root / "nonzero pts.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-i", str(self.source), "-vf", "setpts=PTS+1/TB",
            "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-y", str(shifted)],
            capture_output=True, check=True, timeout=30)
        metadata = action_frames.probe(shifted, self.root / "shifted cache", ffprobe=FFPROBE)
        self.assertEqual(metadata["timestamp_origin_us"], 1000000)
        rows = action_frames.extract(shifted, metadata, [0, 6, 23], self.root / "shifted cache", ffmpeg=FFMPEG)
        self.assertEqual([r["pts_us"] for r in rows], [0, 100000, 383333])

    def test_stale_source_and_invalid_frame_requests_fail_before_decode(self):
        metadata = action_frames.probe(self.source, self.root / "cache", ffprobe=FFPROBE)
        for indices in ([1, 1], [3, 2], [True], [24], []):
            with self.assertRaises(ValueError): action_frames.extract(self.source, metadata, indices, self.root / "cache")
        self.source.write_bytes(b"a changed source")
        with self.assertRaisesRegex(ValueError, "IDENTITY_CHANGED"):
            action_frames.extract(self.source, metadata, [0], self.root / "cache")


if __name__ == "__main__":
    unittest.main()
