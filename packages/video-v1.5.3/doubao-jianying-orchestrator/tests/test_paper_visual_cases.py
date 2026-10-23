"""Thirteen semantic regression pairs, with frozen synthetic model judgments.

These verify downstream acceptance/rejection, not GLM pixel recognition accuracy.
"""
import unittest
import test_ai_editor as fixtures
from orchestrator import ai_editor_contract as contract


CASES = [
    ("wipe_face_hands", "擦脸擦手", "脸和手各有擦拭动作", "只有擦桌面"),
    ("absorb_squeeze_clear_water", "吸水力很好", "挤压巾体清水大量涌出", "只有湿润包装"),
    ("lift_from_water", "可以当毛巾", "从水中捞出巾体并手持", "只有干巾静置"),
    ("cleaning_wipe", "擦桌面打扫", "擦前污渍到擦后消失均可见", "结果已被裁掉"),
    ("ef_plain_both_sides", "一面EF压纹一面平纹", "翻面后两面纹理清楚", "仅有一面"),
    ("fluorescence_zero_test", "没有荧光剂", "明确荧光检测过程及0.00读数", "包装0荧光文字"),
    ("lint_rub_unfold", "揉搓不掉毛", "揉搓后展开无浮毛", "揉搓后没有展开查看"),
    ("tensile_both_directions", "有韧性", "双手双向拉伸后完整", "只展开没有受力拉伸"),
    ("imported_wood_fiber_label", "进口木源纤维", "对应包装字清楚可读", "文字模糊无法确认"),
    ("medical_grade_package_claims", "包装标注医护级0致敏0荧光", "对应三项包装字可读", "只有医护级其余不可见"),
    ("count_140", "一百四十抽", "包装明确140抽", "只有多包装堆叠"),
    ("washcloth_mop_install", "送洗脸巾拖把", "按压安装过程可见", "只展示一包洗脸巾"),
    ("five_packs_countable", "五提", "五提分别可数清", "仅四提或遮挡数不清"),
]


class PaperVisualCaseTests(unittest.TestCase):
    def test_thirteen_positive_negative_pairs_keep_model_verdicts(self):
        for scene, text, positive, negative in CASES:
            with self.subTest(scene=scene):
                fixture = fixtures.AIEditorTests()
                fixture.setUp()
                try:
                    fixture.manifest.update(category="zhipin", product="合成纸品")
                    fixture.manifest["segments"][0]["text"] = text
                    fixture.sentence["text"] = text
                    fixture.sentence["scene_ids"] = [scene]
                    fixture.sentence["selling_points"] = [{"id": "p1", "text": text}]
                    fixture.review.update(observation=positive, reason=positive, scene_ids=[scene],
                        supported_point_ids=["p1"], preferred_speed=1.3 if scene == "cleaning_wipe" else 1)
                    matched = fixture.result()
                    self.assertTrue(matched["ok"], matched)
                    self.assertTrue(contract.validate_segment_receipt(matched["segments"][0], manifest=fixture.manifest)["ok"])
                    fixture.review.update(verdict="mismatch", observation=negative, reason=negative,
                        supported_point_ids=[])
                    rejected = fixture.result()
                    self.assertFalse(rejected["ok"])
                    self.assertEqual(rejected["segments"], [])
                finally:
                    fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
