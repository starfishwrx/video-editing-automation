"""人工审稿语义回流：只测观察、合同与规则，不调用模型或媒体。"""
from __future__ import annotations

import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import demo_actions as da, rulepack


def contract(text, category="zhipin"):
    return da.sentence_contract(text, category=category)


def pointed(label, **fields):
    value = {"role": "product_display", "has_subject": True,
             "readable_claims": [label], "description": f"手指指向包装上的{label}字样",
             "action": "手指指向文字", "explicit_pointing": True}
    value.update(fields)
    return value


class CountSemanticsTests(unittest.TestCase):
    def test_chinese_numbers_include_zero_and_sections(self):
        for source, expected in (("一百四十", "140"), ("七十", "70"), ("二百零五", "205"),
                                 ("一万零三十", "10030"), ("二〇二", "202"), ("两千零二", "2002")):
            with self.subTest(source=source):
                self.assertEqual(da._chinese_count_to_arabic(source), expected)
        self.assertEqual(da.count_mentions("每一包都有足足一百四十抽"), ["140"])

    def test_single_pull_is_an_action_not_a_quantity(self):
        c = contract("单手一抽一张不连张")
        self.assertEqual(c["counts"], [])
        self.assertNotIn("quantity", c["intents"])
        self.assertIn("usage", c["intents"])
        self.assertNotIn("count", [topic["id"] for topic in c["topics"]])

    def test_chinese_spoken_count_blocks_wrong_pack(self):
        c = contract("一百四十抽大容量")
        self.assertEqual(c["sheet_counts"], ["140"])
        self.assertTrue(da.contradiction(pointed("70PCS"), c).startswith("count:"))
        self.assertEqual(da.shot_fit(pointed("140PCS"), c), 2)

    def test_readable_count_requires_same_unit(self):
        c = contract("一百四十抽大容量")
        self.assertGreater(da.count_readable_rank(pointed("140PCS"), c), 0)
        self.assertEqual(da.count_readable_rank(pointed("140层"), c), 0)
        self.assertEqual(da.shot_fit(pointed("140层"), c), 0)
        layer = contract("四层")
        self.assertGreater(da.count_readable_rank(pointed("4层"), layer), 0)
        self.assertEqual(da.count_readable_rank(pointed("4抽"), layer), 0)

    def test_unverified_text_never_boosts_count_rank(self):
        for state in ("pending", "failed"):
            with self.subTest(state=state):
                shot = pointed("140PCS", text_pass=state)
                self.assertEqual(da.count_readable_rank(shot, contract("140抽")), 0)

    def test_layer_aliases_match_but_wrong_layers_contradict(self):
        c = contract("四层")
        self.assertEqual(da.shot_fit(pointed("4层"), c), 2)
        self.assertTrue(da.contradiction(pointed("3层"), c).startswith("layers:"))


class PackagingBoundaryTests(unittest.TestCase):
    def test_pure_marks_remain_narrow_packaging_claims(self):
        for label, text in (("100%原生木浆", "百分百原生木浆"),
                            ("100%纯棉", "优选天然纯棉制作的面料"),
                            ("4层", "四层"), ("4层", "这是4层加厚")):
            with self.subTest(text=text):
                decision = da.packaging_claim_direct(pointed(label), text)
                self.assertTrue(decision["ok"], decision)
                self.assertTrue(decision["authenticity_not_verified"])

    def test_mixed_functional_claims_do_not_pass_on_marks(self):
        for label, text in (("100%原生木浆", "原生木浆而且柔软"),
                            ("100%原生木浆", "原生木浆揉搓不掉絮"),
                            ("100%纯棉", "纯棉吸收快"),
                            ("100%纯棉", "纯棉面层柔软"),
                            ("4层亲肤", "四层亲肤"),
                            ("4层", "4层加厚又柔软")):
            with self.subTest(text=text):
                decision = da.packaging_claim_direct(pointed(label), text)
                self.assertFalse(decision["ok"], decision)

    def test_unreadable_unpointed_and_wrong_layer_labels_are_rejected(self):
        for shot, text in ((pointed("4层", text_pass="failed"), "四层"),
                           (pointed("4层", explicit_pointing=False), "四层"),
                           (pointed("3层"), "四层")):
            with self.subTest(shot=shot):
                self.assertFalse(da.packaging_claim_direct(shot, text)["ok"])

    def test_fit_and_soft_claim_contracts_still_require_evidence(self):
        self.assertTrue(da.CLAIMS_BY_ID["C-FIT"].evidence_required)
        self.assertTrue(da.CLAIMS_BY_ID["C-SOFT"].evidence_required)
        self.assertFalse(da.judge_claim(pointed("柔软"), da.CLAIMS_BY_ID["C-SOFT"])[0])


