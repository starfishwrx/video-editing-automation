"""Exercise the opt-in single-sentence adapter without network or source decoding."""
from copy import deepcopy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.selection import ai_editor, directed_display


class DirectedDisplayTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.source = str(self.directory / "candidate.mp4")
        self.shot = {"shot_id": "candidate", "video": self.source, "status": "ready_for_matching",
                     "source_start": 0, "source_end": 2}
        self.manifest = {"material_sources": [self.source],
                         "segments": [{"text": "第一句。"}, {"text": "第二句。"}]}
        self.original = [{"video": video, "source_start": 0, "source_end": 1, "audio_duration_us": 1_000_000}
                         for video in ("first.mp4", "second.mp4")]
        self.plan = {"rows": [{"index": i, "parts": [{"shot_id": f"old-{i}"}]} for i in (1, 2)],
                     "sentences": [{"index": i, "kind": "product_intro", "selling_points": [], "text": s["text"]}
                                   for i, s in enumerate(self.manifest["segments"], 1)],
                     "_frozen_segments": {1: self.original[0]}, "display_reviews": []}
        self.start_us = 250_000

        def assemble(manifest, analysis, plan, **kwargs):
            rows = deepcopy(self.original)
            part = plan["rows"][1]["parts"][0]
            if part["shot_id"] == "candidate":
                start = part["source_start_us"] / 1e6
                rows[1].update(video=self.source, source_start=start, source_end=start + 1)
            return {"segments": rows}

        self.enterContext(patch.object(ai_editor, "_assemble", side_effect=assemble))
        self.review = self.enterContext(patch.object(ai_editor, "_review", return_value={
            "shot_id": "candidate", "verdict": "matched", "same_product": True}))
        self.review_cut = self.enterContext(patch.object(ai_editor, "_review_proposed_window", return_value="new-cut"))

    def apply(self):
        return directed_display.apply(self.manifest, {"shots": [self.shot]}, self.plan, {}, index=2,
                                      shot_id="candidate", start_us=self.start_us, directory=self.directory)

    def test_actual_window_and_original_plan(self):
        old_plan = deepcopy(self.plan)
        match = self.apply()
        self.assertEqual(old_plan, self.plan)
        self.assertEqual(match["segments"][0], self.original[0])
        self.assertEqual(match["segments"][1]["source_start"], 0.25)
        args = self.review_cut.call_args.args
        self.assertTrue(args[3]["require_sentence_fit"])
        self.assertEqual(args[-1], (250_000, 1_250_000))

    def test_failed_exact_cut_cannot_generate_a_match(self):
        self.review_cut.return_value = None
        with self.assertRaisesRegex(ValueError, "EXACT_CUT_NOT_CONFIRMED"):
            self.apply()
        self.assertEqual(self.plan["display_reviews"], [])

    def test_rejects_unfrozen_or_action_bound_edit(self):
        self.plan["_frozen_segments"].clear()
        with self.assertRaisesRegex(ValueError, "FROZEN_SCOPE_REQUIRED"):
            self.apply()
        self.plan["_frozen_segments"] = {1: self.original[0]}
        self.plan["display_reviews"] = [{"shot_id": "old-2", "presentation_observation": {"reveal_us": 500_000}}]
        with self.assertRaisesRegex(ValueError, "REQUIRES_INDEPENDENT_DISPLAY"):
            self.apply()
        self.review.assert_not_called()

    def test_rejects_foreign_source_bad_window_and_unverified_product(self):
        self.manifest["material_sources"] = []
        with self.assertRaisesRegex(ValueError, "SOURCE_OUTSIDE_TASK"):
            self.apply()
        self.manifest["material_sources"] = [self.source]
        self.start_us = 1_500_000
        with self.assertRaisesRegex(ValueError, "WINDOW_OUTSIDE_SOURCE"):
            self.apply()
        self.review.assert_not_called()
        self.start_us = 250_000
        self.review.return_value["same_product"] = "yes"
        with self.assertRaisesRegex(ValueError, "SOURCE_NOT_CONFIRMED"):
            self.apply()
        self.review_cut.assert_not_called()
