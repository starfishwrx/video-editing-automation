"""Real selector branches behind the October human-review failures, offline."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import rulepack, scene_rules, semantic_gate, shot_analyzer as sa, timing_contract as tc
from orchestrator import demo_actions, usage_ledger, vision_analyzer


def shot(sid, **fields):
    row = dict(shot_id=sid, video=f"fixture/{sid}.mp4", source_start=0.0,
               source_end=12.0, duration=12.0, material_duration_us=12 * tc.US,
               status="ready_for_matching", action_complete=True, has_subject=True,
               role="product_display", evidence_strength="B", action_phase="static",
               head_waste=0.0, tail_waste=0.0, action="举起", object="产品包装",
               description="手持产品入画展示", visual_tags=["包装展示"],
               evidence_tags=["包装展示"],
               evidence_intervals=[dict(start=4.0, end=6.0, frame_time=5.0)])
    return {**row, **fields}


def manifest(texts=None, **fields):
    rows = [dict(text=text, audio_duration_us=2 * tc.US, timing_certainty="audio")
            for text in (texts or ["今天给大家推荐一款宝藏好物"])]
    return rulepack.apply_manifest_defaults(dict(
        segments=rows, category="纸品", product="纯水湿厕纸", delivery_mode="formal",
        head_trim_s=0.0, tail_trim_s=0.0, tail_pad_s=0.0, fps=30,
        **fields))


def analysis(shots, *scores):
    return dict(shots=shots, semantic_matches=[dict(index=index, top3=[
        dict(shot_id=sid, score=score, matched_claims=["手持产品展示"])
        for sid, score in row]) for index, row in enumerate(scores, 1)])


class SelectionFeedbackTests(unittest.TestCase):
    def _two_sentences(self, *, same_setup=False, alternative_score=95, feasible=False):
        common = "fixture/shared.mp4"
        first = shot("first", video=common, setup_id="same")
        best = shot("best", video="fixture/second.mp4" if same_setup else common,
                    setup_id="same" if same_setup else "other")
        bad = shot("bad", setup_id="different")
        if not feasible:
            bad.update(duration=0.25, source_end=0.25, material_duration_us=250000,
                       evidence_intervals=[dict(start=0.05, end=0.20, frame_time=0.1)])
        current = analysis([first, best, bad], [("first", 99), ("best", 20), ("bad", 20)],
                           [("first", 20), ("best", 99), ("bad", alternative_score)])
        result = sa.match_manifest(manifest(["推荐一款宝藏好物", "大家看看这个产品"],
            avoid_adjacent_source_repeat=not same_setup), current)
        self.assertTrue(result["ok"], result["pending_items"])
        return result

    def test_infeasible_other_source_cannot_evict_usable_evidence(self):
        self.assertEqual(self._two_sentences()["accepted_shots"], ["first", "best"])

    def test_infeasible_other_setup_cannot_evict_usable_evidence(self):
        self.assertEqual(self._two_sentences(same_setup=True)["accepted_shots"], ["first", "best"])

    def test_much_weaker_alternative_cannot_trigger_adjacent_ban(self):
        self.assertEqual(self._two_sentences(alternative_score=70, feasible=True)["accepted_shots"],
                         ["first", "best"])

    def test_equal_quality_near_tie_still_avoids_adjacent_source(self):
        self.assertEqual(self._two_sentences(alternative_score=95, feasible=True)["accepted_shots"],
                         ["first", "bad"])

    def _ledger_pick(self, low):
        high, fresh = shot("high"), shot("fresh")
        key = usage_ledger.shot_key(high["video"], 0)
        with mock.patch.object(usage_ledger, "load_usage", return_value={key: usage_ledger.SAME_TASK}):
            return sa.match_manifest(manifest(), analysis([high, fresh], [("high", 99), ("fresh", low)]))

    def test_unused_70_cannot_displace_used_99(self):
        self.assertEqual(self._ledger_pick(70)["accepted_shots"], ["high"])

    def test_near_tied_unused_candidate_can_win(self):
        self.assertEqual(self._ledger_pick(94)["accepted_shots"], ["fresh"])

    def test_report_first_candidate_matches_feasible_selection(self):
        bad = shot("bad", duration=0.25, source_end=0.25, material_duration_us=250000,
                   evidence_intervals=[dict(start=0.05, end=0.20, frame_time=0.1)])
        good = shot("good")
        result = sa.match_manifest(manifest(), analysis([bad, good], [("bad", 99), ("good", 90)]))
        self.assertEqual(result["accepted_shots"], ["good"])
        self.assertEqual(result["recommendations"][0]["top3"][0]["shot_id"], "good")
        self.assertFalse(result["recommendations"][0]["top3"][1]["usable"])
        self.assertEqual(result["segments"][0]["selection_margin_basis"], "same_usable_quality_band")

    def test_variant_rotation_is_reflected_in_report(self):
        a, b = shot("a"), shot("b")
        result = sa.match_manifest(manifest(variant_index=1),
                                  analysis([a, b], [("a", 99), ("b", 96)]))
        self.assertEqual(result["recommendations"][0]["top3"][0]["shot_id"], result["accepted_shots"][0])

    def test_successful_span_is_not_overwritten_by_single_shot_fallback(self):
        shots = [shot(sid, duration=3.0, source_end=3.0, material_duration_us=3 * tc.US,
                      evidence_intervals=[dict(start=0.8, end=1.8, frame_time=1.3)])
                 for sid in ("a", "b")]
        m = manifest()
        m["segments"][0]["audio_duration_us"] = 5 * tc.US
        result = sa.match_manifest(m, analysis(shots, [("a", 90), ("b", 89)]))
        self.assertTrue(result["ok"], result["pending_items"])
        segment = result["segments"][0]
        self.assertTrue(segment["multi_shot"])
        self.assertEqual(len(segment["span_parts"]), 2)
        self.assertFalse(segment["degraded_no_match"])

    def test_no_selling_point_opening_is_normal_display(self):
        result = sa.match_manifest(manifest(["生理期贴身用品选不好"]),
                                  analysis([shot("held")], [("held", 20)]))
        self.assertTrue(result["ok"], result["pending_items"])
        self.assertEqual(result["segments"][0]["selection_mode"], "auto_top1_display_carry")
        self.assertEqual(result["degraded_count"], 0)

    def test_multi_shot_cannot_bypass_functional_evidence_gate(self):
        shots = [shot(sid, duration=3.0, source_end=3.0, material_duration_us=3 * tc.US,
                      description="吸水纸张材质特写", visual_tags=["吸水", "材质特写"],
                      evidence_tags=["吸水"], evidence_intervals=[dict(start=0.8, end=1.8, frame_time=1.3)])
                 for sid in ("a", "b")]
        m = manifest(["瞬吸吸水更快"])
        m["segments"][0]["audio_duration_us"] = 5 * tc.US
        result = sa.match_manifest(m, analysis(shots, [("a", 95), ("b", 90)]))
        self.assertFalse(any(s.get("multi_shot") and not s.get("degraded_no_match")
                             for s in result["segments"]))

    def test_formal_related_fallback_remains_diagnostic_and_blocked(self):
        related = shot("related", description="纸张材质特写展示吸水结构",
                       evidence_tags=["吸水", "纸张特写"], visual_tags=["材质特写"])
        result = sa.match_manifest(manifest(["吸水性很好"]),
                                  analysis([related], [("related", 60)]))
        self.assertFalse(result["ok"])
        self.assertIn("FORMAL_EVIDENCE_REQUIRED", [p["type"] for p in result["pending_items"]])
        self.assertTrue(result["segments"][0]["formal_rejected"])

    def test_functional_lint_sentence_cannot_use_opening_display(self):
        result = sa.match_manifest(manifest(["怎么揉搓也不掉絮"]),
                                  analysis([shot("package")], [("package", 99)]))
        self.assertFalse(result["ok"])
        self.assertFalse(any(s.get("display_carry") for s in result["segments"]))

    def test_lint_focus_does_not_mistake_intact_stretch_for_rubbing(self):
        s = shot("rub", action="拉伸", result="保持完整", description="先拉伸再揉搓，再拉伸",
                 action_segments=[dict(start=0, end=4.67, action="横向拉伸", result="保持完整"),
                                  dict(start=4.67, end=7.35, action="揉搓捏合巾体", result=None),
                                  dict(start=7.35, end=9.35, action="再次拉伸", result="保持完整")],
                 evidence_intervals=[dict(start=2, end=4.5, frame_time=3)])
        c = demo_actions.sentence_contract("怎么揉搓也不掉絮", category="纸品")
        focused = sa.focus_on_topic(s, c)
        self.assertGreaterEqual(focused["evidence_intervals"][0]["start"], 4.67)
        self.assertLessEqual(focused["evidence_intervals"][0]["end"], 7.35)

    def test_wet_strength_focus_prefers_wet_action_over_dry_unfold(self):
        s = shot("wet", description="先展开干纸，随后浸水并提起湿纸仍完整",
                 action_segments=[dict(start=0.72, end=2.17, action="展开干燥巾体", result="巾体展开"),
                                  dict(start=3.62, end=5.65, action="将巾体按入水中浸湿后提起", result="湿透提起仍完整")],
                 evidence_intervals=[dict(start=3.62, end=5.65, frame_time=5.075)])
        c = demo_actions.sentence_contract("百分百原生木浆无荧光剂，湿水不破", category="纸品")
        focused = sa.focus_on_topic(s, c)
        self.assertEqual(focused["evidence_intervals"][0]["start"], 3.62)
        self.assertEqual(focused["evidence_intervals"][0]["end"], 5.65)

    def test_model_contradiction_cannot_receive_qualifying_score(self):
        current = analysis([shot("package")], [("package", 99)])
        current["semantic_matches"][0]["top3"][0]["contradictions"] = ["画面与要求相反"]
        result = sa.match_manifest(manifest(["140抽大容量"]), current)
        self.assertEqual(result["recommendations"][0]["top3"][0]["score"], 0)
        self.assertFalse(result["recommendations"][0]["top3"][0]["eligible"])
        self.assertEqual(result["accepted_shots"], [])

    def test_pack_count_140_pcs_beats_generic_multi_pack(self):
        label = shot("label", description="手指指向包装侧面140 PCS字样",
                     readable_claims=["140", "PCS"], evidence_tags=["140 PCS"],
                     action="手指指向文字", pointing_to_text=True)
        multi = shot("multi", description="四包产品成排静置", evidence_tags=["多包陈列"])
        result = sa.match_manifest(manifest(["每一包都有足足一百四十抽"]),
                                  analysis([label, multi], [("label", 86), ("multi", 20)]))
        self.assertEqual(result["accepted_shots"], ["label"])
        self.assertEqual(result["degraded_count"], 0)

    def test_layer_mark_survives_numeric_sku_and_final_semantic_check(self):
        label = shot("label", description="手指指向包装背面DT55080及4层字样",
                     readable_claims=["DT55080", "4层"], evidence_tags=["包装背标"],
                     action="手指指向文字", pointing_to_text=True)
        stretch = shot("stretch", role="usage_demo", action="拉伸揉搓",
                       description="双手拉伸揉搓纸巾，保持完整", evidence_tags=["拉伸", "厚实"])
        result = sa.match_manifest(manifest(["纸巾还是4层加厚的"]),
                                  analysis([label, stretch], [("label", 88), ("stretch", 20)]))
        self.assertEqual(result["accepted_shots"], ["label"])
        self.assertEqual(result["degraded_count"], 0)
        report = semantic_gate.evaluate_segments(result["segments"])
        self.assertTrue(report["ok"], report["issues"])
        self.assertEqual(report["coverage"][0]["evidence_basis"]["厚实"]["via"], "packaging_claim")

    def test_display_opening_preserves_later_unique_layer_evidence(self):
        label = shot("label", description="手指指向包装背面的4层字样",
                     readable_claims=["4层"], evidence_tags=["4层"],
                     action="手指指向文字", pointing_to_text=True)
        display = shot("display")
        result = sa.match_manifest(manifest(["谁能拒绝东方美学的抽纸", "纸巾还是4层加厚的"]),
            analysis([label, display], [("label", 90), ("display", 80)], [("label", 88)]))
        self.assertEqual(result["accepted_shots"], ["display", "label"])


class ContextSceneTests(unittest.TestCase):
    def test_fluorescence_continues_into_compatible_neutral_sentence(self):
        m = manifest(["不含荧光剂", "这样就放心了"])
        m["category"], m["product"] = "卫品", "日用卫生巾"
        s = shot("fluor", role="usage_demo", action="荧光测试", object="检测仪和巾体",
                 result="读数清晰显示", description="检测笔贴着巾体，荧光测试读数清晰可见",
                 evidence_tags=["荧光测试", "读数"], visual_tags=["荧光测试"])
        result = sa.match_manifest(m, analysis([s], [("fluor", 90)], [("fluor", 90)]))
        self.assertTrue(result["ok"], result["pending_items"])
        self.assertEqual(result["accepted_shots"], ["fluor", "fluor"])
        self.assertTrue(result["segments"][1]["scene_group_plan"]["continuation"])
        locate = mock.Mock(return_value=dict(ok=True, complete=True, phases={"action": [4.0, 7.0]}))
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(m, result, {"shots": [s]}, Path(tmp), locate=locate)
        rows = [r for r in report["rows"] if r["scene"] == "fluorescence"]
        self.assertEqual(rows[0]["status"], "applied")
        first, second = result["segments"]
        self.assertAlmostEqual(first["window_solution"]["source_end"], second["window_solution"]["source_start"], places=3)

    def test_absorb_does_not_swallow_independent_side_leak_or_cta(self):
        rules = rulepack.for_manifest({"category": "卫品"})
        rows = [dict(text=text, video="fixture", audio_duration_us=tc.US)
                for text in ["瞬吸", "防侧漏护边", "趁现在赶紧囤"]]
        groups = scene_rules._split_groups(rows, rules, {}, category="卫品")
        absorb = next(g for g in groups if g["scene"]["id"] == "absorb_test")
        self.assertEqual(absorb["positions"], [0])

    def test_shorter_complete_group_candidate_precedes_long_selected_source(self):
        scene = dict(id="test", kind="full_window", text_keywords=["荧光"],
                     shot_keywords=["荧光"], phases=[dict(name="action")])
        long, short = shot("long", duration=50, description="荧光测试"), shot("short", duration=7, description="荧光测试")
        members = [dict(temporary_shot_id="long")]
        rows = scene_rules._group_candidates(scene, members, [0], {},
                                              {"long": long, "short": short}, {"long"})
        self.assertEqual(rows[0][0]["shot_id"], "short")


class MatchPromptFeedbackTests(unittest.TestCase):
    def test_model_and_selector_share_sentence_indices_and_context(self):
        prompts = []
        def fake(_sheet, prompt, response, **_kwargs):
            prompts.append(prompt)
            return True, json.dumps([dict(index=1, top3=[dict(shot_id="s", score=80)]),
                                     dict(index=2, top3=[])])
        m = dict(category="卫品", segments=[{}, dict(text="荧光检测"), dict(text="这样就放心了")])
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"JY_VISION_TRANSPORT": "volc_glm"}), \
                mock.patch.object(vision_analyzer, "_run_volc_chat", side_effect=fake), \
                mock.patch.object(vision_analyzer, "_vision_slot"):
            vision_analyzer.match_with_codex(m, {"shots": [shot("s")]}, Path(tmp))
        self.assertIn("1. 荧光检测", prompts[0])
        self.assertIn("2. 这样就放心了", prompts[0])
        self.assertNotIn("3. ", prompts[0])
        self.assertIn("先通读完整口播", prompts[0])


if __name__ == "__main__":
    unittest.main()
