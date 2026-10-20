"""Identity/safety checks for local pilot outputs, without a model or FFmpeg."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from tools.v154_pilot import freeze_inputs, read_json, verify_frozen, write_json
from tools.v154_review import readback_plan, render_groups, review_document, validate_feedback, write_review


class FrozenInputsTests(unittest.TestCase):
    def test_freeze_copies_bytes_and_removes_historical_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video, audio = root / "video.mov", root / "voice.ogg"
            video.write_bytes(b"real-video-test-fixture")
            audio.write_bytes(b"real-audio-test-fixture")
            manifest = root / "input.json"
            analysis = root / "analysis.json"
            write_json(manifest, {"voice": "historical display name", "contextual_plan": {"old": True},
                "material_sources": [str(video)], "segments": [{"text": "sample",
                "audio": str(audio), "audio_duration_us": 1000000}]})
            write_json(analysis, {"shots": [{"video": str(video), "description": "observed"}]})
            receipt = freeze_inputs(manifest, analysis, root / "frozen")
            frozen = read_json(root / "frozen" / "manifest.json")
            self.assertNotIn("contextual_plan", frozen)
            self.assertEqual(frozen["segments"][0]["audio_mode"], "file")
            self.assertEqual(frozen["voice"], "historical display name")
            self.assertEqual(receipt["shot_count"], 1)
            self.assertFalse(receipt["human_accepted"])
            verify_frozen(root / "frozen")
            Path(frozen["segments"][0]["audio"]).write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "asset changed"):
                verify_frozen(root / "frozen")


class ReviewEvidenceTests(unittest.TestCase):
    def _draft(self):
        return {"duration": 1000000, "materials": {"videos": [{"id": "v", "path": "clip.mov",
            "duration": 2000000}], "audios": [], "texts": []}, "tracks": [{"type": "video",
            "segments": [{"material_id": "v", "target_timerange": {"start": 0, "duration": 1000000},
                          "source_timerange": {"start": 0, "duration": 1000000}, "speed": 1}]}]}

    def test_readback_rejects_gap_and_source_overrun(self):
        draft = self._draft()
        self.assertEqual(readback_plan(draft)["video"][0]["frames"], 30)
        draft["tracks"][0]["segments"][0]["target_timerange"]["start"] = 100000
        with self.assertRaisesRegex(ValueError, "gap"):
            readback_plan(draft)
        draft = self._draft()
        draft["tracks"][0]["segments"][0]["source_timerange"]["start"] = 1500000
        with self.assertRaisesRegex(ValueError, "exceeds"):
            readback_plan(draft)

    def test_unknown_transforms_are_not_silently_ignored(self):
        draft = self._draft()
        draft["tracks"][0]["segments"][0]["clip"] = {"scale": {"x": 1.2, "y": 1.2}}
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            readback_plan(draft)
        draft = self._draft()
        draft["materials"]["audios"] = [{"id": "a", "path": "voice.ogg"}]
        draft["tracks"].append({"type": "audio", "segments": [{"material_id": "a",
            "target_timerange": {"start": 0, "duration": 1100000},
            "source_timerange": {"start": 0, "duration": 1100000}}]})
        with self.assertRaisesRegex(ValueError, "Audio is outside"):
            readback_plan(draft)

    def test_continuous_source_resamples_once_without_rewriting_saved_rows(self):
        rows = [
            {"path": "continuous.mov", "source_start_us": 700000, "source_duration_us": 2231976,
             "target_start_us": 10508671, "target_duration_us": 1345605, "frames": 41,
             "speed": 1.6587156778790026, "brightness": .08, "flip_h": False},
            {"path": "continuous.mov", "source_start_us": 2931976, "source_duration_us": 1948024,
             "target_start_us": 11854276, "target_duration_us": 1174417, "frames": 35,
             "speed": 1.6587156778790026, "brightness": .08, "flip_h": False},
        ]
        plan = {"video": rows, "total_frames": 76,
                "audio": [{"path": "original-audio.ogg", "start_us": 10508671}],
                "subtitles": [{"text": "第一句"}, {"text": "下一句连续播放"}]}
        original = copy.deepcopy(plan)
        groups = render_groups(plan)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["video_indices"], [0, 1])
        self.assertEqual(groups[0]["frames"], 76)
        self.assertEqual(groups[0]["source_start_us"], 700000)
        self.assertEqual(groups[0]["source_duration_us"], 4180000)
        self.assertEqual(groups[0]["target_duration_us"], 2520022)
        self.assertEqual(plan, original)

    def test_render_group_never_bridges_source_jumps_or_different_video_settings(self):
        first = {"path": "continuous.mov", "source_start_us": 0, "source_duration_us": 1000000,
                 "target_start_us": 0, "target_duration_us": 1000000, "frames": 30,
                 "speed": 1, "brightness": .08, "flip_h": False}
        next_row = {**first, "source_start_us": 1000000, "target_start_us": 1000000}
        for change in ({"source_start_us": 1100000}, {"source_start_us": 900000},
                       {"path": "other.mov"}, {"speed": 2}, {"brightness": .1}, {"flip_h": True}):
            with self.subTest(change=change):
                plan = {"video": [first, {**next_row, **change}], "total_frames": 60}
                self.assertEqual(len(render_groups(plan)), 2)
        invalid = {"video": [first, {**next_row, "frames": 29}], "total_frames": 59}
        with self.assertRaisesRegex(ValueError, "global frame coverage"):
            render_groups(invalid)

    def test_feedback_cannot_attach_to_changed_source_or_plan(self):
        document = review_document({"title": "example"}, arms=[{"name": "B", "plan": {"groups": []},
                    "source_sha256s": ["source"]}], cases=[{"case_id": "C1", "source_sha256": "source",
                    "case_identity_sha256": "case-hash"}])
        arm = document["arms"][0]
        feedback = {"review_identity_sha256": document["review_identity_sha256"],
                    "items": [{"arm": "B", "plan_sha256": arm["plan_sha256"],
                               "source_sha256s": ["source"], "verdict": "reject"}]}
        validate_feedback(document, feedback)
        feedback["items"][0]["source_sha256s"] = ["different"]
        with self.assertRaisesRegex(ValueError, "source"):
            validate_feedback(document, feedback)
        feedback["items"][0]["source_sha256s"] = ["source"]
        feedback["items"][0]["plan_sha256"] = "changed"
        with self.assertRaisesRegex(ValueError, "plan"):
            validate_feedback(document, feedback)
        feedback["items"][0]["plan_sha256"] = arm["plan_sha256"]
        feedback["cases"] = [{"case_id": "C1", "source_sha256": "source",
                               "case_identity_sha256": "case-hash", "label": "DIRECT"}]
        validate_feedback(document, feedback)
        feedback["cases"][0]["source_sha256"] = "another-video"
        with self.assertRaisesRegex(ValueError, "case/source"):
            validate_feedback(document, feedback)

    def test_html_keeps_caption_data_inert_and_pending_cases_distinct(self):
        document = review_document({"title": "<unsafe>", "full_script": "</script><script>alert(1)</script>"},
            arms=[], cases=[{"case_id": "C1", "truth_status": "PENDING_HUMAN_REVIEW"}])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "review.html"
            write_review(document, path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("&lt;unsafe&gt;", text)
            self.assertNotIn("</script><script>alert(1)", text)
            self.assertIn("PENDING_HUMAN_REVIEW", text)
            self.assertEqual(document["human_review_status"], "PENDING")


if __name__ == "__main__":
    unittest.main()
