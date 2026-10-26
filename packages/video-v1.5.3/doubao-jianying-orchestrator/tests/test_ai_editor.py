"""Selection behavior and stale/fabricated receipt blockers without API calls."""
import copy
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import ai_editor as editor, ai_editor_contract as contract
from orchestrator import ai_editor_qc, semantic_gate, shot_analyzer, visual_qc_contract, draft_visual_qc


class AIEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "synthetic.mp4"
        self.video.write_bytes(b"synthetic source identity")
        self.manifest = {"selection_mode": "ai_editor", "product": "合成商品", "head_trim_s": 0,
            "tail_trim_s": 0, "tail_pad_s": 0, "segments": [{"text": "薄薄的一层很舒服", "audio_duration_us": 2000000}]}
        self.shot = {"shot_id": "handfeel", "video": str(self.video), "status": "ready_for_matching",
            "source_start": 0, "source_end": 6, "duration": 6, "material_duration_us": 6000000,
            "role": "usage_demo", "evidence_strength": "C"}
        self.sentence = {"index": 1, "text": self.manifest["segments"][0]["text"], "kind": "selling_point",
            "selling_points": [{"id": "p1", "text": "轻薄舒适"}], "candidate_ids": ["handfeel"]}
        self.review = {"shot_id": "handfeel", "verdict": "matched", "same_product": True,
            "basis": "editorial_fit", "observation": "手指轻捏薄边，巾体自然垂坠", "reason": "贴切展示轻薄手感",
            "confidence": .9, "supported_point_ids": ["p1"], "time_sensitive": False,
            "evidence_intervals_us": [{"start": 2000000, "end": 3000000}],
            "source_identity": contract.source_identity(self.video)}

    def plan(self):
        return {"schema": contract.PLAN_SCHEMA, "ok": True, "sentences": [self.sentence],
            "policy_version": contract.POLICY_VERSION,
            "reviews": {"1": [self.review]}, "display_reviews": [],
            "rows": [{"index": 1, "parts": [{"shot_id": "handfeel", "audio_duration_us": 2000000}], "reason": "先安排卖点"}],
            "identity": {"model": "synthetic-model", "manifest_sha256": contract.digest(self.manifest),
                "analysis_sha256": contract.digest({"shots": [self.shot]}),
                "timing_sha256": contract.digest(None), "prompt_sha256": contract.digest(contract.POLICY),
                "editorial_rules": contract.rules_identity(self.manifest)}}

    def result(self, plan=None):
        return editor.match_manifest(self.manifest, {"shots": [self.shot], "ai_editor_plan": plan or self.plan()})

    def test_soft_claim_is_normal_match_without_old_strength_or_keyword_gate(self):
        result = self.result()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["degraded_count"], 0)
        with mock.patch("orchestrator.demo_actions.judge", side_effect=AssertionError("legacy gate")):
            self.assertTrue(semantic_gate.evaluate_segments(result["segments"], require_explicit=True, category="weipin")["ok"])

    def test_selector_cannot_pick_packaging_that_pixel_review_rejected(self):
        plan = self.plan()
        plan["rows"][0]["parts"][0]["shot_id"] = "wet-packaging"
        self.assertFalse(self.result(plan)["ok"])

    def test_old_policy_plan_cannot_receive_new_certification(self):
        plan = self.plan()
        plan["policy_version"] = "selling-points-first-v1"
        self.assertFalse(self.result(plan)["ok"])

    def test_natural_speed_and_complete_start_evidence_remain_eligible(self):
        self.review["evidence_intervals_us"] = [{"start": 0, "end": 1_000_000}]
        segment = self.result()["segments"][0]
        self.assertEqual(segment["video_speed"], 1.0)
        self.assertEqual(segment["source_start"], 0)
        self.assertGreaterEqual(segment["source_end"], 1)

    def test_editor_receives_actual_speed_and_content_identity(self):
        plan = self.plan()
        with mock.patch.object(editor, "_call", return_value={"rows": plan["rows"]}) as call:
            editor._edit_plan(self.manifest, {"shots": [self.shot]}, plan, self.root)
        prompt = call.call_args.args[0]
        self.assertIn('"within_preferred_speed": true', prompt)
        self.assertIn('"source_identity": "source1"', prompt)
        self.assertNotIn(str(self.video), prompt)
        self.assertIn("最高20倍", prompt)

    def test_mid_late_preference_preserves_correct_evidence_and_short_sources(self):
        self.review["evidence_intervals_us"] = [{"start": 2_000_000, "end": 2_200_000}]
        segment = self.result()["segments"][0]
        self.assertEqual(segment["video_speed"], 1)
        self.assertAlmostEqual(segment["source_start"], 2)
        self.shot.update(source_end=2, material_duration_us=2_000_000)
        self.review["evidence_intervals_us"] = [{"start": 0, "end": 1_000_000}]
        self.assertTrue(self.result()["ok"])

    def test_long_required_evidence_can_exceed_two_but_not_twenty(self):
        self.shot.update(source_end=12, material_duration_us=12_000_000)
        self.review["evidence_intervals_us"] = [{"start": 0, "end": 10_000_000}]
        segment = self.result()["segments"][0]
        self.assertGreater(segment["video_speed"], 2)
        self.assertLessEqual(segment["video_speed"], 20)
        self.assertGreaterEqual(segment["source_end"], 10)

    def test_correct_visual_can_speed_up_beyond_legacy_display_limit(self):
        self.review["evidence_intervals_us"] = [{"start": 2000000, "end": 4500000}]
        result = self.result()
        self.assertTrue(result["ok"], result)
        segment = result["segments"][0]
        self.assertGreater(segment["video_speed"], 1.2)
        self.assertLessEqual(segment["source_start"], 2)
        self.assertGreaterEqual(segment["source_end"], 4.5)
        self.assertEqual(shot_analyzer._fit_speed_range({"visual_role": "product_display"},
            manifest={"selection_mode": "legacy"}, time_sensitive=False), (.85, 1.2))
        changed = copy.deepcopy(segment)
        changed["video_speed"] = 1
        self.assertIn("AI_SELECTION_SPEED_CHANGED", contract.validate_segment_receipt(changed)["reasons"])

    def test_qc_checks_actual_speed_and_timeline_duration(self):
        self.review["evidence_intervals_us"] = [{"start": 2000000, "end": 4500000}]
        segment = self.result()["segments"][0]
        claim = {"index": 1, "ai_segment": segment}
        row = {"source": str(self.video), "source_start": segment["source_start"],
            "source_duration": segment["duration"], "target_duration": 2.0}
        self.assertEqual(ai_editor_qc.validate_cut(claim, [row]), [])
        row["target_duration"] = 1.0
        self.assertIn("AI_QC_FINAL_SPEED_CHANGED", ai_editor_qc.validate_cut(claim, [row]))

    def test_every_selling_point_must_be_covered(self):
        plan = self.plan()
        plan["sentences"][0]["selling_points"].append({"id": "p2", "text": "按压不反渗"})
        self.assertFalse(self.result(plan)["ok"])

    def test_source_change_invalidates_plan(self):
        plan = self.plan()
        self.video.write_bytes(b"changed source")
        with self.assertRaisesRegex(ValueError, "SOURCE_CONTENT_CHANGED"):
            editor._assemble(self.manifest, {"shots": [self.shot]}, plan, narration_timing=None, verify_sources=True)

    def test_changed_script_and_timing_do_not_reuse_old_plan(self):
        plan = self.plan()
        self.manifest["segments"][0]["text"] = "新文案"
        self.assertFalse(self.result(plan)["ok"])
        self.assertFalse(editor.match_manifest(self.manifest, {"ai_editor_plan": plan}, narration_timing={"changed": True})["ok"])

    def test_missing_receipt_is_not_legacy_fallback(self):
        result = semantic_gate.evaluate_segments([{"selection_mode": "ai_editor", "text": "看看这个", "video": str(self.video)}])
        self.assertFalse(result["ok"])

    def test_cut_away_or_modified_receipt_cannot_pass(self):
        segment = self.result()["segments"][0]
        changed = copy.deepcopy(segment)
        changed["window_solution"]["source_end"] = 2.1
        self.assertFalse(contract.validate_segment_receipt(changed)["ok"])
        changed = copy.deepcopy(segment)
        changed["ai_selection"]["observation"] = ""
        self.assertFalse(contract.validate_segment_receipt(changed)["ok"])

    def test_hook_packaging_is_normal_and_selling_point_keeps_its_candidate(self):
        self.manifest["segments"].insert(0, {"text": "姐妹们看看这个", "audio_duration_us": 2000000})
        plan = self.plan()
        sale = {**self.sentence, "index": 2}
        hook = {"index": 1, "text": "姐妹们看看这个", "kind": "hook", "selling_points": [], "candidate_ids": ["pack"]}
        pack = {**self.shot, "shot_id": "pack"}
        pack_video = self.root / "pack.mp4"
        pack_video.write_bytes(b"different synthetic packaging source")
        pack["video"] = str(pack_video)
        display = {**self.review, "shot_id": "pack", "supported_point_ids": [], "basis": "product_display",
                   "observation": "同商品外包装展示", "reason": "承接无卖点钩子", "source_identity": contract.source_identity(pack_video)}
        plan.update(sentences=[hook, sale], reviews={"2": [self.review]}, display_reviews=[display], rows=[
            {"index": 1, "parts": [{"shot_id": "pack", "audio_duration_us": 2000000}]},
            {"index": 2, "parts": [{"shot_id": "handfeel", "audio_duration_us": 2000000}]}])
        plan["identity"]["analysis_sha256"] = contract.digest({"shots": [self.shot, pack]})
        result = editor.match_manifest(self.manifest, {"shots": [self.shot, pack], "ai_editor_plan": plan})
        self.assertTrue(result["ok"], result)
        self.assertEqual([s["temporary_shot_id"] for s in result["segments"]], ["pack", "handfeel"])
        self.assertEqual(result["degraded_count"], 0)

    def test_blind_qc_prompt_does_not_contain_selector_verdict(self):
        prompt = draft_visual_qc._prompt([{"claim": "薄薄一层", "shot_id": "SECRET_SHOT", "index": 1,
            "selection_mode": "ai_editor", "sentence_kind": "selling_point", "slots": 1,
            "claim_gate": {"reason": "SECRET_PASS"}, "selection_score": 99}], 8)
        self.assertNotIn("SECRET_PASS", prompt)
        self.assertNotIn("SECRET_SHOT", prompt)
        self.assertNotIn("selection_score", prompt)
        self.assertIn(ai_editor_qc.RESPONSE_VERSION, prompt)

    def test_qc_v3_response_cannot_certify_ai_editor(self):
        model = {"response_version": visual_qc_contract.RESPONSE_VERSION, "overall": "PASS", "segments": [
            {"index": 1, "match_level": "DIRECT", "visual_ok": True, "observed": "可见", "reason": "贴切", "confidence": .9}]}
        self.assertIn("RESPONSE_VERSION_UNSUPPORTED", visual_qc_contract.response_errors(model, 1, expected_version=ai_editor_qc.RESPONSE_VERSION))

    def test_qc_inspects_start_process_result_and_actual_cut(self):
        segment = self.result()["segments"][0]
        claim = {"index": 1, "ai_segment": segment}
        row = {"source": str(self.video), "source_start": segment["source_start"], "source_duration": segment["duration"]}
        self.assertEqual(ai_editor_qc.validate_cut(claim, [row]), [])
        frames = ai_editor_qc.frame_plan(claim, [row])
        self.assertGreaterEqual(len(frames), 8)
        self.assertIn(0, [f[2] for f in frames])
        row["source_duration"] = .2
        self.assertIn("AI_QC_EVIDENCE_CUT_AWAY", ai_editor_qc.validate_cut(claim, [row]))

    def test_round_expands_beyond_first_eight(self):
        ids = [f"candidate{i}" for i in range(9)]
        sentence = {**self.sentence, "candidate_ids": ids}
        shots = {sid: {**self.shot, "shot_id": sid} for sid in ids}
        def review(_m, _s, shot, _d):
            return {**self.review, "shot_id": shot["shot_id"], "verdict": "matched" if shot["shot_id"] == ids[-1] else "mismatch"}
        with mock.patch.object(editor, "_review", side_effect=review):
            reviews, coverage = editor._review_pool(self.manifest, sentence, shots, self.root, {})
        self.assertEqual(coverage["checked"], 9)
        self.assertEqual(reviews[-1]["verdict"], "matched")

    def test_no_ai_plan_cannot_activate_legacy_matcher(self):
        result = shot_analyzer.match_manifest(self.manifest, {"shots": [self.shot]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["selection_mode"], "ai_editor")

    def test_explicit_legacy_overrides_ai_editor_environment(self):
        with mock.patch.dict("os.environ", {"JY_SELECTION_MODE": "ai_editor"}):
            with mock.patch.object(editor, "match_manifest", side_effect=AssertionError("AI dispatch")):
                result = shot_analyzer.match_manifest({"selection_mode": "legacy", "segments": []}, {"shots": []})
        self.assertNotEqual(result.get("selection_mode"), "ai_editor")

    def test_repeated_source_range_is_not_a_continuation(self):
        segment = self.result()["segments"][0]
        self.assertTrue(contract.sequence_errors([segment, copy.deepcopy(segment)]))
        later = copy.deepcopy(segment)
        later["ai_selection"]["window_us"] = {"start": 4000000, "end": 5000000}
        self.assertEqual(contract.sequence_errors([segment, later]), [])

    def test_scarce_reuse_requires_explicit_reason_and_no_alternatives(self):
        self.manifest["segments"].append(dict(self.manifest["segments"][0]))
        plan = self.plan()
        plan["sentences"].append({**self.sentence, "index": 2})
        plan["reviews"]["2"] = [self.review]
        plan["coverage"] = {"2": {"checked": 1, "total": 1, "exhausted": True,
                                  "feasible": True, "failed_calls": 0, "uncertain": 0}}
        plan["rows"].append({"index": 2, "parts": [{"shot_id": "handfeel", "audio_duration_us": 2_000_000}]})
        self.assertFalse(self.result(plan)["ok"])
        plan["rows"][1]["scarce_reuse_reason"] = "本句只有这一条实际支持轻薄手感的候选"
        self.assertTrue(self.result(plan)["ok"])
        plan["sentences"][1]["kind"] = "hook"
        self.assertFalse(self.result(plan)["ok"])

    def test_scarce_reuse_expands_unreviewed_batch_before_second_edit(self):
        candidate_ids = [f"shot{i}" for i in range(1, 10)]
        shots = [{**self.shot, "shot_id": sid} for sid in candidate_ids]
        sentence = {**self.sentence, "candidate_ids": candidate_ids,
                    "audio_durations_us": [2_000_000]}
        first_review = {**self.review, "shot_id": "shot1"}
        partial_reviews = [first_review] + [
            {"shot_id": sid, "verdict": "mismatch"} for sid in candidate_ids[1:8]]
        plan = {"sentences": [sentence], "reviews": {"1": partial_reviews},
            "display_reviews": [], "coverage": {"1": {"checked": 8, "total": 9,
                "exhausted": False, "feasible": True, "failed_calls": 0, "uncertain": 0}},
            "identity": {"model": "synthetic-model", "manifest_sha256": "a" * 64,
                "timing_sha256": "b" * 64, "analysis_sha256": "c" * 64,
                "prompt_sha256": "d" * 64, "editorial_rules": contract.rules_identity(self.manifest)}}
        responses = [
            {"rows": [{"index": 1, "parts": [{"shot_id": "shot1", "audio_duration_us": 2_000_000}],
                       "scarce_reuse_reason": "首批只有一个支持候选"}]},
            {"rows": [{"index": 1, "parts": [{"shot_id": "shot9", "audio_duration_us": 2_000_000}]}]},
        ]

        def review(_manifest, _sentence, shot, _directory):
            self.assertEqual(shot["shot_id"], "shot9")
            return {**self.review, "shot_id": "shot9"}

        with mock.patch.object(editor, "_call", side_effect=responses) as call, \
             mock.patch.object(editor, "_review", side_effect=review):
            editor._edit_plan(self.manifest, {"shots": shots}, plan, self.root)

        self.assertEqual(call.call_count, 2)
        self.assertIn('"shot_id": "shot9"', call.call_args_list[1].args[0])
        self.assertTrue(plan["coverage"]["1"]["exhausted"])
        self.assertEqual(plan["coverage"]["1"]["checked"], 9)
        self.assertEqual(plan["rows"][0]["parts"][0]["shot_id"], "shot9")
        self.assertTrue(plan["ok"])

    def test_adjacent_physical_experiment_cannot_run_backwards(self):
        early = self.result()["segments"][0]
        late = copy.deepcopy(early)
        early["ai_selection"]["time_sensitive"] = True
        late["ai_selection"].update(time_sensitive=True, window_us={"start": 4000000, "end": 5000000})
        self.assertEqual(contract.sequence_errors([early, late]), [])
        self.assertIn("EXPERIMENT_TIME_REVERSED", contract.sequence_errors([late, early])[0])

    def test_placement_cannot_diverge_from_the_stored_solver_window(self):
        segment = self.result()["segments"][0]
        segment["source_start"] += .5
        self.assertFalse(contract.validate_segment_receipt(segment)["ok"])

    def test_failed_candidate_stays_unknown_without_erasing_valid_observations(self):
        ids = ["failure", "handfeel"]
        def review(_m, _s, shot, _d):
            if shot["shot_id"] == "failure":
                raise ValueError("transient failure")
            return self.review
        sentence = {**self.sentence, "candidate_ids": ids, "audio_durations_us": [2000000]}
        with mock.patch.object(editor, "_review", side_effect=review):
            rows, coverage = editor._review_pool(self.manifest, sentence,
                {sid: {**self.shot, "shot_id": sid} for sid in ids}, self.root, {})
        self.assertTrue(coverage["feasible"])
        self.assertEqual(coverage["failed_calls"], 1)
        self.assertEqual(rows[0]["verdict"], "uncertain")

    def test_success_cache_binds_prompt_image_and_model(self):
        from orchestrator import vision_analyzer
        output = self.root / "response.json"
        sheet = self.root / "sheet.jpg"
        sheet.write_bytes(b"synthetic image")
        with mock.patch.object(vision_analyzer, "_vision_slot", side_effect=lambda: contextlib.nullcontext(SimpleNamespace(acquired=True))), \
                mock.patch.object(vision_analyzer, "_run_volc_chat", return_value=(True, '{"ok":true}')) as provider, \
                mock.patch.dict("os.environ", {"JY_VISION_TRANSPORT": "volc_glm", "JY_VISION_MODEL": "synthetic"}):
            editor._call("prompt", output, sheet)
            editor._call("prompt", output, sheet)
            self.assertEqual(provider.call_count, 1)
            sheet.write_bytes(b"different image")
            editor._call("prompt", output, sheet)
            self.assertEqual(provider.call_count, 2)
            editor._call("changed prompt", output, sheet)
            self.assertEqual(provider.call_count, 3)

    def test_slot_timeout_does_not_make_an_unthrottled_request(self):
        from orchestrator import vision_analyzer
        with mock.patch.object(vision_analyzer, "_vision_slot", return_value=contextlib.nullcontext(SimpleNamespace(acquired=False))), \
                mock.patch.object(vision_analyzer, "_run_volc_chat") as provider, \
                mock.patch.dict("os.environ", {"JY_VISION_TRANSPORT": "volc_glm"}):
            with self.assertRaisesRegex(ValueError, "SLOT_TIMEOUT"):
                editor._call("prompt", self.root / "response.json")
            provider.assert_not_called()

    def test_normal_cli_defaults_to_ai_and_never_calls_legacy_match(self):
        from orchestrator import cli, vision_analyzer
        analysis = {"shots": [self.shot], "vision_analysis": {"errors": []}}
        with mock.patch.object(cli, "_run_shot_analysis", return_value=analysis), \
                mock.patch("orchestrator.material_corrections.apply", side_effect=lambda _m, a, _r: a), \
                mock.patch.object(editor, "prepare", return_value={**analysis, "ai_editor_plan": {"ok": False}}) as prepare, \
                mock.patch.object(vision_analyzer, "match_with_codex") as legacy, \
                mock.patch.dict("os.environ", {"JY_SELECTION_MODE": ""}):
            _, blocker = cli._temporary_match({"segments": [{"text": "看看这个"}]}, self.root / "input.json", self.root)
        self.assertEqual(prepare.call_args.args[0]["selection_mode"], "ai_editor")
        self.assertEqual(blocker["status"], "SHOT_MATCH_BLOCKED")
        legacy.assert_not_called()

    def test_multiple_shots_cover_distinct_points_and_bind_every_part(self):
        plan = self.plan()
        plan["sentences"][0]["selling_points"].append({"id": "p2", "text": "干爽"})
        other_video = self.root / "second.mp4"
        other_video.write_bytes(b"different second source")
        other = {**self.shot, "shot_id": "second", "video": str(other_video)}
        first_review = {**self.review, "evidence_intervals_us": [{"start": 2000000, "end": 2500000}]}
        second_review = {**first_review, "shot_id": "second", "supported_point_ids": ["p2"],
                         "source_identity": contract.source_identity(other_video)}
        plan["reviews"]["1"] = [first_review, second_review]
        plan["rows"][0]["parts"] = [{"shot_id": sid, "audio_duration_us": 1000000} for sid in ("handfeel", "second")]
        plan["identity"]["analysis_sha256"] = contract.digest({"shots": [self.shot, other]})
        result = editor.match_manifest(self.manifest, {"shots": [self.shot, other], "ai_editor_plan": plan})
        self.assertTrue(result["ok"], result)
        segment = result["segments"][0]
        self.assertTrue(segment["multi_shot"])
        self.assertTrue(contract.validate_segment_receipt(segment)["ok"])
        segment["span_parts"][1]["source_end"] += 1
        self.assertFalse(contract.validate_segment_receipt(segment)["ok"])


if __name__ == "__main__":
    unittest.main()
