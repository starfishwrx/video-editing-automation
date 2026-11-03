"""Paper production integration; synthetic model responses do not measure vision accuracy."""
import copy
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_ai_editor as fixtures
from orchestrator import ai_editor as editor, ai_editor_contract as contract, ai_editor_qc, rulepack
from orchestrator.engine import OrchestrationEngine, Clip
from orchestrator.production_parameters import subtitle_transform, gap_errors, silence_edges


class PaperProductionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AIEditorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.manifest.update(category="zhipin", product="合成棉柔巾")

    def test_subtitle_pixel_conversion_and_invalid_coordinates(self):
        config = {"subtitle_position_px": {"x": 0, "y": -528}}
        self.assertAlmostEqual(subtitle_transform(config, 1080, 1920)["y"], -0.55)
        self.assertEqual(subtitle_transform({}, 1080, 1920), {"x": 0, "y": -.55})
        for bad in ({"x": 0, "y": -2000}, {"x": False, "y": -800}, {"x": 0, "y": float("nan")}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                subtitle_transform({"subtitle_position_px": bad}, 1080, 1920)
        with self.assertRaisesRegex(ValueError, "OUT_OF_CANVAS"):
            subtitle_transform({"subtitle_position_px": {"x": 0, "y": -800}}, 1920, 1080)

    def test_actual_gap_includes_silence_inside_files_and_timeline_gap(self):
        rows = [{"sentence": 0, "start_s": 0, "end_s": 2, "leading_s": 0, "trailing_s": .08},
                {"sentence": 1, "start_s": 2.02, "end_s": 3, "leading_s": .08, "trailing_s": 0}]
        self.assertEqual(gap_errors(rows, fps=30)[0]["gap_frames"], 5.4)
        rows[1]["start_s"] = 2
        self.assertEqual(gap_errors(rows, fps=30), [])
        rows[1]["sentence"] = 0
        rows[1]["leading_s"] = .5
        self.assertEqual(gap_errors(rows, fps=30), [])

    def test_silence_probe_is_measured_and_failure_is_not_zero_gap(self):
        run = SimpleNamespace(returncode=0, stdout="out_time_us=2000000", stderr="silence_start: 0\nsilence_end: 0.03\nsilence_start: 1.9\nsilence_end: 2.0")
        with mock.patch("orchestrator.production_parameters.subprocess.run", return_value=run) as call:
            result = silence_edges(str(self.fixture.video), start_s=0, duration_s=2, ffmpeg="test")
        self.assertEqual(result["leading_s"], .03)
        self.assertAlmostEqual(result["trailing_s"], .1)
        self.assertIn("silencedetect=noise=-40dB:d=0.005", call.call_args.args[0])
        with mock.patch("orchestrator.production_parameters.subprocess.run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaisesRegex(ValueError, "PROBE_FAILED"):
                silence_edges(str(self.fixture.video), start_s=0, duration_s=2, ffmpeg="test")

    def test_hash_metadata_is_optional_but_policy_version_still_applies(self):
        result = self.fixture.result()
        self.assertTrue(result["ok"], result)
        changed = self.fixture.plan()
        changed["identity"]["editorial_rules"]["sha256"] = "0" * 64
        self.assertTrue(self.fixture.result(changed)["ok"])
        item = copy.deepcopy(result["segments"][0])
        item["ai_selection"]["policy_version"] = "selling-points-first-v1"
        self.assertFalse(contract.validate_segment_receipt(item)["ok"])

    def test_paper_context_reaches_selector_and_blind_qc(self):
        fixture = self.fixture
        context = contract.editorial_context(fixture.manifest)
        identifiers = [s["id"] for s in context["rules"]["semantic_contracts"]]
        self.assertEqual(len(identifiers), 13)
        prompts = []
        def inventory(prompt, _path):
            prompts.append(prompt)
            return {"sentences": [{**fixture.sentence, "scene_ids": []}], "display_pool_ids": [fixture.shot["shot_id"]]}
        with mock.patch.object(editor, "_call", side_effect=inventory):
            editor._inventory(fixture.manifest, [fixture.shot], fixture.root)
        qc_prompt = ai_editor_qc.prompt([{"claim": "包装标注0荧光", "product": "合成棉柔巾", "category": "zhipin",
            "selection_score": "SECRET_SCORE", "reason": "SECRET_REASON", "scene_ids": ["SECRET_SCENE"]}], 8)
        for identifier in identifiers:
            self.assertIn(identifier, prompts[0])
            self.assertIn(identifier, qc_prompt)
        for secret in ("SECRET_SCORE", "SECRET_REASON", "SECRET_SCENE"):
            self.assertNotIn(secret, qc_prompt)

    def test_preference_is_bound_and_reaches_actual_engine_request(self):
        fixture = self.fixture
        fixture.review["preferred_speed"] = 1.3
        fixture.review["scene_ids"] = fixture.sentence["scene_ids"] = ["cleaning_wipe"]
        result = fixture.result()
        self.assertTrue(result["ok"], result)
        item = result["segments"][0]
        self.assertAlmostEqual(item["video_speed"], 1.3, places=3)
        self.assertEqual(item["ai_selection"]["preferred_speed"], 1.3)
        plan = fixture.plan()
        inventory = editor._build_edit_inventory(fixture.manifest, {"shots": [fixture.shot]}, plan, None)
        self.assertAlmostEqual(inventory[0]["candidates"][0]["technical_option"]["speed"], 1.3, places=3)
        with mock.patch.object(editor, "_call", return_value={"rows": plan["rows"]}) as call:
            editor._edit_plan(fixture.manifest, {"shots": [fixture.shot]}, plan, fixture.root)
        self.assertIn("cleaning_wipe", call.call_args.args[0])
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = fixture.manifest
        clip = Clip(str(fixture.video), item["source_start"], 2, preferred_speed=1.3,
            source_end=6, video_duration=6, visual_role="product_display", speed_range_override=[.85,20])
        self.assertEqual(engine._timing_request(clip, 2000000).preferred_speed, 1.3)

    def test_selected_window_does_not_reapply_original_scene_guard(self):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = {"head_trim_s": .3, "tail_trim_s": .2, "tail_pad_s": 0}
        engine._raw_segments = [{"ai_selection": {"schema": "test"},
                                 "scene_source_start": 0.0, "scene_source_end": 10.0}]
        clip = Clip("clip.mp4", 1.0, 2, source_end=4.0, video_duration=10, raw_index=0)
        request = engine._timing_request(clip, 2_000_000)
        self.assertEqual(request.lo_bound_us, 1_000_000)
        self.assertEqual(request.hi_bound_us, 4_000_000)
        clip.source_start = 0.1
        request = engine._timing_request(clip, 2_000_000)
        self.assertEqual(request.lo_bound_us, 300_000)
        engine._raw_segments[0]["scene_source_start"] = 2.0
        request = engine._timing_request(clip, 2_000_000)
        self.assertEqual(request.lo_bound_us, 400_000)

    def test_failed_gap_probe_blocks_paper_but_other_categories_are_unchanged(self):
        fixture = self.fixture
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m, engine.report_dir, engine.ffmpeg = fixture.manifest, fixture.root, "test"
        clip = Clip(str(fixture.video), 0, 2, audio=str(fixture.video), audio_mode="file")
        with mock.patch("orchestrator.production_parameters.silence_edges", side_effect=ValueError("probe failed")):
            self.assertEqual(engine._narration_gap_gate([clip])["status"], "NARRATION_GAP_UNVERIFIED")
        engine.m = {"category":"weipin"}
        self.assertIsNone(engine._narration_gap_gate([clip]))

    def test_paper_hard_cuts_clear_stale_transitions(self):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = {"category": "zhipin", "auto_transition": True}
        clip = Clip(str(self.fixture.video), 0, 2, transition={"type": "fade"},
            transition_in_handle_us=100000, transition_out_handle_us=100000)
        engine.plan_transitions([clip])
        self.assertIsNone(clip.transition)
        self.assertEqual(clip.transition_in_handle_us + clip.transition_out_handle_us, 0)

    def test_audio_eof_is_counted_as_silence(self):
        run = SimpleNamespace(returncode=0, stdout="out_time_us=1000000", stderr="")
        with mock.patch("orchestrator.production_parameters.subprocess.run", return_value=run):
            edges = silence_edges(str(self.fixture.video), start_s=0, duration_s=2, ffmpeg="test")
        self.assertEqual(edges["trailing_s"], 1)
        run.stdout = ""
        with mock.patch("orchestrator.production_parameters.subprocess.run", return_value=run):
            with self.assertRaisesRegex(ValueError, "DURATION_UNVERIFIED"):
                silence_edges(str(self.fixture.video), start_s=0, duration_s=2, ffmpeg="test")

    def test_negative_cleaning_sentence_does_not_require_positive_result(self):
        context = contract.editorial_context(self.fixture.manifest)
        scene = next(s for s in context["rules"]["semantic_contracts"] if s["id"] == "cleaning_wipe")
        self.assertIn("反例不适用成功结果合同", scene["when"])
        self.assertIn("不得把正面效果示例反套", "".join(context["rules"]["production_rules"]))

    def test_unrelated_scene_cannot_set_cleaning_speed(self):
        self.fixture.sentence["scene_ids"] = ["count_140"]
        self.fixture.review.update(scene_ids=["cleaning_wipe"], preferred_speed=1.3)
        self.assertEqual(self.fixture.result()["segments"][0]["preferred_speed"], 1)


if __name__ == "__main__":
    unittest.main()