class FeedbackTopicTests(unittest.TestCase):
    def test_lint_and_layers_are_functional_topics(self):
        for text, topic in (("怎么揉搓也不掉絮", "lint_free"), ("四层", "layers")):
            c = contract(text)
            self.assertEqual(c["kind"], "functional")
            self.assertIn(topic, [row["id"] for row in c["topics"]])

    def test_known_action_and_print_topics_cannot_be_rescued_by_ai_score(self):
        for topic in ("lint_free", "texture", "layers", "count", "cotton", "surface"):
            self.assertIn(topic, da.KEYWORD_LOCKED_TOPICS)
        pack = {"description": "手持产品入画展示", "visual_tags": ["包装展示"],
                "role": "product_display", "has_subject": True}
        self.assertEqual(da.shot_fit(pack, contract("揉搓也不掉絮")), 0)

    def test_stuffy_does_not_mean_soft_or_chafing(self):
        ids = da.infer_claim_ids("很容易闷闷的不舒服")
        self.assertIn("C-BREATHABLE", ids)
        self.assertNotIn("C-SOFT", ids)
        self.assertNotIn("C-NO-CHAFE", ids)

    def test_display_opening_and_topic_helpers(self):
        c = contract("生理期贴身用品选不好", "weipin")
        self.assertEqual(c["kind"], "display")
        self.assertIn("opening", c["intents"])
        hand = {"description": "手持产品入画并举起"}
        static = {"description": "未拆封展示完整展示包装"}
        self.assertGreater(da.opening_display_rank(hand), da.opening_display_rank(static))
        self.assertGreater(da.lint_action_rank({"action": "揉搓"}),
                           da.lint_action_rank({"action": "托展翻转"}))
        self.assertGreater(da.surface_action_rank({"action": "抚摸面层"}),
                           da.surface_action_rank({"action": "撕开包装"}))

    def test_breath_absorb_and_side_leak_remain_distinct(self):
        for text, expected, forbidden in (("透气不闷", "breath", "absorb"),
                                          ("吸收更快", "absorb", "breath"),
                                          ("立体护边防侧漏", "side_leak", "absorb")):
            ids = {row["id"] for row in contract(text, "weipin")["topics"]}
            self.assertIn(expected, ids)
            self.assertNotIn(forbidden, ids)


class FluorescenceAndContinuityTests(unittest.TestCase):
    def test_device_name_does_not_invent_reading_or_test(self):
        for category in ("weipin", "zhipin"):
            for description in ("荧光检测仪放在桌上", "检测设备贴着巾体，屏幕有数字",
                                "检测笔贴着巾体照射，未见读数", "厨房计时器显示红色数字"):
                with self.subTest(category=category, description=description):
                    shot = {"description": description, "evidence_tags": [], "visual_tags": []}
                    added = rulepack.annotate_shot_labels(shot, rulepack.load(category))
                    self.assertNotIn("读数", added)
                    self.assertNotIn("荧光测试", added)

    def test_observed_reading_can_be_derived_but_keeps_provenance(self):
        shot = {"description": "检测笔贴着巾体照射，屏幕出现荧光测试读数",
                "evidence_tags": [], "visual_tags": []}
        self.assertIn("读数", rulepack.annotate_shot_labels(shot, rulepack.load("weipin")))
        self.assertEqual(shot["shot_label_sources"]["读数"], "derived:fluorescence")
        unverified = {"description": shot["description"], "text_pass": "pending"}
        self.assertNotIn("读数", rulepack.annotate_shot_labels(unverified, rulepack.load("weipin")))

    def test_group_contracts_preserve_whole_actions_without_fixed_sentence_cap(self):
        rules = rulepack.load("weipin")
        for sid in ("fluorescence", "ultra_thin"):
            scene = next(s for s in rules["scenes"] if s["id"] == sid)
            self.assertEqual(scene["kind"], "full_window")
            self.assertEqual(scene["group_max"], 0)
            self.assertIn("开始", scene["phases"][0]["describe"])
            self.assertIn("完整", scene["phases"][0]["describe"])
        absorb = next(s for s in rules["scenes"] if s["id"] == "absorb_test")
        self.assertFalse(set(absorb["follow_keywords"]) & {"侧漏", "防漏", "不漏"})

    def test_pack_in_background_does_not_force_another_unpack(self):
        scene = next(s for s in rulepack.load("weipin")["scenes"] if s["id"] == "tear_pack")
        shot = {"description": "背景有包装，手掌抚摸面层",
                "action": "抚摸面层", "evidence_tags": ["完整展示包装", "面层卖点"]}
        self.assertFalse(rulepack.static_pack_shot(scene, shot))


if __name__ == "__main__":
    unittest.main()
