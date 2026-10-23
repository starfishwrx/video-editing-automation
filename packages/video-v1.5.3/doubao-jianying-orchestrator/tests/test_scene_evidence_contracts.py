"""末端语义闸门须对场景换镜后的当前观察重做品类合同判定。"""
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import rulepack, scene_rules, semantic_gate, shot_analyzer, usage_ledger


def shot(sid, **fields):
    row = dict(shot_id=sid, video=f"fixture/{sid}.mp4", source_start=0.0,
               source_end=12.0, duration=12.0, material_duration_us=12_000_000,
               status="ready_for_matching", action_complete=True, has_subject=True,
               role="product_display", evidence_strength="B", action_phase="static",
               head_waste=0.0, tail_waste=0.0, action="举起", object="产品包装",
               description="手持产品入画展示", visual_tags=["包装展示"],
               evidence_tags=["包装展示"], analysis_confidence=0.99,
               evidence_intervals=[dict(start=0.8, end=1.8, frame_time=1.3)])
    return {**row, **fields}


def manifest(texts, category="卫品"):
    return rulepack.apply_manifest_defaults(dict(
        segments=[dict(text=text, claim_text=text, audio_duration_us=2_000_000,
                       timing_certainty="audio") for text in texts],
        category=category, product="日用卫生巾", delivery_mode="formal",
        head_trim_s=0.0, tail_trim_s=0.0, tail_pad_s=0.0, fps=30))


def analysis(shots, scores):
    return dict(shots=shots, semantic_matches=[dict(index=i, top3=[
        dict(shot_id=sid, score=score, matched_claims=["产品展示"])
        for sid, score in row]) for i, row in enumerate(scores, 1)])


