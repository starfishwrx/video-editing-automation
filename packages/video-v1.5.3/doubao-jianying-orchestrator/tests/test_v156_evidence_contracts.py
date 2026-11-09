"""观察、包装规格、购买量感和物理功能合同的正反边界；无需模型或媒体。"""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import demo_actions as da


def physical(**fields):
    row = {"role": "direct_evidence", "has_subject": True, "evidence_strength": "A",
           "analysis_confidence": 0.99, "action": "空气持续穿过巾体",
           "object": "巾体和气流装置", "result": "气流穿透巾体可见"}
    return {**row, **fields}


def label(**fields):
    row = {"role": "product_display", "has_subject": True,
           "readable_claims": ["4层"], "description": "手指指向包装4层字样",
           "pointing_to_text": True, "action": "手指指向文字"}
    return {**row, **fields}


class PurchaseQuantityContractTests(unittest.TestCase):
    def test_internal_content_and_single_item_cannot_prove_bulk_cta(self):
        for description in ("只展示一包产品，包装正面可读140抽", "单包包装140PCS",
                            "只展示1包产品，单包静态特写", "只有一包140抽纸巾",
                            "一卷纸巾单件特写", "包装印着12包", "包装正面可读6包",
                            "库存充足，适合囤货", "空纸箱放在桌上", "一包纸巾平铺在桌面",
                            "手提袋放在桌面", "纸箱包装特写"):
            with self.subTest(description=description):
                shot = {"has_subject": True, "role": "product_display", "description": description}
                self.assertFalse(da.cta_quantity_eligible(shot, "趁现在赶紧囤一箱", "纸品")[0])

    def test_multiple_purchase_units_remain_usable(self):
        for description in ("多包产品成排陈列", "三包产品放在桌上", "2包产品举在手里",
                            "6卷产品整齐摆放", "手托一提产品", "打开整箱产品展示",
                            "桌上堆叠着产品", "镜头展示1箱产品"):
            with self.subTest(description=description):
                self.assertTrue(da.cta_quantity_eligible(
                    {"description": description}, "趁现在赶紧囤一箱", "纸品")[0])

    def test_negated_quantity_does_not_become_a_positive_tag(self):
        for description in ("没有多包，只有单包静态展示", "并非整箱，只有1包",
                            "不是成排陈列", "未见堆叠产品", "空箱，产品只有一包"):
            with self.subTest(description=description):
                self.assertFalse(da.cta_quantity_eligible(
                    {"description": description, "visual_tags": ["整箱", "多包"]},
                    "赶紧囤一箱", "纸品")[0])

    def test_paper_unspecified_stocking_line_mixes_stack_and_pack_actions(self):
        text = "多囤几包真不亏"
        self.assertTrue(da.cta_quantity_eligible(
            {"description": "多包产品堆叠成一堆", "action": "堆叠产品"}, text, "纸品")[0])
        self.assertEqual(da.cta_quantity_eligible(
            {"description": "手持一包产品", "action": "举起单包展示"}, text, "纸品"),
            (True, "PAPER_UNSPECIFIED_PACK_MIX"))
        self.assertTrue(da.cta_quantity_eligible(
            {"description": "两包产品拿在手里", "action": "举起两包对比"}, text, "纸品")[0])
        self.assertFalse(da.cta_quantity_eligible(
            {"description": "单包静态特写", "action": "无动作"}, text, "纸品")[0])
        self.assertFalse(da.cta_quantity_eligible(
            {"description": "单片巾体展开", "action": "展开巾体"}, text, "纸品")[0])
        self.assertFalse(da.cta_quantity_eligible(
            {"description": "手持一包产品", "action": "举起单包展示"}, "趁现在赶紧囤一箱", "纸品")[0])
        self.assertFalse(da.cta_quantity_eligible(
            {"description": "背景摆着一包产品", "action": "揉搓巾体"}, text, "纸品")[0])
        self.assertFalse(da.cta_quantity_eligible(
            {"description": "手持一包产品", "action": "举起单包展示"}, "囤两包更划算", "纸品")[0])

    def test_pack_action_cannot_borrow_a_verb_or_object_from_another_clause(self):
        for description in ("拿起纸巾揉搓，背景摆着一包产品", "把纸巾拿起擦手，一包产品摆在背景",
                            "拿起纸巾并揉搓旁边的一包包装", "背景里手拿一包产品，前景揉搓巾体",
                            "镜头没有拿起单包，只有包装特写", "未举起单包产品",
                            "镜头晃动拍摄一包产品", "一包产品旁镜头晃动", "拿起一包旁的毛巾",
                            "一包旁的毛巾被拿起", "对比一包产品旁的毛巾"):
            with self.subTest(description=description):
                self.assertFalse(da.cta_quantity_eligible(
                    {"description": description, "has_subject": True}, "多囤几包真不亏", "纸品")[0])
        for description in ("拿起一包洗脸巾展示", "从箱中拿出一包洗脸巾，双手举起", "手持两包产品对比", "一包洗脸巾被手举起", "一包纸巾被手举起", "手持一包在镜头前晃动展示"):
            self.assertTrue(da.cta_quantity_eligible(
                {"description": description, "has_subject": True}, "多囤几包真不亏", "纸品")[0])

    def test_pack_mix_does_not_expand_to_named_units_or_other_categories(self):
        shot = {"description": "手持一包产品", "action": "举起单包展示"}
        for text in ("囤一箱", "囤整箱", "囤两包", "囤一提", "每包200抽", "4层加厚"):
            self.assertFalse(da.paper_unspecified_pack_line(text, "纸品"))
        self.assertFalse(da.cta_quantity_eligible(shot, "多囤几包真不亏", "卫品")[0])

    def test_content_spec_sentence_still_accepts_matching_readable_pack(self):
        self.assertEqual(da.cta_quantity_eligible(
            {"description": "包装正面140PCS特写", "readable_claims": ["140PCS"]},
            "一包有140抽", "纸品"), (True, "READABLE_PACK_CONTENT_COUNT"))


