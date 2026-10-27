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
            "source_sha256": contract.file_sha256(self.video)}
        self.review.update(review_prompt_sha256="a" * 64, frame_sheet_sha256="b" * 64)

    def plan(self):
        return {"schema": contract.PLAN_SCHEMA, "ok": True, "sentences": [self.sentence],
            "reviews": {"1": [self.review]}, "display_reviews": [],
            "rows": [{"index": 1, "parts": [{"shot_id": "handfeel", "audio_duration_us": 2000000}], "reason": "先安排卖点"}],
            "identity": {"model": "synthetic-model", "manifest_sha256": contract.digest(self.manifest),
                "analysis_sha256": contract.digest({"shots": [self.shot]}),
                "timing_sha256": contract.digest(None), "prompt_sha256": contract.digest(contract.POLICY)}}

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
        changed["ai_selection"]["observation"] = "伪造通过"
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
                   "observation": "同商品外包装展示", "reason": "承接无卖点钩子", "source_sha256": contract.file_sha256(pack_video)}
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
        self.assertNotIn("99", prompt)
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

    def test_empty_response_retries_with_backoff_then_succeeds(self):
        from orchestrator import vision_analyzer
        output = self.root / "empty_retry.json"
        body = '{"ok":true,"sentences":[]}'
        with mock.patch.object(vision_analyzer, "_vision_slot", side_effect=lambda: contextlib.nullcontext(SimpleNamespace(acquired=True))), \
                mock.patch.object(vision_analyzer, "_run_volc_chat",
                                  side_effect=[(False, vision_analyzer.EMPTY_RESPONSE),
                                               (True, "  "),
                                               (True, body)]) as provider, \
                mock.patch.object(vision_analyzer.time, "sleep") as sleep, \
                mock.patch.dict("os.environ", {"JY_VISION_TRANSPORT": "volc_glm",
                                               "JY_VISION_RETRIES": "4",
                                               "JY_VISION_MODEL": "synthetic"}):
            payload = editor._call("prompt", output)
        self.assertEqual(payload["ok"], True)
        self.assertEqual(provider.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_empty_response_exhausts_retries(self):
        from orchestrator import vision_analyzer
        with mock.patch.object(vision_analyzer, "_vision_slot", side_effect=lambda: contextlib.nullcontext(SimpleNamespace(acquired=True))), \
                mock.patch.object(vision_analyzer, "_run_volc_chat",
                                  return_value=(False, vision_analyzer.EMPTY_RESPONSE)) as provider, \
                mock.patch.object(vision_analyzer.time, "sleep"), \
                mock.patch.dict("os.environ", {"JY_VISION_TRANSPORT": "volc_glm",
                                               "JY_AI_EDITOR_RETRIES": "3"}):
            with self.assertRaisesRegex(ValueError, "AI_EDITOR_CALL_FAILED:.*empty response"):
                editor._call("prompt", self.root / "empty_fail.json")
        self.assertEqual(provider.call_count, 3)

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
                         "source_sha256": contract.file_sha256(other_video)}
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

    def test_resolve_candidate_ids_accepts_bare_source_prefix(self):
        known = {"source_026_shot_001", "source_020_shot_001", "source_004_shot_001"}
        got = editor._resolve_candidate_ids(
            ["source_026", "source_020_shot_001", "source_999", "source_004"], known)
        self.assertEqual(got, ["source_026_shot_001", "source_020_shot_001", "source_004_shot_001"])

    def test_normalize_selling_points_accepts_bare_strings(self):
        got = editor._normalize_selling_points(
            ["一次一片", {"id": "p9", "text": "湿敷"}], "selling_point")
        self.assertEqual(got, [{"id": "p1", "text": "一次一片"}, {"id": "p9", "text": "湿敷"}])

    def test_clamp_evidence_intervals_shrinks_full_shot_mark(self):
        shot = {"source_start": 0.0, "source_end": 6.4}
        evidence = [{"start": 0.0, "end": 6.4, "claim": "整镜"}]
        got = editor._clamp_evidence_intervals(shot, evidence, {"head_trim_s": 0.3, "tail_trim_s": 0.2})
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0]["start"], 0.3, places=3)
        self.assertAlmostEqual(got[0]["end"], 6.2, places=3)

    def test_inventory_normalizes_truncated_ids_and_nested_display_pool(self):
        shots = [
            {"shot_id": "source_001_shot_001", "visual_description": "盒", "source_start": 0, "source_end": 2},
            {"shot_id": "source_026_shot_001", "visual_description": "抽纸", "source_start": 0, "source_end": 3},
        ]
        payload = {
            "sentences": [{
                "index": 1, "kind": "selling_point",
                "selling_points": [{"id": "p1", "text": "便携"}],
                "candidate_ids": ["source_026", "source_001"],
                "display_pool_ids": ["source_001"],
            }],
        }
        with mock.patch.object(editor, "_call", return_value=payload):
            rows = editor._inventory(self.manifest, shots, self.root / "ai_editor")
        self.assertEqual(rows[0]["candidate_ids"][:2],
                         ["source_026_shot_001", "source_001_shot_001"])

    def test_refill_prefers_action_evidence_over_packaging(self):
        pack = {"shot_id": "pack", "verdict": "matched", "confidence": 0.95,
                "reason": "包装文字支持标称，陈列展示"}
        tear = {"shot_id": "tear", "verdict": "matched", "confidence": 0.7,
                "reason": "画面完整呈现拆开独立包装并取出湿巾"}
        face = {"shot_id": "face", "verdict": "matched", "confidence": 0.7,
                "reason": "直接展示用湿巾擦拭面部清洁油脂"}
        shots = {
            "pack": {"shot_id": "pack", "description": "包装盒陈列", "action": "展示"},
            "tear": {"shot_id": "tear", "description": "撕开小袋取出", "action": "撕开",
                     "action_complete": True},
            "face": {"shot_id": "face", "description": "擦拭脸颊", "action": "擦拭",
                     "action_complete": True},
        }
        best_tear = editor._pick_best_refill_review(
            [pack, tear], claim_text="每次拆开都是新鲜卫生的",
            shots=shots, reserved_shot_ids=set())
        self.assertEqual(best_tear["shot_id"], "tear")
        best_face = editor._pick_best_refill_review(
            [pack, face], claim_text="可以用它清洁脸上的油脂",
            shots=shots, reserved_shot_ids=set())
        self.assertEqual(best_face["shot_id"], "face")
        analysis = {"shots": [
            {**shots["pack"], "status": "ready_for_matching"},
            {**shots["tear"], "status": "ready_for_matching"},
            {**shots["face"], "status": "ready_for_matching"},
        ]}
        self.assertIn("tear", editor._action_shot_ids_for_claim(
            analysis, "每次拆开都是新鲜卫生的"))
        self.assertIn("face", editor._action_shot_ids_for_claim(
            analysis, "清洁脸上的油脂"))
        # Wet-wipe absorbency claim must pull hand-wipe shots, not face-only pool.
        hand = {"shot_id": "hand", "status": "ready_for_matching",
                "description": "用手背展示污渍后用湿巾擦净", "action": "擦拭",
                "action_complete": True}
        analysis_wipe = {"shots": [
            {**shots["pack"], "status": "ready_for_matching"},
            hand,
            {**shots["face"], "status": "ready_for_matching"},
        ]}
        wipe_ids = editor._action_shot_ids_for_claim(analysis_wipe, "擦拭不渗透")
        self.assertIn("hand", wipe_ids)
        self.assertTrue(editor._claim_needs_action("擦拭不渗透"))
        self.assertFalse(editor._claim_needs_action("包装好看又方便"))
        # Product-name「湿敷巾」must not crowd out real face-wipe shots.
        pack_named = {
            "shot_id": "wipe_pack", "status": "ready_for_matching",
            "description": "手持生理盐水湿敷巾包装盒展示", "action": "展示",
        }
        analysis2 = {"shots": [
            pack_named,
            {**shots["face"], "status": "ready_for_matching"},
        ]}
        ids = editor._action_shot_ids_for_claim(analysis2, "清洁脸上的油脂")
        self.assertIn("face", ids)
        self.assertNotIn("wipe_pack", ids)
        # Packaging-only matched review must not win hard action refill.
        self.assertIsNone(editor._pick_best_refill_review(
            [pack], claim_text="可以用它清洁脸上的油脂",
            shots=shots, reserved_shot_ids=set()))

    def test_shortlist_prefers_long_enough_over_short_rule_hits(self):
        # Rule top3 is all short; a long ready shot must still lead the list
        # when min_duration_s requires ~audio/1.2 usable seconds.
        matched = {
            "recommendations": [{
                "index": 1,
                "candidates": [
                    {"shot_id": "short_a"},
                    {"shot_id": "short_b"},
                    {"shot_id": "short_c"},
                ],
            }],
            "segments": [],
        }
        analysis = {"shots": [
            {"shot_id": "short_a", "status": "ready_for_matching", "duration": 1.5},
            {"shot_id": "short_b", "status": "ready_for_matching", "duration": 1.8},
            {"shot_id": "short_c", "status": "ready_for_matching", "duration": 1.6},
            {"shot_id": "long_ok", "status": "ready_for_matching", "duration": 4.0},
        ]}
        ids = editor._rule_shortlist_ids(
            matched, analysis, 1, limit=4, min_duration_s=2.5)
        self.assertEqual(ids[0], "long_ok")
        self.assertIn("long_ok", ids[:1] or ids)

    def test_pick_best_skips_audio_infeasible_shots(self):
        short = {"shot_id": "short", "verdict": "matched", "confidence": 0.99,
                 "reason": "包装展示"}
        long = {"shot_id": "long", "verdict": "matched", "confidence": 0.5,
                "reason": "包装展示"}
        shots = {
            "short": {"shot_id": "short", "duration": 1.5, "source_start": 0,
                      "source_end": 1.5, "description": "短镜"},
            "long": {"shot_id": "long", "duration": 5.0, "source_start": 0,
                     "source_end": 5.0, "description": "长镜"},
        }
        # ~3.5s audio cannot fit 1.5s usable at 1.2x after default trims.
        best = editor._pick_best_refill_review(
            [short, long], claim_text="展示一下包装",
            shots=shots, reserved_shot_ids=set(),
            audio_us=3_500_000, manifest={"head_trim_s": 0.3, "tail_trim_s": 0.2})
        self.assertEqual(best["shot_id"], "long")

    def test_edi_pure_water_soft_rematch_same_product_display(self):
        # Packaging-nominal EDI/纯水 must soft-accept same-product display
        # instead of looping AI_REFILL_NO_MATCH when OCR cannot read the mark.
        sentence = {
            "index": 1, "text": "EDI纯水基底，", "kind": "selling_point",
            "selling_points": [{"id": "p1", "text": "EDI纯水基底"}],
            "candidate_ids": ["pack"],
        }
        shot = {
            "shot_id": "pack", "video": str(self.video),
            "status": "ready_for_matching", "source_start": 0, "source_end": 4,
            "duration": 4, "material_duration_us": 4_000_000,
            "role": "product_display", "evidence_strength": "C",
        }
        uncertain = {
            "verdict": "uncertain", "same_product": True, "confidence": 0.7,
            "observation": "同款包装陈列，标称字样不清晰",
            "reason": "看不清EDI纯水字样",
            "supported_point_ids": [], "basis": "product_display",
            "time_sensitive": False, "evidence_intervals_us": [],
        }

        def fake_extract(source, timestamp, target, ffmpeg=None):
            Path(target).write_bytes(b"frame")

        def fake_sheet(frames, sheet, **kwargs):
            Path(sheet).write_bytes(b"sheet")
            return True

        with mock.patch.object(editor, "_call", return_value=uncertain), \
                mock.patch("orchestrator.shot_analyzer.extract_frame", side_effect=fake_extract), \
                mock.patch("orchestrator.vision_analyzer._write_sheet", side_effect=fake_sheet), \
                mock.patch("orchestrator.platform_env.find_ffmpeg", return_value=("ffmpeg", None)):
            got = editor._review(self.manifest, sentence, shot, self.root)
        self.assertEqual(got["verdict"], "matched")
        self.assertEqual(got["basis"], "product_display")
        self.assertIn("EDI纯水", got["reason"])


if __name__ == "__main__":
    unittest.main()
