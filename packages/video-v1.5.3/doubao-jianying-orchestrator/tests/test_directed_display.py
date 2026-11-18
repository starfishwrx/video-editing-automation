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


class WholeOrderDecisionTests(unittest.TestCase):
    def setUp(self):
        import wave
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name)
        self.audio = self.directory / "line.wav"
        with wave.open(str(self.audio), "wb") as stream:
            stream.setparams((1, 2, 8000, 8000, "NONE", "NONE"))
            stream.writeframes(bytes(16000))
        self.source = str(self.directory / "subject.mp4")
        text = "包装标有四片"
        self.manifest = {"category": "weipin", "material_sources": [self.source], "segments": [{"text": text}]}
        self.analysis = {"shots": [{"shot_id": "new", "video": self.source, "status": "ready_for_matching",
                                   "source_start": 0, "source_end": 4}]}
        self.timing = {"rows": [{"tts_path": str(self.audio), "audio_duration_us": 1_000_000,
                                "timing_certainty": "audio", "text": text,
                                "key": ai_editor.timing_contract.sentence_key(self.manifest["segments"][0], 1)}]}
        self.sentence = {"text": text, "index": 1, "kind": "selling_point", "selling_points": [{"id": "p1", "text": "四片"}]}
        self.inventory = self.enterContext(patch.object(ai_editor, "_inventory", return_value=[self.sentence]))
        self.enterContext(patch("orchestrator.audio.narration_probe.probe_audio", return_value={"duration_us": 1_000_000}))
        self.enterContext(patch("orchestrator.runtime.platform_env.find_ffmpeg", return_value=("ffmpeg", "ffprobe")))
        directed_display.freeze_intake(self.manifest, self.analysis, self.timing, self.directory)
        self.plan = {"schema": ai_editor.contract.PLAN_SCHEMA, "identity": ai_editor.plan_identity(
            self.manifest, self.analysis, self.timing), "sentences": [deepcopy(self.sentence)],
            "rows": [{"index": 1}], "reviews": {}, "display_reviews": []}

    def window(self):
        return directed_display.review_window(self.manifest, self.analysis, self.plan, self.timing,
            index=1, shot_id="new", start_us=0, directory=self.directory, inventory_dir=self.directory)

    def match(self):
        return directed_display.match_reviewed(self.manifest, self.analysis, self.plan, self.timing,
                                              inventory_dir=self.directory)

    def test_fresh_candidate_registers_observation_and_preserves_exact_window(self):
        def observe(manifest, plan, *args, require_current_sentence):
            self.assertTrue(require_current_sentence)
            plan["window_reviews"] = {"verified": {"verdict": "matched", "shot_id": "new"}}
            return "verified"
        with patch.object(ai_editor, "_review_proposed_window", side_effect=observe):
            self.plan["rows"] = [self.window()]
        self.assertEqual(self.plan["reviews"]["1"], [self.plan["window_reviews"]["verified"]])
        actual = {"video": self.source, "shot_id": "new", "ai_selection": {"window_us": {"start": 0, "end": 1_000_000}}}
        with patch.object(ai_editor, "_assemble", return_value={"ok": True, "segments": [actual]}):
            self.assertTrue(self.match()["ok"])
            actual["ai_selection"]["window_us"]["start"] = 1
            with self.assertRaisesRegex(ValueError, "DIRECTED_WINDOW_CHANGED"):
                self.match()
        self.inventory.assert_called_once()

    def test_multi_claim_ids_cannot_be_reassigned(self):
        points = [{"id": "p1", "text": "五层"}, {"id": "p2", "text": "柔软"}]
        first = [{**self.sentence, "selling_points": points}]
        swapped = [{**self.sentence, "selling_points": [
            {"id": "p2", "text": "五层"}, {"id": "p1", "text": "柔软"}]}]
        self.assertNotEqual(directed_display._sentence_contract(first), directed_display._sentence_contract(swapped))
        self.assertEqual(directed_display.start_plan(self.manifest, self.analysis, self.timing, self.directory)["rows"],
                         [{"index": 1, "parts": []}])

    def test_same_duration_audio_swap_and_semantic_downgrade_fail_before_review(self):
        self.plan["sentences"][0].update(kind="product_intro", selling_points=[])
        with self.assertRaisesRegex(ValueError, "SENTENCE_CONTRACT_CHANGED"):
            self.match()
        self.plan["sentences"] = [self.sentence]
        with self.audio.open("r+b") as stream:
            stream.seek(-1, 2)
            stream.write(bytes([1]))
        with patch.object(ai_editor, "_review_proposed_window") as pixels:
            with self.assertRaisesRegex(ValueError, "DIRECTED_INTAKE_CHANGED"):
                self.window()
            pixels.assert_not_called()