class ObservationReliabilityTests(unittest.TestCase):
    def test_explicit_bad_confidence_cannot_certify_packaging(self):
        for confidence in (0, 0.01, 0.59, -1, 1.1, float("nan"), float("inf"),
                           float("-inf"), "not-a-number", True):
            with self.subTest(confidence=confidence):
                self.assertFalse(da.packaging_claim_direct(
                    label(analysis_confidence=confidence), "纸巾是4层加厚的")["ok"])

    def test_independent_hires_pointed_text_can_replace_low_general_confidence(self):
        shot = label(analysis_confidence=0.1, text_pass="hires", text_agreed=["4层"],
                     pointed_agreed=["4层"])
        decision = da.packaging_claim_direct(shot, "纸巾4层加厚")
        self.assertTrue(decision["ok"], decision)
        self.assertEqual(decision["perception_basis"], "independent_text_review")
        self.assertEqual(decision["evidence_contract"]["kind"], "packaging_label")

    def test_review_flag_or_wrong_reviewed_mark_cannot_bypass_low_confidence(self):
        for fields in ({"text_pass": "hires"},
                       {"text_pass": "hires", "text_agreed": ["4层"], "pointed_agreed": []},
                       {"text_pass": "hires", "text_agreed": ["3层"], "pointed_agreed": ["3层"]}):
            with self.subTest(fields=fields):
                self.assertFalse(da.packaging_claim_direct(
                    label(analysis_confidence=0.1, **fields), "4层加厚")["ok"])

    def test_nonfinite_confidence_cannot_be_hidden_by_hires_review(self):
        for confidence in (float("nan"), float("inf")):
            self.assertFalse(da.packaging_claim_direct(label(analysis_confidence=confidence,
                text_pass="hires", text_agreed=["4层"], pointed_agreed=["4层"]), "4层")["ok"])

    def test_pointing_must_land_on_the_same_claim(self):
        spoken = "纸巾是4层加厚的"
        mismatch = label(analysis_confidence=0.95, text_pass="hires",
                         text_agreed=["4层", "医护级"], pointed_agreed=["医护级"])
        self.assertEqual(da.packaging_claim_direct(mismatch, spoken)["reason"],
                         "PACKAGING_POINTING_MISMATCH")
        loose = label(description="手指指向包装正面", action="手指指向文字",
                      readable_claims=["4层"])
        self.assertEqual(da.packaging_claim_direct(loose, spoken)["reason"],
                         "PACKAGING_POINTING_MISMATCH")
        hit = label(analysis_confidence=0.95, text_pass="hires",
                    text_agreed=["4层", "医护级"], pointed_agreed=["4层"])
        self.assertTrue(da.packaging_claim_direct(hit, spoken)["ok"])
        nearby = label(description="手指指向包装正面，旁边这块印着4层",
                       action="手指指向文字", readable_claims=["4层", "医护级"])
        self.assertTrue(da.packaging_claim_direct(nearby, spoken)["ok"])
        other_face = label(description="手指指向包装正面，背面另有4层",
                           action="手指指向文字", readable_claims=["4层"])
        self.assertEqual(da.packaging_claim_direct(other_face, spoken)["reason"],
                         "PACKAGING_POINTING_MISMATCH")
        block = label(analysis_confidence=0.95, text_pass="hires",
                      text_agreed=["4层", "医护级", "加厚"],
                      pointed_agreed=["加厚", "4层"])
        self.assertTrue(da.packaging_claim_direct(block, spoken)["ok"])
        contract = da.sentence_contract(spoken, category="纸品")
        self.assertEqual(da.shot_fit(
            {"description": "手指指向包装正面", "action": "手指指向文字",
             "pointing_to_text": True, "readable_claims": ["4层"], "has_subject": True},
            contract), 0)
        self.assertEqual(da.shot_fit(
            {"description": "手指指向包装上的4层字样", "pointing_to_text": True,
             "readable_claims": ["4层"], "has_subject": True},
            contract), 2)
        self.assertEqual(da.shot_fit(
            {"description": "包装侧面印着4层", "readable_claims": ["4层"], "has_subject": True},
            contract), 2)

    def test_explicit_no_pointing_cannot_be_overridden_by_descriptions_or_hires(self):
        for fields in ({"pointing_to_text": False}, {"explicit_pointing": False},
                       {"pointing_to_text": False, "explicit_pointing": True},
                       {"pointing_to_text": False, "text_pass": "hires",
                        "text_agreed": ["4层"], "pointed_agreed": ["4层"]}):
            with self.subTest(fields=fields):
                result = da.packaging_claim_direct(label(**fields), "4层加厚")
                self.assertFalse(result["ok"], result)

    def test_negated_pointing_is_not_a_positive_keyword_match(self):
        for description in ("手指没有指向包装4层字样", "并未指着4层", "未指向包装4层",
                            "手指不是指向4层"):
            shot = label(description=description, action="普通拿包装", pointing_action="")
            self.assertFalse(da.packaging_claim_direct(shot, "4层加厚")["ok"])
            shot.pop("pointing_to_text")
            self.assertFalse(da.packaging_claim_direct(shot, "4层加厚")["ok"])

    def test_literal_pointing_copy_from_current_prompt_is_consumed(self):
        shot = label(description="手指指向包装正面", pointing_action="4层", gesture="加厚")
        from orchestrator.vision.vision_analyzer import _demo_action_fields
        shot.update(_demo_action_fields(shot))
        self.assertTrue(da.packaging_claim_direct(shot, "4层加厚")["ok"])
        self.assertEqual(da.shot_fit(shot, da.sentence_contract("4层加厚", category="纸品")), 2)
        shot["pointing_to_text"] = False
        self.assertFalse(da.packaging_claim_direct(shot, "4层加厚")["ok"])

    def test_completed_empty_pointed_review_does_not_borrow_stale_fields(self):
        shot = label(text_pass="hires", text_agreed=["4层"], pointed_agreed=[],
                     pointed_claims=["4层"], pointed_text=["4层"], pointing_action="4层")
        self.assertFalse(da.packaging_claim_direct(shot, "4层加厚")["ok"])

    def test_pointed_block_does_not_expand_to_other_faces_or_arbitrary_neighbors(self):
        for description in ("手指指向包装正面但4层字样印在背面", "手指指向包装正面，包装另一块印着4层",
                            "手指指向包装正面，4层印在其他位置", "手指指向包装正面，包装印着4层",
                            "手指指向包装正面而4层字样印在背面",
                            "手指指向包装正面且远处包装印着4层",
                            "手指指向包装正面（4层印在背面）"):
            self.assertFalse(da.packaging_claim_direct(label(description=description), "4层加厚")["ok"])
        self.assertTrue(da.packaging_claim_direct(
            label(description="手指指向包装背面的4层字样"), "4层加厚")["ok"])

    def test_old_missing_confidence_retains_narrow_packaging_compatibility(self):
        self.assertTrue(da.packaging_claim_direct(label(), "4层加厚")["ok"])
        self.assertTrue(da.perception_reliability({})["ok"])


