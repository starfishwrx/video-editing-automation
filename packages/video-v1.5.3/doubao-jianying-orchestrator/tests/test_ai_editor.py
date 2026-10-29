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
        prepare = mock.patch.object(editor, "_prepare_review_round")
        prepare.start()
        self.addCleanup(prepare.stop)
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

    def test_inventory_discards_unknown_ids_without_promoting_them(self):
        model = {"sentences": [{**self.sentence, "candidate_ids": ["invented", "handfeel"]}],
                 "display_pool_ids": ["invented-display", "handfeel"]}
        with mock.patch.object(editor, "_call", return_value=model):
            rows = editor._inventory(self.manifest, [self.shot], self.root)
        self.assertEqual(rows[0]["candidate_ids"], ["handfeel"])
        self.assertEqual(rows[0]["inventory_ignored_candidate_ids"], ["invented"])
        self.assertEqual(rows[0]["inventory_ignored_display_ids"], ["invented-display"])

    def test_review_accepts_unambiguous_interval_pair(self):
        media = {"start": 0, "end": 6, "before": contract.source_identity(self.video),
                 "times": [0, 3, 5], "sheet": self.root / "sheet.jpg"}
        model = {**self.review, "scene_ids": [], "evidence_intervals_us": [2_000_000, 3_000_000]}
        with mock.patch.object(editor, "_review_media", return_value=media), mock.patch.object(editor, "_call", return_value=model) as call:
            result = editor._review(self.manifest, self.sentence, self.shot, self.root)
        self.assertEqual(result["evidence_intervals_us"], [{"start": 2_000_000, "end": 3_000_000}])
        self.assertIn("不能证明某种具体香型", call.call_args.args[0])

    def test_inventory_still_blocks_malformed_id_types(self):
        model = {"sentences": [{**self.sentence, "candidate_ids": [123]}],
                 "display_pool_ids": ["handfeel"]}
        with mock.patch.object(editor, "_call", return_value=model), \
                self.assertRaisesRegex(ValueError, "AI_EDITOR_CANDIDATE_UNKNOWN"):
            editor._inventory(self.manifest, [self.shot], self.root)

    def test_inventory_resolves_only_unambiguous_source_aliases(self):
        unique = {**self.shot, "shot_id": "source_001_shot_001"}
        first = {**self.shot, "shot_id": "source_002_shot_001"}
        second = {**self.shot, "shot_id": "source_002_shot_002"}
        model = {"sentences": [{**self.sentence, "candidate_ids": ["source_001", "source_002", "source_999"]}],
                 "display_pool_ids": ["source_001"]}
        with mock.patch.object(editor, "_call", return_value=model):
            row = editor._inventory(self.manifest, [unique, first, second], self.root)[0]
        self.assertEqual(row["candidate_ids"][0], "source_001_shot_001")
        self.assertEqual(row["inventory_resolved_candidate_ids"], {"source_001": "source_001_shot_001"})
        self.assertEqual(row["inventory_ignored_candidate_ids"], ["source_002", "source_999"])
        self.assertEqual(row["inventory_resolved_display_ids"], {"source_001": "source_001_shot_001"})

    def test_soft_claim_is_normal_match_without_old_strength_or_keyword_gate(self):
        result = self.result()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["degraded_count"], 0)
        with mock.patch("orchestrator.demo_actions.judge", side_effect=AssertionError("legacy gate")):
            self.assertTrue(semantic_gate.evaluate_segments(result["segments"], require_explicit=True, category="weipin")["ok"])

    def test_partial_selling_points_are_preview_only_and_remain_unresolved(self):
        self.sentence["selling_points"].append({"id": "p2", "text": "具体香型"})
        self.manifest.update(delivery_mode="preview", allow_partial_selling_points=True)
        result = self.result()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][0]["uncovered_selling_points"], [{"id": "p2", "text": "具体香型"}])
        self.assertEqual(result["manifest"]["claim_gate_coverage"]["certification"], "UNCERTIFIED")
        self.assertEqual(result["claim_gate_coverage"]["certification"], "UNCERTIFIED")
        self.assertEqual(result["pending_items"][0]["type"], "PARTIAL_SELLING_POINTS")
        self.manifest["delivery_mode"] = "formal"
        self.assertFalse(self.result()["ok"])

    def test_partial_preview_stops_after_reviewed_supported_point(self):
        self.manifest.update(delivery_mode="preview", allow_partial_selling_points=True)
        sentence = {**self.sentence, "selling_points": self.sentence["selling_points"] + [{"id": "p2", "text": "具体香型"}],
                    "audio_durations_us": [2_000_000]}
        with mock.patch.object(editor, "_review", return_value=self.review):
            rows, coverage = editor._review_pool(self.manifest, sentence, {"handfeel": self.shot}, self.root, {})
        self.assertTrue(coverage["feasible"])
        self.assertEqual(rows[0]["supported_point_ids"], ["p1"])

    def test_refill_preserves_other_sentences_and_reindexes_current_receipt(self):
        manifest = {**self.manifest, "selection_mode": "rule_then_ai", "segments": [
            {"text": "保留原句", "audio_duration_us": 2000000}, self.manifest["segments"][0]]}
        kept = {"index": 1, "text": "保留原句", "temporary_shot_id": "keep",
                "video": "kept.mp4", "source_start": 3, "duration": 2}
        matched = {"segments": [kept, {"index": 2, "text": self.sentence["text"]}],
                   "pending_items": [{"index": 2}], "material_gaps": [{"index": 2}]}
        review = {**self.review, "source_identity": contract.source_identity(self.video)}
        with mock.patch.object(editor, "_review_pool", return_value=([review], {"feasible": True})):
            result = editor.refill_indices(manifest, {"shots": [self.shot]}, matched, [2], self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][0], kept)
        row = result["segments"][1]
        self.assertEqual(row["ai_selection"]["index"], 2)
        self.assertTrue(contract.validate_segment_receipt(row, index=2, verify_source=True, manifest=manifest)["ok"])

    def test_refill_prefers_reviewed_natural_speed_when_claim_coverage_matches(self):
        manifest = {**self.manifest, "selection_mode": "rule_then_ai",
                    "segments": [{"text": self.sentence["text"], "audio_duration_us": 1_000_000}]}
        second = self.root / "natural.mp4"
        second.write_bytes(b"natural source identity")
        fast = {**self.shot, "shot_id": "fast", "source_end": 12, "duration": 12, "material_duration_us": 12_000_000}
        natural = {**self.shot, "shot_id": "natural", "video": str(second)}
        fast_review = {**self.review, "shot_id": "fast", "confidence": .99,
                       "evidence_intervals_us": [{"start": 1_000_000, "end": 9_000_000}]}
        natural_review = {**self.review, "shot_id": "natural", "confidence": .85,
                          "source_identity": contract.source_identity(second),
                          "evidence_intervals_us": [{"start": 2_000_000, "end": 2_800_000}]}
        matched = {"segments": [{"index": 1, "text": self.sentence["text"]}], "pending_items": []}
        with mock.patch.object(editor, "_review_pool", return_value=([fast_review, natural_review], {"feasible": True})):
            result = editor.refill_indices(manifest, {"shots": [fast, natural]}, matched, [1], self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][0]["shot_id"], "natural")
        self.assertLessEqual(result["segments"][0]["video_speed"], 2)

    def test_absorb_speed_cap_blocks_refill_without_trimming_reviewed_evidence(self):
        text = "吸水实验看看吸收效果"
        manifest = {**self.manifest, "selection_mode": "rule_then_ai", "category": "weipin",
                    "max_absorb_demo_speed": 2,
                    "segments": [{"text": text, "audio_duration_us": 1_000_000}]}
        shot = {**self.shot, "source_end": 12, "material_duration_us": 12_000_000}
        review = {**self.review, "time_sensitive": True,
                  "evidence_intervals_us": [{"start": 1_000_000, "end": 9_000_000}]}
        matched = {"segments": [{"index": 1, "text": text}], "pending_items": []}
        for cap in (2, 0.5, "invalid", float("inf")):
            with self.subTest(cap=cap), mock.patch.object(editor, "_review_pool",
                    return_value=([review], {"feasible": True})):
                result = editor.refill_indices({**manifest, "max_absorb_demo_speed": cap},
                    {"shots": [shot]}, matched, [1], self.root)
            self.assertFalse(result["ok"], result)
            self.assertEqual(result["refill_replaced"], [])
        with mock.patch.object(editor, "_review_pool", return_value=([review], {"feasible": True})):
            result = editor.refill_indices({**manifest, "max_absorb_demo_speed": 8},
                {"shots": [shot]}, matched, [1], self.root)
        self.assertTrue(result["ok"], result)
        self.assertLessEqual(result["segments"][0]["video_speed"], 8)
        self.assertTrue(contract.validate_segment_receipt(result["segments"][0],
            index=1, verify_source=True, manifest={**manifest, "max_absorb_demo_speed": 8})["ok"])

    def test_absorb_cap_applies_to_final_assembly_and_not_unrelated_display(self):
        self.manifest.update(category="weipin", max_absorb_demo_speed=2)
        self.shot.update(source_end=12, material_duration_us=12_000_000)
        self.review["evidence_intervals_us"] = [{"start": 1_000_000, "end": 9_000_000}]
        self.review["scene_ids"] = ["absorb_test"]
        self.assertFalse(self.result()["ok"])
        self.review["scene_ids"] = []
        self.assertTrue(self.result()["ok"])
        self.review["observation"] = "往产品表面倒蓝色液体，然后吸收"
        self.assertFalse(self.result()["ok"])

    def test_absorb_cap_feasibility_single_and_multi_share_same_bound(self):
        manifest = {**self.manifest, "category": "weipin", "max_absorb_demo_speed": 2}
        sentence = {**self.sentence, "text": "吸水实验", "audio_durations_us": [1_000_000]}
        shot = {**self.shot, "source_end": 12, "material_duration_us": 12_000_000}
        review = {**self.review, "evidence_intervals_us": [{"start": 1_000_000, "end": 9_000_000}]}
        with mock.patch.object(editor, "_review", return_value=review):
            _, coverage = editor._review_pool(manifest, sentence, {"handfeel": shot}, self.root, {})
        self.assertFalse(coverage["feasible"])
        observed = editor._observed_shot(shot, review, manifest=manifest, sentence=sentence)
        single = shot_analyzer.candidate_window(observed, audio_duration_us=1_000_000,
            manifest=manifest, time_sensitive=False)
        span = shot_analyzer.span_segment_for(observed, manifest=manifest, time_sensitive=False)
        self.assertFalse(single.ok)
        self.assertEqual(span.speed_max, 2)
        combined = editor.timing_contract.solve_span(editor.timing_contract.SpanRequest(
            segments=(span, span), audio_duration_us=2_000_000, tail_pad_us=0))
        self.assertFalse(combined.ok)

    def test_refill_preserves_repeated_sentences_and_rejects_duplicate_identity(self):
        manifest = {**self.manifest, "selection_mode": "rule_then_ai", "segments": [
            {"text": "囤纸了，", "audio_duration_us": 1_000_000},
            {"text": "囤纸了，", "audio_duration_us": 1_000_000}, self.manifest["segments"][0]]}
        first = {"index": 1, "text": "囤纸了，", "temporary_shot_id": "first", "video": "first.mp4"}
        second = {"index": 2, "text": "囤纸了，", "temporary_shot_id": "second", "video": "second.mp4"}
        matched = {"segments": [first, second, {"index": 3, "text": self.sentence["text"]}], "pending_items": []}
        with mock.patch.object(editor, "_review_pool", return_value=([self.review], {"feasible": True})):
            result = editor.refill_indices(manifest, {"shots": [self.shot]}, matched, [3], self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][:2], [first, second])
        self.assertEqual(result["segments"][2]["ai_selection"]["index"], 3)
        broken = {**matched, "segments": [first, {**second, "index": 1}, matched["segments"][2]]}
        with mock.patch.object(editor, "_review_pool", side_effect=AssertionError("must not review ambiguous input")):
            result = editor.refill_indices(manifest, {"shots": [self.shot]}, broken, [3], self.root)
        self.assertFalse(result["ok"])
        self.assertEqual(result["refill_errors"], ["AI_REFILL_SEGMENT_INDEX_DUPLICATE:1"])
        self.assertEqual(result["segments"], broken["segments"])

    def test_refill_combines_reviewed_claims_without_losing_timing_or_evidence(self):
        text = "擦油污、擦水渍都好用，湿水不容易破。"
        manifest = {**self.manifest, "selection_mode": "rule_then_ai", "delivery_mode": "preview",
                    "allow_partial_selling_points": True, "ai_refill_indices": [1],
                    "segments": [{"text": text, "audio_duration_us": 4500000}]}
        matched = {"segments": [{"index": 1, "text": text}], "pending_items": []}
        shots, reviews = [], []
        for i in range(3):
            video = self.root / f"source-{i}.mp4"
            video.write_bytes(f"source-{i}".encode())
            sid = f"claim-{i}"
            shots.append({**self.shot, "shot_id": sid, "video": str(video)})
            reviews.append({**self.review, "shot_id": sid, "source_identity": contract.source_identity(video),
                            "supported_point_ids": [f"p{i+1}"]})
        with mock.patch.object(editor, "_review_pool", return_value=(list(reversed(reviews)), {"feasible": True})) as review:
            result = editor.refill_indices(manifest, {"shots": shots}, matched, [1], self.root)
        self.assertFalse(review.call_args.args[0]["allow_partial_selling_points"])
        self.assertTrue(result["ok"], result)
        row = result["segments"][0]
        self.assertEqual(len(row["span_parts"]), 3)
        self.assertEqual([p["shot_id"] for p in row["span_parts"]], [s["shot_id"] for s in shots])
        self.assertTrue(contract.validate_segment_receipt(row, index=1, verify_source=True, manifest=manifest)["ok"])
        self.assertAlmostEqual(sum(p["segment_s"] for p in row["span_parts"]), 4.5, places=5)
        self.assertFalse(row.get("uncovered_selling_points"))

    def test_explicit_compound_refill_keeps_incomplete_evidence_preview_only(self):
        text = "擦油污、擦水渍都好用"
        manifest = {**self.manifest, "selection_mode": "rule_then_ai", "delivery_mode": "preview",
                    "allow_partial_selling_points": True, "ai_refill_indices": ["1"],
                    "segments": [{"text": text, "audio_duration_us": 2000000}]}
        matched = {"segments": [{"index": 1, "text": text}], "pending_items": []}
        with mock.patch.object(editor, "_review_pool", return_value=([self.review], {"feasible": False})) as review:
            result = editor.refill_indices(manifest, {"shots": [self.shot]}, matched, [1], self.root)
        self.assertFalse(review.call_args.args[0]["allow_partial_selling_points"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["segments"][0]["uncovered_selling_points"][0]["id"], "p2")
        self.assertEqual(result["claim_gate_coverage"]["certification"], "UNCERTIFIED")
        manifest["delivery_mode"] = "formal"
        with mock.patch.object(editor, "_review_pool", return_value=([self.review], {"feasible": False})):
            self.assertFalse(editor.refill_indices(manifest, {"shots": [self.shot]}, matched, [1], self.root)["ok"])

    def test_user_directed_display_is_preview_only_and_never_certifies_claim(self):
        text = "自带玫瑰花香"
        manifest = {**self.manifest, "selection_mode": "rule_then_ai", "delivery_mode": "preview",
                    "segments": [{"text": text, "audio_duration_us": 2000000}],
                    "user_display_fallbacks": [{"index": 1, "kind": "packaging", "user_authorized": True}]}
        matched = {"segments": [{"index": 1, "text": text}], "pending_items": []}
        shot = {**self.shot, "description": "产品包装正面"}
        review = {**self.review, "verdict": "mismatch", "same_product": True,
                  "observation": "手持展示同商品包装，香型文字看不清"}
        with mock.patch.object(editor, "_review_pool", return_value=([review], {"feasible": False})):
            result = editor.refill_indices(manifest, {"shots": [shot]}, matched, [1], self.root)
        self.assertTrue(result['ok'], result)
        self.assertTrue(result['segments'][0]['degraded_no_match'])
        self.assertEqual(result['segments'][0]['evidence_intervals'], [])
        self.assertNotIn('ai_selection', result['segments'][0])
        self.assertEqual(result['claim_gate_coverage']['certification'], 'UNCERTIFIED')
        self.assertEqual(result['preview_unresolved'][0]['type'], 'USER_AUTHORIZED_DISPLAY_UNVERIFIED')
        for changes in ({"delivery_mode": "formal"}, {"user_display_fallbacks": []}):
            with mock.patch.object(editor, "_review_pool", return_value=([review], {"feasible": False})):
                self.assertFalse(editor.refill_indices({**manifest, **changes}, {"shots": [shot]}, matched, [1], self.root)['ok'])
        stale = {**review, "source_identity": {"path": "unrelated"}}
        with mock.patch.object(editor, "_review_pool", return_value=([stale], {"feasible": False})):
            self.assertFalse(editor.refill_indices(manifest, {"shots": [shot]}, matched, [1], self.root)['ok'])
        other_product = {**review, "same_product": False}
        with mock.patch.object(editor, "_review_pool", return_value=([other_product], {"feasible": False})):
            self.assertFalse(editor.refill_indices(manifest, {"shots": [shot]}, matched, [1], self.root)['ok'])
        paper = {**manifest, "user_display_fallbacks": [{"index": 1, "kind": "paper", "user_authorized": True}]}
        with mock.patch.object(editor, "_review_pool", return_value=([review], {"feasible": False})):
            self.assertFalse(editor.refill_indices(paper, {"shots": [shot]}, matched, [1], self.root)['ok'])
        shown = {**review, "observation": "双手展开一张白色纸巾"}
        with mock.patch.object(editor, "_review_pool", return_value=([shown], {"feasible": False})):
            self.assertTrue(editor.refill_indices(paper, {"shots": [shot]}, matched, [1], self.root)['ok'])

    def test_refill_prioritizes_observed_specific_object_but_still_requires_review(self):
        text = "还送定制纸巾支架"
        manifest = {**self.manifest, "segments": [{"text": text, "audio_duration_us": 2000000}]}
        generic = [{**self.shot, "shot_id": f"generic-{i}", "description": "展示绿色包装的厨房纸巾"}
                   for i in range(20)]
        holder = {**self.shot, "shot_id": "holder", "description": "双手将纸巾卷放在木质支架上"}
        matched = {"segments": [{"index": 1, "text": text}], "recommendations": [{"index": 1,
                    "top3": [{"shot_id": s["shot_id"]} for s in generic[:10]]}]}
        with mock.patch.object(editor, "_review_pool", return_value=([], {"feasible": False})) as review:
            result = editor.refill_indices(manifest, {"shots": generic + [holder]}, matched, [1], self.root)
        order = review.call_args.args[1]["candidate_ids"]
        self.assertIn("holder", order[:editor.ROUND_SIZE])
        self.assertEqual(order[0], "generic-0")
        self.assertLess(order.index("holder"), order.index("generic-8"))
        self.assertEqual(set(order), {s["shot_id"] for s in generic + [holder]})
        self.assertFalse(result["ok"])
        self.assertIn("AI_REFILL_NO_MATCH:1", result["refill_errors"])

    def test_observed_text_retrieval_is_empty_without_overlap_and_stable_on_ties(self):
        shots = {"a": {"description": "手拿纸巾支架"}, "b": {"description": "展示纸巾支架"},
                 "c": {"description": "展示产品包装"}}
        self.assertEqual(editor._observed_text_shortlist("纸巾支架", shots), ["a", "b"])
        self.assertEqual(editor._observed_text_shortlist("xyz", shots), [])

    def test_refill_does_not_certify_other_degraded_sentences(self):
        manifest = {**self.manifest, "selection_mode": "rule_then_ai", "delivery_mode": "preview",
                    "segments": [{"text": "保留原句", "audio_duration_us": 2000000}, self.manifest["segments"][0]]}
        kept = {"index": 1, "text": "保留原句", "temporary_shot_id": "keep",
                "video": "kept.mp4", "source_start": 3, "duration": 2}
        matched = {"segments": [kept, {"index": 2, "text": self.sentence["text"]}],
                   "pending_items": [], "degraded_matches": [{"index": 1}, {"index": 2}]}
        review = {**self.review, "source_identity": contract.source_identity(self.video)}
        with mock.patch.object(editor, "_review_pool", return_value=([review], {"feasible": True})):
            result = editor.refill_indices(manifest, {"shots": [self.shot]}, matched, [2], self.root)
        self.assertTrue(result["ok"])
        self.assertFalse(result["claim_gate_coverage"]["validation_passed"])
        self.assertEqual(result["claim_gate_coverage"]["certification"], "UNCERTIFIED")
        self.assertEqual(result["manifest"]["claim_gate_coverage"], result["claim_gate_coverage"])

    def test_refill_preserves_uncovered_compound_claim_for_preview(self):
        text = "精致包装和好闻的雪松绿茶香"
        manifest = {**self.manifest, "selection_mode": "rule_then_ai", "delivery_mode": "preview",
                    "allow_partial_selling_points": True,
                    "segments": [{"text": text, "audio_duration_us": 2000000}]}
        matched = {"segments": [{"index": 1, "text": text}], "pending_items": [],
                   "degraded_matches": [{"index": 1}]}
        review = {**self.review, "source_identity": contract.source_identity(self.video),
                  "supported_point_ids": ["p1"]}
        with mock.patch.object(editor, "_review_pool", return_value=([review], {"feasible": True})):
            result = editor.refill_indices(manifest, {"shots": [self.shot]}, matched, [1], self.root)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][0]["uncovered_selling_points"],
                         [{"id": "p2", "text": "好闻的雪松绿茶香"}])
        self.assertEqual(result["manifest"]["preview_unresolved"][0]["index"], 1)
        self.assertEqual(result["claim_gate_coverage"]["certification"], "UNCERTIFIED")

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

    def test_continuation_applies_head_guard_to_original_scene(self):
        self.manifest["head_trim_s"] = 0.3
        plan = self.plan()
        plan["rows"][0]["parts"][0]["source_start_us"] = 2_000_000
        result = self.result(plan)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["segments"][0]["source_start"], 2.0)
        self.review["evidence_intervals_us"] = [{"start": 100_000, "end": 1_000_000}]
        plan = self.plan()
        plan["rows"][0]["parts"][0]["source_start_us"] = 100_000
        self.assertFalse(self.result(plan)["ok"])

    def test_editor_receives_actual_speed_and_content_identity(self):
        plan = self.plan()
        with mock.patch.object(editor, "_call", return_value={"rows": plan["rows"]}) as call:
            editor._edit_plan(self.manifest, {"shots": [self.shot]}, plan, self.root)
        prompt = call.call_args.args[0]
        self.assertIn('"within_preferred_speed": true', prompt)
        self.assertIn('"source_identity": "source1"', prompt)
        self.assertNotIn(str(self.video), prompt)
        self.assertIn("最高20倍", prompt)

    def test_editor_repairs_duplicate_rows_without_silently_selecting_one(self):
        plan = self.plan()
        final = copy.deepcopy(plan["rows"])
        duplicate = final + copy.deepcopy(final)
        with mock.patch.object(editor, "_call", side_effect=[{"rows": duplicate}, {"rows": duplicate}, {"rows": final}]) as call:
            editor._edit_plan(self.manifest, {"shots": [self.shot]}, plan, self.root)
        self.assertTrue(plan["ok"])
        self.assertEqual(call.call_count, 3)
        self.assertIn("duplicate=[1]", call.call_args.args[0])

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

    def test_guarded_window_is_reobserved_not_silently_clipped(self):
        self.manifest["head_trim_s"] = 0.3
        sentence = {**self.sentence, "audio_durations_us": [2_000_000]}
        previous = {**self.review, "scene_ids": [], "evidence_intervals_us": [{"start": 0, "end": 1_000_000}]}
        observed = []
        def review(_m, _s, shot, _d, *, dense=False):
            observed.append((shot["source_start"], dense))
            if dense:
                return {**previous, "evidence_intervals_us": [{"start": 300_000, "end": 1_000_000}]}
            return previous
        with mock.patch.object(editor, "_review", side_effect=review):
            rows, coverage = editor._review_pool(self.manifest, sentence, {"handfeel": self.shot}, self.root, {})
        self.assertIn((0.3, True), observed)
        self.assertEqual(rows[0]["guarded_review_status"], "confirmed")
        self.assertTrue(coverage["feasible"])
        def uncertain(_m, _s, shot, _d, *, dense=False):
            return {"verdict": "uncertain"} if dense else previous
        with mock.patch.object(editor, "_review", side_effect=uncertain):
            rows, coverage = editor._review_pool(self.manifest, sentence, {"handfeel": self.shot}, self.root, {})
        self.assertEqual(rows[0]["guarded_review_status"], "unconfirmed")
        self.assertEqual(rows[0]["evidence_intervals_us"], previous["evidence_intervals_us"])
        self.assertFalse(coverage["feasible"])
        self.assertEqual(coverage["uncertain"], 1)
        def lost_point(_m, _s, shot, _d, *, dense=False):
            return {**previous, "supported_point_ids": [], "evidence_intervals_us": [{"start": 300_000, "end": 1_000_000}]} if dense else previous
        with mock.patch.object(editor, "_review", side_effect=lost_point):
            rows, coverage = editor._review_pool(self.manifest, sentence, {"handfeel": self.shot}, self.root, {})
        self.assertEqual(rows[0]["guarded_review_status"], "unconfirmed")
        self.assertFalse(coverage["feasible"])

    def test_broad_evidence_gets_dense_review_without_losing_support(self):
        sentence = {**self.sentence, "audio_durations_us": [1_000_000]}
        media = {"start": 0, "end": 6, "before": contract.source_identity(self.video),
                 "times": [0, 3, 5], "sheet": self.root / "sheet.jpg"}
        broad = {**self.review, "scene_ids": [], "evidence_intervals_us": [{"start": 1_000_000, "end": 5_000_000}]}
        with mock.patch.object(editor, "_review_media", return_value=media), mock.patch.object(editor, "_call", return_value=broad):
            first = editor._review(self.manifest, sentence, self.shot, self.root)
        self.assertTrue(first["dense_required"])
        def review(_m, _s, _shot, _directory, *, dense=False):
            return {**first, "verdict": "uncertain"} if dense else first
        with mock.patch.object(editor, "_review", side_effect=review):
            rows, _ = editor._review_pool(self.manifest, sentence, {"handfeel": self.shot}, self.root, {})
        self.assertEqual(rows[0]["verdict"], "matched")
        self.assertEqual(rows[0]["evidence_intervals_us"], broad["evidence_intervals_us"])
        self.assertEqual(rows[0]["refinement_status"], "retained_original")
        refined = {**first, "dense_required": False, "evidence_intervals_us": [{"start": 2_000_000, "end": 3_000_000}]}
        def review_confirmed(_m, _s, _shot, _directory, *, dense=False):
            return refined if dense else first
        with mock.patch.object(editor, "_review", side_effect=review_confirmed):
            rows, _ = editor._review_pool(self.manifest, sentence, {"handfeel": self.shot}, self.root, {})
        self.assertEqual(rows[0]["evidence_intervals_us"], refined["evidence_intervals_us"])
        self.assertEqual(rows[0]["refinement_status"], "dense_review")

    def test_compact_window_requires_new_pixel_review_and_preserves_support(self):
        sentence = {**self.sentence, "audio_durations_us": [1_000_000]}
        broad = {**self.review, "scene_ids": [], "dense_required": False,
                 "evidence_intervals_us": [{"start": 1_000_000, "end": 5_000_000}]}
        calls = []
        def review(_m, _s, shot, _d, *, dense=False):
            if not shot.get("_compact_probe"):
                return broad
            calls.append((shot["source_start"], shot["source_end"], dense))
            return {**broad, "evidence_intervals_us": [{"start": 2_000_000, "end": 4_000_000}]}
        with mock.patch.object(editor, "_review", side_effect=review):
            rows, coverage = editor._review_pool(self.manifest, sentence, {"handfeel": self.shot}, self.root, {})
        self.assertEqual(calls, [(2.0, 4.0, True)])
        self.assertTrue(coverage["feasible"])
        self.assertEqual(rows[0]["compact_review_status"], "confirmed")
        self.assertEqual(rows[0]["evidence_intervals_us"], [{"start": 2_000_000, "end": 4_000_000}])
        for changes in ({"verdict": "uncertain"}, {"same_product": False},
                        {"supported_point_ids": []}, {"source_identity": {"changed": True}},
                        {"evidence_intervals_us": [{"start": 1_000_000, "end": 4_000_000}]}):
            def failed(_m, _s, shot, _d, *, dense=False):
                if not shot.get("_compact_probe"):
                    return broad
                return {**broad, "evidence_intervals_us": [{"start": 2_000_000, "end": 4_000_000}], **changes}
            with self.subTest(changes=changes), mock.patch.object(editor, "_review", side_effect=failed):
                rows, _ = editor._review_pool(self.manifest, sentence, {"handfeel": self.shot}, self.root, {})
                self.assertEqual(rows[0]["evidence_intervals_us"], broad["evidence_intervals_us"])
                self.assertNotIn("compact_review_status", rows[0])

    def test_compact_probe_checks_edge_when_center_misses_action(self):
        sentence = {**self.sentence, "audio_durations_us": [1_000_000]}
        broad = {**self.review, "scene_ids": [], "dense_required": False,
                 "evidence_intervals_us": [{"start": 1_000_000, "end": 5_000_000}]}
        calls = []
        def review(_m, _s, shot, _d, *, dense=False):
            if not shot.get("_compact_probe"):
                return broad
            calls.append((shot["source_start"], shot["source_end"]))
            if shot["source_start"] == 1.0:
                return {**broad, "evidence_intervals_us": [{"start": 1_200_000, "end": 2_300_000}]}
            return {**broad, "verdict": "mismatch"}
        with mock.patch.object(editor, "_review", side_effect=review):
            rows, coverage = editor._review_pool(self.manifest, sentence, {"handfeel": self.shot}, self.root, {})
        self.assertEqual(calls, [(2.0, 4.0), (1.0, 3.0)])
        self.assertEqual(rows[0]["compact_review_status"], "confirmed")
        self.assertEqual(rows[0]["evidence_intervals_us"], [{"start": 1_200_000, "end": 2_300_000}])
        self.assertTrue(coverage["feasible"])

    def test_one_feasible_candidate_gets_one_more_round_for_editorial_choice(self):
        ids = [f"candidate{i}" for i in range(24)]
        sentence = {**self.sentence, "candidate_ids": ids, "audio_durations_us": [2_000_000]}
        shots = {sid: {**self.shot, "shot_id": sid} for sid in ids}
        def review(_m, _s, shot, _d):
            sid = shot["shot_id"]
            return {**self.review, "shot_id": sid,
                    "verdict": "matched" if sid in {ids[0], ids[8]} else "mismatch"}
        with mock.patch.object(editor, "_review", side_effect=review):
            reviews, coverage = editor._review_pool(self.manifest, sentence, shots, self.root, {})
        self.assertEqual(coverage["checked"], 16)
        self.assertEqual(coverage["reason"], "READY")
        self.assertEqual([r["shot_id"] for r in reviews if r["verdict"] == "matched"], [ids[0], ids[8]])

    def test_missing_evidence_and_infeasible_windows_are_distinguished(self):
        sentence = {**self.sentence, "audio_durations_us": [2_000_000]}
        with mock.patch.object(editor, "_review", return_value={"shot_id": "handfeel", "verdict": "uncertain"}):
            _, coverage = editor._review_pool(self.manifest, sentence, {"handfeel": self.shot}, self.root, {})
        self.assertEqual(coverage["reason"], "EVIDENCE_UNKNOWN")
        shot = {**self.shot, "source_end": 60, "material_duration_us": 60_000_000}
        review = {**self.review, "evidence_intervals_us": [{"start": 0, "end": 50_000_000}]}
        with mock.patch.object(editor, "_review", return_value=review):
            _, coverage = editor._review_pool(self.manifest, sentence, {"handfeel": shot}, self.root, {})
        self.assertFalse(coverage["feasible"])
        self.assertEqual(coverage["reason"], "KNOWN_WINDOWS_INFEASIBLE")

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

    def test_pixel_review_uses_qwen_and_keeps_completed_glm_reviews(self):
        from orchestrator import vision_analyzer
        output = self.root / "reviews" / "kept.json"
        output.parent.mkdir()
        sheet = self.root / "sheet.jpg"
        sheet.write_bytes(b"synthetic image")
        editor._save(output, {"verdict": "matched"})
        editor._save(output.with_suffix(".request.json"), {
            "prompt_sha256": contract.digest("prompt"),
            "image_sha256": contract.file_sha256(sheet),
            "model": "glm-5.3-flash",
            "endpoint": "https://ark.cn-beijing.volces.com/api/plan/v3",
        })
        fresh = self.root / "reviews" / "fresh.json"
        with mock.patch.object(vision_analyzer, "_run_text_chat", return_value=(True, '{"ok":true}')) as qwen, \
                mock.patch.object(vision_analyzer, "_run_volc_chat", return_value=(True, '{"ok":true}')) as glm, \
                mock.patch.dict("os.environ", {
                    "JY_VISION_TRANSPORT": "volc_glm",
                    "JY_VISION_MODEL": "glm-5.3-flash",
                    "JY_VISION_TEXT_BASE_URL": "https://example.invalid/v1",
                    "JY_VISION_TEXT_API_KEY": "sk-test-secret-value",
                    "JY_VISION_TEXT_MODEL": "qwen3.8-flash",
                }):
            cached = editor._call("prompt", output, sheet)
            editor._call("prompt", fresh, sheet)
            editor._call("plan only", self.root / "edit.json")
        self.assertEqual(cached["verdict"], "matched")
        qwen.assert_called_once()
        self.assertIs(qwen.call_args.args[0], sheet)
        glm.assert_called_once()
        self.assertIsNone(glm.call_args.args[0])
        recorded = json.loads(fresh.with_suffix(".request.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["model"], "qwen3.8-flash")
        self.assertEqual(recorded["thinking"], "enable_thinking=false")

    def test_explicit_qwen_editor_routes_text_and_invalidates_glm_cache(self):
        from orchestrator import vision_analyzer
        output = self.root / "response.json"
        editor._save(output, {"old": True})
        editor._save(output.with_suffix(".request.json"), {"prompt_sha256": contract.digest("prompt"),
            "image_sha256": None, "model": "glm", "endpoint": "old"})
        env = {"JY_VISION_TRANSPORT": "volc_glm", "JY_AI_EDITOR_QWEN": "1",
               "JY_VISION_TEXT_BASE_URL": "https://example.invalid/v1",
               "JY_VISION_TEXT_API_KEY": "sk-test-secret-value", "JY_VISION_TEXT_MODEL": "qwen"}
        with mock.patch.object(vision_analyzer, "_run_text_chat", return_value=(True, '{"new":true}')) as qwen, \
                mock.patch.object(vision_analyzer, "_run_volc_chat") as glm, mock.patch.dict("os.environ", env):
            self.assertEqual(editor._call("prompt", output), {"new": True})
            self.assertEqual(editor._call("prompt", output), {"new": True})
        qwen.assert_called_once()
        glm.assert_not_called()

    def test_qwen_transport_used_for_text_and_image_editor_calls(self):
        from orchestrator import vision_analyzer
        sheet = self.root / "qwen-sheet.jpg"
        sheet.write_bytes(b"image")
        env = {"JY_VISION_TRANSPORT": "qwen", "JY_QWEN_BASE_URL": "https://example.invalid/v1",
               "JY_QWEN_API_KEY": "sk-test-secret", "JY_QWEN_MODEL": "qwen-test"}
        with mock.patch.dict("os.environ", env), \
                mock.patch.object(vision_analyzer, "_run_qwen_chat", return_value=(True, '{"ok":true}')) as qwen, \
                mock.patch.object(vision_analyzer, "_run_volc_chat") as glm:
            editor._call("text", self.root / "text.json")
            editor._call("image", self.root / "image.json", sheet)
            self.assertEqual(editor._editor_model(), "qwen-test")
        self.assertEqual(qwen.call_count, 2)
        glm.assert_not_called()

    def test_slot_timeout_does_not_make_an_unthrottled_request(self):
        from orchestrator import vision_analyzer
        with mock.patch.object(vision_analyzer, "_run_volc_chat", return_value=(False, "VISION_SLOT_TIMEOUT")) as provider, \
                mock.patch.dict("os.environ", {"JY_VISION_TRANSPORT": "volc_glm"}):
            with self.assertRaisesRegex(ValueError, "SLOT_TIMEOUT"):
                editor._call("prompt", self.root / "response.json")
            provider.assert_called_once()

    def test_float_vision_timeout_env_string_is_parsed_safely(self):
        from orchestrator import vision_analyzer
        output = self.root / "out.json"
        with mock.patch.object(vision_analyzer, "_run_volc_chat", return_value=(True, '{"ok":true}')) as provider, \
             mock.patch.dict("os.environ", {"JY_VISION_TRANSPORT": "volc_glm", "JY_VISION_TIMEOUT": "360.0"}):
            editor._call("prompt", output)
            # Ensure the timeout passed to provider is an int parsed from the float string
            self.assertTrue(provider.called)
            kwargs = provider.call_args.kwargs
            self.assertIn("timeout", kwargs)
            self.assertIsInstance(kwargs["timeout"], int)
            self.assertEqual(kwargs["timeout"], 360)

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

    def test_rule_first_keeps_tts_clause_boundaries_and_refills_preview_gaps(self):
        from orchestrator import cli, timing_contract
        manifest = {"selection_mode": "rule_then_ai", "delivery_mode": "preview",
                    "full_script": "谁能拒绝精致包装和好闻的雪松绿茶香啊，尝鲜价整箱抱走美滋滋。"
                                   "纸张厚实亲肤，四层加厚，随便揉搓不掉絮。",
                    "ai_refill_max_sentences": 1}
        expected = timing_contract.split_sentences(manifest)
        self.assertGreater(len(expected), 2)
        analysis = {"shots": [self.shot], "vision_analysis": {"errors": []}}
        def fake_match(rule_manifest, _analysis, **_kwargs):
            self.assertEqual(rule_manifest["selection_mode"], "legacy")
            self.assertEqual(rule_manifest["segments"], expected)
            return {"ok": True, "segments": expected, "manifest": {**rule_manifest},
                    "degraded_matches": [{"index": 1}, {"index": 2}], "pending_items": []}
        with mock.patch.object(cli, "_run_shot_analysis", return_value=analysis), \
                mock.patch("orchestrator.material_corrections.apply", side_effect=lambda _m, a, _r: a), \
                mock.patch.object(shot_analyzer, "match_manifest", side_effect=fake_match), \
                mock.patch("orchestrator.scene_rules.apply", return_value={}), \
                mock.patch.object(editor, "refill_indices", side_effect=lambda _m, _a, match, *_args, **_kw: match) as refill:
            _, blocker = cli._temporary_match(manifest, self.root / "input.json", self.root)
        self.assertIsNone(blocker)
        self.assertEqual(refill.call_args.args[3], [1])

    def test_rule_first_refills_blocked_claim_even_when_match_says_ok(self):
        from orchestrator import cli
        manifest = {"selection_mode": "rule_then_ai", "delivery_mode": "preview",
                    "segments": [{"text": "普通介绍"}, {"text": "柔软又亲肤"}]}
        analysis = {"shots": [self.shot], "vision_analysis": {"errors": []}}
        matched = {"ok": True, "manifest": manifest, "segments": [
            {"index": 1, "text": "普通介绍", "claim_gate": {"ok": True}},
            {"index": 2, "text": "柔软又亲肤", "claim_gate": {"ok": False, "status": "blocked"}}],
            "pending_items": [], "degraded_matches": []}
        with mock.patch.object(cli, "_run_shot_analysis", return_value=analysis), \
                mock.patch("orchestrator.material_corrections.apply", side_effect=lambda _m, a, _r: a), \
                mock.patch.object(shot_analyzer, "match_manifest", return_value=matched), \
                mock.patch("orchestrator.scene_rules.apply", return_value={}), \
                mock.patch.object(editor, "refill_indices", side_effect=lambda _m, _a, m, *_args, **_kw: m) as refill:
            cli._temporary_match(manifest, self.root / "input.json", self.root)
        self.assertEqual(refill.call_args.args[3], [2])

    def test_explicit_refill_keeps_prior_rule_selection_and_rejects_stale_inputs(self):
        from orchestrator import cli
        video = self.root / "prior.mp4"
        video.write_bytes(b"source")
        manifest = {"selection_mode": "rule_then_ai", "delivery_mode": "preview",
                    "draft_name": "new", "segments": [{"text": "第一句"}, {"text": "第二句"}],
                    "ai_refill_indices": [2]}
        rows = [{"index": i, "text": text, "shot_id": f"s{i}", "video": str(video),
                 "source_start": 0.0, "source_end": 1.0, "segment_s": 1.0}
                for i, text in enumerate(("第一句", "第二句"), 1)]
        prior = {"ok": True, "segments": rows, "manifest": {"segments": rows, "draft_name": "old"},
                 "pending_items": []}
        analysis = {"shots": [{"shot_id": f"s{i}", "video": str(video), "source_start": 0.0,
                               "source_end": 2.0, "status": "ready_for_matching"} for i in (1, 2)]}
        path = self.root / "shot_match_report.json"
        path.write_text(json.dumps(prior, ensure_ascii=False), encoding="utf-8")
        fresh = {"ok": True, "segments": [{**rows[0], "shot_id": "changed"}, rows[1]],
                 "manifest": manifest, "pending_items": []}
        def refill(m, a, selected, indices, *_args, **_kwargs):
            self.assertEqual(indices, [2])
            self.assertEqual(selected["segments"], rows)
            self.assertEqual(selected["manifest"]["draft_name"], "new")
            return selected
        with mock.patch.object(cli, "_run_shot_analysis", return_value=analysis), \
                mock.patch("orchestrator.material_corrections.apply", side_effect=lambda _m, a, _r: a), \
                mock.patch.object(shot_analyzer, "match_manifest", return_value=fresh), \
                mock.patch("orchestrator.scene_rules.apply", return_value={}), \
                mock.patch.object(editor, "refill_indices", side_effect=refill):
            result, blocker = cli._temporary_match(manifest, self.root / "input.json", self.root)
        self.assertIsNone(blocker)
        self.assertEqual(result["segments"][0], rows[0])
        stale = {**manifest, "segments": [{"text": "文案已变"}, {"text": "第二句"}]}
        self.assertEqual(cli._validate_preserved_match(stale, analysis, prior, [2], None), "PRIOR_SENTENCE_MISMATCH")
        self.assertEqual(cli._validate_preserved_match(manifest, {"shots": []}, prior, [2], None), "PRIOR_SOURCE_CATALOG_MISMATCH")
        changed_window = {**analysis, "shots": [{**analysis["shots"][0], "source_end": 0.5}, analysis["shots"][1]]}
        self.assertEqual(cli._validate_preserved_match(manifest, changed_window, prior, [2], None), "PRIOR_SOURCE_WINDOW_INVALID")
        changed_timing = {"rows": [{"audio_duration_us": 2000000}, {"audio_duration_us": 1000000}]}
        self.assertEqual(cli._validate_preserved_match(manifest, analysis, prior, [2], changed_timing), "PRIOR_AUDIO_TIMING_MISMATCH")
        bad = {**prior, "manifest": {"segments": []}}
        self.assertEqual(cli._validate_preserved_match(manifest, analysis, bad, [2], None), "PRIOR_SELECTION_IDENTITY_MISMATCH")

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
