"""Evidence order and bounded full-action search, without media/model calls."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from test_v156_selection_feedback import analysis, manifest, shot
from orchestrator import rulepack, scene_rules, semantic_gate, shot_analyzer as sa, usage_ledger


class EvidenceOrderTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(usage_ledger, "load_usage", return_value={})
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(usage_ledger, "other_task_counts", return_value={})
        patch.start()
        self.addCleanup(patch.stop)
        self.exact = shot("direct", description="手指捏住纸张，展示纸张厚实",
                          action="捏住纸张展示厚度", object="纸张", result="厚实可见",
                          visual_tags=["厚实展示"], evidence_tags=["厚实"], role="usage_demo")
        self.related = shot("related", description="展示纸张材质纹理特写",
                            action="展示纸张纹理", object="纸张", result="纹理可见",
                            visual_tags=["材质特写"], evidence_tags=["材质特写"])

    def test_lower_semantic_exact_candidate_is_not_evicted_by_related_score(self):
        result = sa.match_manifest(manifest(["这款纸张很厚实"]),
                                   analysis([self.exact, self.related], [("direct", 60), ("related", 95)]))
        self.assertEqual(result["accepted_shots"], ["direct"])
        self.assertFalse(result["ok"], "低语义分精准候选仍应待审，不靠标签伪造通过")
        self.assertTrue(result["segments"][0]["formal_rejected"])
        top = result["recommendations"][0]["top3"]
        self.assertEqual((top[0]["shot_id"], top[0]["match_tier"]), ("direct", "exact"))
        self.assertEqual(top[1]["match_tier"], "fuzzy")

    def test_qualified_exact_candidate_precedes_higher_related_score(self):
        result = sa.match_manifest(manifest(["这款纸张很厚实"]),
                                   analysis([self.exact, self.related], [("direct", 85), ("related", 99)]))
        self.assertTrue(result["ok"], result["pending_items"])
        self.assertEqual(result["accepted_shots"], ["direct"])
        self.assertEqual(result["degraded_count"], 0)

    def test_high_related_score_still_requires_formal_review(self):
        result = sa.match_manifest(manifest(["这款纸张很厚实"]),
                                   analysis([self.related], [("related", 99)]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["degraded_matches"][0]["match_tier"], "fuzzy")
        self.assertTrue(result["segments"][0]["formal_rejected"])

    def test_preview_keeps_association_as_explicit_degraded(self):
        m = manifest(["这款纸张很厚实"])
        m["delivery_mode"] = "preview"
        result = sa.match_manifest(m, analysis([self.related], [("related", 99)]))
        self.assertTrue(result["ok"], result["pending_items"])
        self.assertTrue(result["segments"][0]["degraded_no_match"])
        self.assertFalse(result["segments"][0]["formal_rejected"])

    def test_mixed_direct_and_associated_claims_cannot_be_reported_exact(self):
        candidate = {**self.exact, "evidence_tags": ["厚实", "材质特写"]}
        result = sa.match_manifest(manifest(["纸张厚实又耐用"]),
                                   analysis([candidate], [("direct", 95)]))
        self.assertEqual(result["recommendations"][0]["top3"][0]["match_tier"], "fuzzy")
        self.assertFalse(result["ok"])

    def test_low_confidence_is_still_blocked_at_final_semantic_gate(self):
        segment = {**self.exact, "text": "纸张很厚实", "claim_text": "纸张很厚实",
                   "analysis_confidence": 0.01}
        result = semantic_gate.evaluate_segments([segment])
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["type"], "visual_observation_unreliable")

    def test_independent_package_text_proof_survives_selector_and_semantic_gate(self):
        label = shot("label", action="手指指向文字", pointing_to_text=True,
                     description="手指指向包装4层字样", readable_claims=["4层"],
                     evidence_tags=["4层"], visual_tags=["包装背标"], analysis_confidence=0.01,
                     text_pass="hires", text_agreed=["4层"], pointed_agreed=["4层"])
        result = sa.match_manifest(manifest(["纸巾还是4层加厚的"]),
                                   analysis([label], [("label", 20)]))
        self.assertTrue(result["ok"], result["pending_items"])
        segment = result["segments"][0]
        self.assertEqual(segment["analysis_confidence"], 0.01)
        self.assertEqual(segment["text_agreed"], ["4层"])
        checked = semantic_gate.evaluate_segments([segment])
        self.assertTrue(checked["ok"], checked["issues"])

    def test_scene_replacement_does_not_inherit_previous_observation_confidence(self):
        replacement = {**self.related, "analysis_confidence": 0.01}
        fields = scene_rules._shot_fields(replacement)
        segment = {**self.exact, "analysis_confidence": 0.99, **fields,
                   "text": "纸张很厚实", "claim_text": "纸张很厚实"}
        self.assertEqual(segment["analysis_confidence"], 0.01)
        self.assertFalse(semantic_gate.evaluate_segments([segment])["ok"])

    def test_absorb_group_cannot_override_other_unmet_functional_claims(self):
        candidate = shot("absorb", role="direct_evidence", evidence_strength="A",
                         action="倾倒", object="液体+巾体", result="液体被吸入",
                         description="倾倒液体，巾体迅速吸水",
                         evidence_tags=["吸水", "柔软"], visual_tags=["倒水"])
        m = manifest(["瞬吸吸水又柔软亲肤"])
        m.update(category="卫品", product="日用卫生巾")
        result = sa.match_manifest(m, analysis([candidate], [("absorb", 99)]))
        self.assertFalse(result["ok"])
        self.assertTrue(all(s.get("formal_rejected") for s in result["segments"]))

    def test_undefined_contract_is_reported_separately_from_missing_material(self):
        candidate = shot("surface", role="usage_demo", evidence_strength="A", action="抚摸面层",
                         object="巾体", result="表面纹理可见", description="抚摸柔软面层",
                         evidence_tags=["柔软"], visual_tags=["面层特写"])
        m = manifest(["面层柔软"])
        m.update(category="卫品", product="日用卫生巾")
        result = sa.match_manifest(m, analysis([candidate], [("surface", 99)]))
        self.assertFalse(result["ok"])
        self.assertEqual(result["material_gap_count"], 0)
        self.assertEqual(result["contract_issue_count"], 1)
        self.assertEqual(result["contract_issues"][0]["type"], "CONTRACT_UNDEFINED")

    def test_absorb_scene_cannot_override_real_bulk_cta(self):
        candidate = shot("absorb", role="direct_evidence", evidence_strength="A",
                         action="倾倒", object="液体+巾体", result="液体被吸入",
                         description="倾倒液体，巾体迅速吸水", evidence_tags=["吸水"], visual_tags=["倒水"])
        m = manifest(["瞬吸吸水快，赶紧囤一箱"])
        m.update(category="卫品", product="日用卫生巾")
        result = sa.match_manifest(m, analysis([candidate], [("absorb", 99)]))
        self.assertFalse(result["ok"])
        self.assertFalse(result["recommendations"][0]["top3"][0]["cta_visual_ok"])

    def test_absorption_capacity_context_does_not_become_purchase_quantity(self):
        from orchestrator import demo_actions
        self.assertTrue(demo_actions.absorb_quantity_context("吸量大", "卫品"))
        self.assertFalse(demo_actions.absorb_quantity_context("吸量大，一箱12包", "卫品"))
        self.assertFalse(demo_actions.absorb_quantity_context("吸收快，趁活动赶紧囤", "卫品"))


class FullActionSearchTests(unittest.TestCase):
    @staticmethod
    def fluor(sid, seconds, **fields):
        return shot(sid, duration=seconds, source_end=seconds,
                    material_duration_us=int(seconds * 1_000_000), role="usage_demo",
                    action="荧光测试", object="检测仪和巾体", result="读数清晰显示",
                    description="检测笔贴着巾体，荧光测试读数清晰可见",
                    visual_tags=["荧光测试"], evidence_tags=["荧光测试", "读数"], **fields)

    def test_single_sentence_search_keeps_feasible_short_source_before_pool_limit(self):
        m = manifest(["不含荧光剂"])
        m.update(category="卫品", product="日用卫生巾")
        longs = [self.fluor(f"long{i}", 20 + i) for i in range(12)]
        short = self.fluor("short", 3, evidence_intervals=[dict(start=1, end=2, frame_time=1.5)])
        shots = [*longs, short]
        source = analysis(shots, [(s["shot_id"], 95 if i == 0 else 90-i) for i, s in enumerate(shots)])
        with mock.patch.object(usage_ledger, "load_usage", return_value={}), \
                mock.patch.object(usage_ledger, "other_task_counts", return_value={}):
            matched = sa.match_manifest(m, source)
            self.assertTrue(matched["ok"], matched["pending_items"])
            self.assertEqual(matched["accepted_shots"], ["long0"])
            with tempfile.TemporaryDirectory() as tmp:
                report = scene_rules.apply(m, matched, source, Path(tmp), locate=lambda s, *args:
                    dict(ok=True, complete=True, phases={"action": [0.4, s["source_end"]-0.4]}))
        row = report["rows"][0]
        self.assertEqual(row["status"], "applied", row)
        self.assertEqual(row["shot_id"], "short")
        self.assertLessEqual(matched["segments"][0]["video_speed"], 1.6)

    def test_group_pool_truncation_keeps_short_source_even_with_fewer_keywords(self):
        scene = dict(id="fluor", kind="full_window", shot_keywords=["荧光", "检测笔", "读数"],
                     pool_search_limit=2)
        longs = [self.fluor(f"long{i}", 20+i) for i in range(6)]
        short = self.fluor("short", 3)
        short.update(description="荧光", action="展示", object="产品", result=None,
                     evidence_tags=[], visual_tags=[])
        rows = scene_rules._group_candidates(scene, [], [], {},
                                             {s["shot_id"]: s for s in [*longs, short]}, set())
        self.assertEqual(rows[0][0]["shot_id"], "short")
        self.assertEqual(len(rows), 2)

    def test_short_source_still_requires_complete_verified_action(self):
        m = manifest(["不含荧光剂"])
        m.update(category="卫品", product="日用卫生巾")
        short = self.fluor("short", 3)
        scene = next(s for s in rulepack.for_manifest(m)["scenes"] if s["id"] == "fluorescence")
        with tempfile.TemporaryDirectory() as tmp:
            result = scene_rules._plan_for(scene, m["segments"][0], short, swapped=True,
                manifest=m, report_dir=Path(tmp), locate=lambda *args:
                    dict(ok=True, complete=False, phases={}))
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