class FunctionalEvidenceContractsTests(unittest.TestCase):
    def test_new_qualified_airflow_observation_satisfies_a_defined_contract(self):
        claim = da.CLAIMS_BY_ID["C-BREATHABLE"]
        self.assertEqual(da.judge_claim(physical(), claim), (True, "CLAIM_REQUIREMENT_MET"))
        self.assertEqual(claim.as_dict()["evidence_contract"]["kind"], "physical")
        self.assertEqual(claim.as_dict()["reference_evidence_status"], "metaphor")
        self.assertIsNone(claim.as_dict()["material_gap"])

    def test_light_transmission_and_pores_do_not_prove_airflow(self):
        claim = da.CLAIMS_BY_ID["C-BREATHABLE"]
        for fields in ({"action": "举起巾体对光", "object": "巾体和灯光", "result": "光线透过巾体"},
                       {"action": "展示气流装置", "result": "透气孔清晰可见"},
                       {"action": "没有空气穿透巾体", "result": "未见气流穿透巾体"},
                       {"result": "不是气流穿透巾体"},
                       {"result": "无气流穿透巾体"},
                       {"result": "字幕写着气流穿透巾体"},
                       {"object": "灯光和巾体"}, {"result": "气流吹向巾体"}):
            with self.subTest(fields=fields):
                self.assertFalse(da.judge_claim(physical(**fields), claim)[0])

    def test_physical_contract_keeps_role_strength_result_and_reliability_gates(self):
        claim = da.CLAIMS_BY_ID["C-BREATHABLE"]
        for fields in ({"role": "usage_demo"}, {"evidence_strength": "B"}, {"result": ""},
                       {"analysis_confidence": 0.01}, {"analysis_confidence": float("nan")}):
            self.assertFalse(da.judge_claim(physical(**fields), claim)[0])

    def test_undefined_contract_is_repairable_diagnostic_not_permanent_material_gap(self):
        soft = da.CLAIMS_BY_ID["C-SOFT"]
        self.assertEqual(da.judge_claim(physical(action="抚摸面层"), soft),
                         (False, "CONTRACT_UNDEFINED"))
        gap = da.material_gap(1, "柔软亲肤", "", [soft], [])
        self.assertEqual(gap["type"], "CONTRACT_UNDEFINED")
        self.assertIsNone(gap["gap_shot_count"])
        self.assertNotIn("补拍", gap["suggested_shoot"])

    def test_brand_and_easy_claim_cannot_short_circuit_missing_function(self):
        claims = [da.CLAIMS_BY_ID[c] for c in ("C-BRAND", "C-BREATHABLE", "C-SOFT")]
        result = da.judge(physical(), claims)
        self.assertFalse(result["ok"])
        self.assertEqual(result["unmet_claim_ids"], ["C-SOFT"])
        self.assertEqual(result["required_claim_ids"], ["C-BREATHABLE", "C-SOFT"])
        self.assertEqual(result["reason"], "CONTRACT_UNDEFINED")

    def test_all_real_functional_claims_can_pass_together(self):
        shot = physical(action="倾倒液体", object="液体和巾体", result="液体被吸入并锁在中央不扩散")
        claims = [da.CLAIMS_BY_ID[c] for c in ("C-ABSORB-SPEED", "C-ABSORB-CAPACITY", "C-BRAND")]
        self.assertTrue(da.judge(shot, claims)["ok"])

    def test_related_business_picture_remains_separate_from_physical_proof(self):
        shot = {"role": "usage_demo", "has_subject": True, "description": "抚摸巾体面层特写",
                "action": "抚摸", "evidence_tags": ["柔软", "面层"]}
        result = da.judge(shot, [da.CLAIMS_BY_ID["C-SOFT"]])
        self.assertFalse(result["ok"])
        contract = result["business_associations"][0]["evidence_contract"]
        self.assertEqual(contract["kind"], "business_association")
        self.assertEqual(contract["formal_status"], "UNCERTIFIED")


if __name__ == "__main__":
    unittest.main()
