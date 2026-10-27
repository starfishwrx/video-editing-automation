from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import rulepack  # noqa: E402


class PaperRulepackTests(unittest.TestCase):
    def test_paper_defaults_and_explicit_overrides(self):
        defaults = rulepack.apply_manifest_defaults({"product": "绒立方厨房纸"})
        self.assertEqual(defaults["category"], "zhipin")
        self.assertEqual(defaults["voice_tempo"], 1.3)
        self.assertTrue(defaults["voice_edge_trim"])
        self.assertEqual(defaults["rulepack"]["voice_max_gap_frames"], 1)
        self.assertEqual(defaults["rulepack"]["voice_acceptance_max_gap_frames"], 5)
        self.assertFalse(defaults["auto_transition"])
        self.assertEqual(defaults["subtitle_position_px"], {"x": 0, "y": -800})
        overridden = rulepack.apply_manifest_defaults({
            "product": "奶被纸", "voice_tempo": 1.15,
            "subtitle_position_px": {"x": 12, "y": -700}, "auto_transition": True,
        })
        self.assertEqual(overridden["voice_tempo"], 1.15)
        self.assertEqual(overridden["subtitle_position_px"], {"x": 12, "y": -700})
        self.assertTrue(overridden["auto_transition"])
        named = rulepack.apply_manifest_defaults({"product": "纸巾", "voice": "女声1.2倍速"})
        self.assertEqual(named["voice_tempo"], 1.2)

    def test_all_thirteen_contracts_and_intent_separation(self):
        context = rulepack.editorial_context({"category": "纸品"})
        self.assertEqual(set(context), {"category", "versions", "rules"})
        contracts = context["rules"]["semantic_contracts"]
        ids = {c["id"] for c in contracts}
        self.assertEqual(len(contracts), 13)
        self.assertTrue({
            "wipe_face_hands", "absorb_squeeze_clear_water", "lift_from_water",
            "cleaning_wipe", "ef_plain_both_sides", "fluorescence_zero_test",
            "lint_rub_unfold", "tensile_both_directions", "imported_wood_fiber_label",
            "medical_grade_package_claims", "count_140", "washcloth_mop_install",
            "five_packs_countable",
        }.issubset(ids))
        fluoro = next(c for c in contracts if c["id"] == "fluorescence_zero_test")
        label = next(c for c in contracts if c["id"] == "medical_grade_package_claims")
        self.assertIn("0.00", fluoro["evidence"])
        self.assertIn("包装标签", label["evidence"])
        self.assertEqual(next(c for c in contracts if c["id"] == "cleaning_wipe")["preferred_speed"], 1.3)
        by_id = {c["id"]: c for c in contracts}
        self.assertIn("毛巾", by_id["lift_from_water"]["when"])
        self.assertIn("两面", by_id["ef_plain_both_sides"]["when"])
        self.assertIn("仅当本句明确说两面", by_id["ef_plain_both_sides"]["when"])
        self.assertIn("没有可见浮毛或毛絮", by_id["lint_rub_unfold"]["evidence"])
        self.assertIn("拖把", by_id["washcloth_mop_install"]["when"])
        self.assertIn("单说“送洗脸巾”不适用", by_id["washcloth_mop_install"]["when"])
        self.assertIn("EF压纹、平纹或面料", by_id["ef_plain_both_sides"]["when"])
        self.assertIn("不强制重排现有口播", " ".join(context["rules"]["production_rules"]))
        self.assertIn("不涉及音色或语音倍速", " ".join(context["rules"]["production_rules"]))
        self.assertTrue(all(c["preferred_speed"] == 1.0 for c in contracts if c["id"] != "cleaning_wipe"))
        legacy = " ".join(rulepack.prompt_rules({"category": "zhipin"}, "match"))
        self.assertIn("包装标称", legacy)
        self.assertIn("包装标称（医护级、0致敏、0荧光）", legacy)
        self.assertIn("0.00", legacy)
        self.assertIn("价格/量感", legacy)
        self.assertNotIn("必须配检测笔/检测仪", legacy)
        self.assertIn("包装标称", legacy)
        fluorescence = next(s for s in rulepack.load("zhipin")["scenes"] if s["id"] == "fluorescence")
        self.assertNotIn("荧光", fluorescence["text_keywords"])

    def test_category_isolation(self):
        paper = rulepack.apply_manifest_defaults({"product": "纸巾"})
        hygiene = rulepack.apply_manifest_defaults({"product": "卫生巾"})
        self.assertEqual(paper["voice_tempo"], 1.3)
        self.assertNotIn("subtitle_position_px", hygiene)
        self.assertNotIn("voice_acceptance_max_gap_frames", hygiene["rulepack"])
        self.assertNotIn("wipe_face_hands", {c["id"] for c in
                         rulepack.editorial_context({"category": "weipin"})["rules"]["semantic_contracts"]})

    def test_analysis_observes_every_contract_and_invalidates_old_cache(self):
        manifest = {"category": "zhipin"}
        contracts = rulepack.editorial_context(manifest)["rules"]["semantic_contracts"]
        analysis = "\n".join(rulepack.prompt_rules(manifest, "analysis"))
        cache_identity = "\n".join(rulepack.keyed_analysis_rules(manifest))
        for scene in contracts:
            self.assertIn(scene["id"], analysis)
            self.assertIn(scene["evidence"], analysis)
            self.assertIn(scene["id"], cache_identity)
        self.assertIn("不得按规范补全", analysis)
        self.assertNotIn("count_140", "\n".join(rulepack.prompt_rules({"category": "weipin"}, "analysis")))

    def test_identity_changes_when_effective_rules_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("global", "zhipin"):
                original = (rulepack.RULES_DIR / f"{name}.json").read_text(encoding="utf-8")
                (root / f"{name}.json").write_text(original, encoding="utf-8")
            with mock.patch.object(rulepack, "RULES_DIR", root):
                rulepack._read_rules.cache_clear()
                initial = rulepack.editorial_context({"category": "zhipin"})
                paper_file = root / "zhipin.json"
                data = json.loads(paper_file.read_text(encoding="utf-8"))
                data["version"] = "test-next"
                data["editorial_contract"]["visual_preferences"].append("身份变化测试规则")
                paper_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                rulepack._read_rules.cache_clear()
                changed = rulepack.editorial_context({"category": "zhipin"})
                rulepack._read_rules.cache_clear()
        self.assertNotEqual(initial["versions"], changed["versions"])
        self.assertNotEqual(initial["rules"], changed["rules"])


if __name__ == "__main__":
    unittest.main()