class SceneEvidenceContractTests(unittest.TestCase):
    def _single_roundtrip(self, direct, replacement):
        m = manifest(["这个产品超薄"], category="纸品")
        m.update(category="卫品", product="日用卫生巾")
        source = analysis([direct, replacement], [[(direct["shot_id"], 95),
                                                    (replacement["shot_id"], 90)]])
        with mock.patch.object(usage_ledger, "load_usage", return_value={}), \
                mock.patch.object(usage_ledger, "other_task_counts", return_value={}):
            matched = shot_analyzer.match_manifest(m, source)
        with tempfile.TemporaryDirectory() as tmp:
            def locate(s, *_args):
                end = 11.6 if s.get("shot_id") == "direct" else 2.6
                return dict(ok=True, complete=True, phases={"action": [0.4, end]})
            scene_report = scene_rules.apply(m, matched, source, Path(tmp), locate=locate)
        return matched["segments"][0], scene_report

    def test_single_scene_swap_rejudges_current_evidence_and_keeps_good_shot(self):
        good = shot("good", duration=12, source_end=12, material_duration_us=12_000_000,
                    role="usage_demo", evidence_strength="A", action="捏薄巾体边缘",
                    object="巾体+手", result="与手同框厚度对比可见",
                    description="捏薄，与手同框厚度对比可见",
                    visual_tags=["捏薄", "厚度对比"], evidence_tags=["捏薄", "厚度对比"])
        selected, _ = self._single_roundtrip(good, shot("unused"))
        self.assertEqual(selected["temporary_shot_id"], "good")
        passed = semantic_gate.evaluate_segments([selected], category="卫品")
        self.assertTrue(passed["ok"], passed["issues"])
        self.assertTrue(passed["coverage"][0]["claim_contract"]["ok"])

    def test_single_scene_swap_cannot_reuse_stale_claim_gate(self):
        good = shot("direct", role="usage_demo", evidence_strength="A",
                    action="捏薄巾体边缘", object="巾体+手",
                    result="与手同框厚度对比可见",
                    description="捏薄，与手同框厚度对比可见",
                    visual_tags=["捏薄", "厚度对比"], evidence_tags=["捏薄", "厚度对比"])
        bad = shot("bad", duration=3, source_end=3, material_duration_us=3_000_000,
                   role="product_display", evidence_strength="B", action="举起巾体对光",
                   object="巾体+灯光", result="光线透过巾体",
                   description="对光展示，光线透过巾体", visual_tags=["透光"],
                   evidence_tags=["透光"])
        selected, scene_report = self._single_roundtrip(good, bad)
        self.assertEqual(selected["temporary_shot_id"], "bad", scene_report["rows"])
        selected["claim_gate"] = {"ok": True, "status": "ok"}
        report = semantic_gate.evaluate_segments([selected], category="卫品")
        self.assertFalse(report["ok"])
        self.assertEqual(report["coverage"][0]["claim_contract"]["reason"], "ROLE_BELOW_REQUIRED")

    def _group_roundtrip(self, *, include_weak_replacement):
        m = manifest(["瞬吸更快", "瞬吸依旧很快"])
        good1 = shot("g1", role="direct_evidence", evidence_strength="A",
                     action="倾倒液体", object="液体和巾体",
                     result="液体被吸入并锁在中央不扩散",
                     description="向巾体倾倒蓝色液体后被吸收",
                     visual_tags=["倾倒蓝色液体", "液体被吸收"],
                     evidence_tags=["倾倒蓝色液体", "液体被吸收"])
        good2 = {**good1, "shot_id": "g2", "video": "fixture/g2.mp4"}
        candidates = [good1, good2]
        ranks = [[("g1", 95), ("g2", 90)], [("g2", 95), ("g1", 90)]]
        if include_weak_replacement:
            weak = shot("weak", duration=5, source_end=5, material_duration_us=5_000_000,
                        role="product_display", evidence_strength="B",
                        action="倾倒蓝色液体", object="液体和巾体",
                        result="液体停在表面，未见吸收",
                        description="向巾体倾倒蓝色液体后停在表面，未见吸收",
                        visual_tags=["倾倒蓝色液体"], evidence_tags=["倾倒蓝色液体"])
            candidates.append(weak)
            ranks = [[("g1", 95), ("weak", 80)], [("g2", 95), ("weak", 80)]]
        source = analysis(candidates, ranks)
        with mock.patch.object(usage_ledger, "load_usage", return_value={}), \
                mock.patch.object(usage_ledger, "other_task_counts", return_value={}):
            matched = shot_analyzer.match_manifest(m, source)
        self.assertTrue(matched["ok"], matched["pending_items"])

        def locate(candidate, *_args):
            if candidate.get("shot_id") == "weak":
                phases = {"pour": [0.4, 1.4], "absorb": [1.4, 4.2], "wipe": [4.2, 4.8]}
            else:
                phases = {"pour": [0.4, 2.4], "absorb": [2.4, 5.4], "wipe": [5.4, 5.8]}
            return {"ok": True, "complete": True, "phases": phases}

        with tempfile.TemporaryDirectory() as tmp:
            scene_report = scene_rules.apply(m, matched, source, Path(tmp), locate=locate)
        self.assertEqual(scene_report["rows"][0]["scene"], "absorb_test")
        self.assertEqual(scene_report["rows"][0]["status"], "applied")
        return matched["segments"], scene_report

    def test_group_scene_apply_preserves_trusted_evidence(self):
        segments, _ = self._group_roundtrip(include_weak_replacement=False)
        report = semantic_gate.evaluate_segments(segments, category="卫品")
        self.assertTrue(report["ok"], report["issues"])
        self.assertTrue(all(row["claim_contract"]["ok"] for row in report["coverage"]))

    def test_group_scene_replacement_rechecks_every_sentence_against_replacement(self):
        segments, _ = self._group_roundtrip(include_weak_replacement=True)
        self.assertEqual({row["temporary_shot_id"] for row in segments}, {"weak"})
        report = semantic_gate.evaluate_segments(segments, category="卫品")
        self.assertFalse(report["ok"])
        self.assertTrue(all(row["claim_contract"]["reason"] == "ROLE_BELOW_REQUIRED"
                            for row in report["coverage"]), report["coverage"])

    def test_explicit_category_blocks_undefined_or_ungated_function_observations(self):
        row = {"text": "柔软亲肤", "claim_text": "柔软亲肤", "role": "usage_demo",
               "has_subject": True, "action": "抚摸面层", "result": "", "description": "面层特写"}
        report = semantic_gate.evaluate_segments([row], category="卫品")
        contract = report["coverage"][0]["claim_contract"]
        self.assertFalse(contract["ok"])
        self.assertEqual(contract["reason"], "CONTRACT_UNDEFINED")

    def test_cta_replacement_requires_visible_purchase_quantity(self):
        row = {"text": "趁现在赶紧囤一箱", "claim_text": "趁现在赶紧囤一箱",
               "role": "product_display", "has_subject": True,
               "description": "单包包装正面展示", "visual_description": "单包包装"}
        report = semantic_gate.evaluate_segments([row], category="卫品")
        self.assertFalse(report["ok"])
        self.assertEqual(report["coverage"][0]["quantity_contract"]["reason"],
                         "SINGLE_PACK_OR_NO_VISIBLE_QUANTITY")
        self.assertIn("cta_quantity_evidence_missing", [issue["type"] for issue in report["issues"]])

    def test_absorption_amount_context_is_not_misread_as_purchase_quantity(self):
        row = {"text": "吸水量大，吸收很快", "claim_text": "吸水量大，吸收很快",
               "role": "direct_evidence", "evidence_strength": "A", "has_subject": True,
               "analysis_confidence": 0.99, "action": "倾倒液体", "object": "液体和巾体",
               "result": "液体被吸入并锁在中央不扩散",
               "description": "向巾体倾倒液体，液体迅速吸收并锁在中央",
               "visual_tags": ["倾倒液体", "吸收"], "evidence_tags": ["倾倒液体", "吸收"]}
        report = semantic_gate.evaluate_segments([row], category="卫品")
        self.assertTrue(report["coverage"][0]["quantity_contract"]["ok"])
        self.assertNotIn("cta_quantity_evidence_missing", [issue["type"] for issue in report["issues"]])

    def test_legacy_semantic_call_without_category_remains_compatible(self):
        row = {"text": "今天看看", "visual_tags": ["产品展示"], "visual_description": "产品展示"}
        self.assertTrue(semantic_gate.evaluate_segments([row])["ok"])


if __name__ == "__main__":
    unittest.main()
