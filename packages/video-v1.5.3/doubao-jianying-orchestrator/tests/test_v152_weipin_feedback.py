"""v1.5.2 卫品飞书反馈（2026-10-15）：配音无气口、字幕上移、拆包场景。"""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import draft_safety, rulepack, scene_rules  # noqa: E402
from orchestrator import timing_contract as tc  # noqa: E402

US = tc.US
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


class WeipinVoiceGapTests(unittest.TestCase):
    def test_weipin_voice_has_no_tail_pad(self):
        rules = rulepack.load("weipin")
        self.assertEqual(rules["versions"]["weipin"], "1.5.3-9")
        self.assertEqual(rules["voice"]["tempo"], 1.2)
        self.assertEqual(rules["voice"]["max_gap_frames"], 1)
        self.assertEqual(rulepack.gap_tail_pad_s(1, fps=30), 0.0)
        out = rulepack.apply_manifest_defaults({"product": "夜用卫生巾"})
        self.assertEqual(out["tail_pad_s"], 0.0)
        self.assertEqual(out["voice_tempo"], 1.2)
        self.assertEqual(out["rulepack"]["voice_max_gap_frames"], 1)

    def test_other_categories_use_global_no_gap(self):
        # 配音不留气口是全局规则：纸品与卫品一致 tail_pad_s=0 / max_gap_frames=1
        out = rulepack.apply_manifest_defaults({"product": "湿厕纸"})
        self.assertEqual(out["tail_pad_s"], 0.0)
        self.assertEqual(out["rulepack"]["voice_max_gap_frames"], 1)
        general = rulepack.apply_manifest_defaults({"product": "未知商品"})
        self.assertEqual(general["tail_pad_s"], 0.0)
        self.assertEqual(general["rulepack"]["voice_max_gap_frames"], 1)

    def test_explicit_tail_pad_still_wins(self):
        out = rulepack.apply_manifest_defaults({"product": "夜用卫生巾", "tail_pad_s": 0.1})
        self.assertEqual(out["tail_pad_s"], 0.1)


STATIC = {"shot_id": "pk1", "video": "D:/m/pack.mp4", "source_start": 0.0, "source_end": 10.0,
          "material_duration_us": 20 * US, "duration": 10.0, "status": "ready_for_matching",
          "action": "举起", "object": "独立包装", "description": "手持未拆封的独立包装展示袋面",
          "role": "product_display", "action_complete": True}
TEAR = {"shot_id": "tr1", "video": "D:/m/tear.mp4", "source_start": 0.0, "source_end": 10.0,
        "material_duration_us": 20 * US, "duration": 10.0, "status": "ready_for_matching",
        "action": "撕开", "object": "独立包装+巾体",
        "description": "双手撕开独立包装，取出巾体并展开展示面层",
        "role": "usage_demo", "action_complete": True, "frame_time": 2.0,
        "action_segments": [
            {"start": 1.0, "end": 3.0, "action": "撕开包装取出巾体", "result": "巾体被取出"},
            {"start": 3.0, "end": 5.0, "action": "展开巾体", "result": "面层朝上展示"},
            {"start": 6.0, "end": 9.0, "action": "放回桌面", "result": "静置"}]}
MANIFEST = {"tail_pad_s": 0.0, "fps": 30, "head_trim_s": 0.3, "tail_trim_s": 0.2,
            "product": "日用卫生巾", "category": "weipin", "delivery_mode": "preview"}


class TearPackKeywordTests(unittest.TestCase):
    RULES = rulepack.load("weipin")
    SCENE = next(s for s in RULES["scenes"] if s["id"] == "tear_pack")

    def test_unpack_means_take_product_out_of_pack(self):
        """拆包=从外包装取出产品；文案要能指向开封/取出，不是泛卫生/随身带。"""
        triggers = [
            "这款是独立包装", "单独包装更方便", "便携装一盒十片",
            "未拆封的状态", "即开即用", "新鲜取出",
            "撕开包装", "拿出一片", "从包装里取出来",
        ]
        for text in triggers:
            with self.subTest(text=text):
                scene = rulepack.scene_for(self.RULES, text, STATIC)
                self.assertIsNotNone(scene)
                self.assertEqual(scene["id"], "tear_pack")
        for text in ("干净卫生", "随身带着走", "一小包一片", "展开就能用", "不含荧光剂甲醛"):
            with self.subTest(text=text):
                scene = rulepack.scene_for(self.RULES, text, STATIC)
                self.assertTrue(scene is None or scene["id"] != "tear_pack")

    def test_product_name_is_not_a_hygiene_claim(self):
        self.assertIsNone(rulepack.scene_for(self.RULES, "这款卫生巾真的好用", STATIC))
        self.assertFalse(rulepack.text_hits_scene(self.SCENE, "姐妹们囤卫生巾"))

    def test_sealed_pack_is_static_not_tear(self):
        self.assertFalse(rulepack.shot_has_visual(self.SCENE, STATIC))
        self.assertTrue(rulepack.static_pack_shot(self.SCENE, STATIC))
        self.assertTrue(rulepack.shot_has_visual(self.SCENE, TEAR))
        self.assertTrue(rulepack.shot_extends(self.SCENE, TEAR))
        self.assertFalse(rulepack.static_pack_shot(self.SCENE, TEAR))

    def test_followup_after_static_pack(self):
        self.assertIs(rulepack.followup_scene(self.RULES, "每包都是密封的", STATIC), self.SCENE)
        self.assertIsNone(rulepack.followup_scene(self.RULES, "每包都是密封的", TEAR))
        # 文案没提包装、这一句也不是袋面：不触发。
        self.assertIsNone(rulepack.followup_scene(self.RULES, "真的很好用", STATIC))
        # 文案没提包装，但这一句又选中袋面 → 仍触发，避免产品展示连着拍外包装。
        self.assertIs(rulepack.followup_scene(self.RULES, "真的很好用", STATIC, STATIC), self.SCENE)
        # 荧光句由 apply 层用 text_forces_visual 挡住；followup 本身仍可能命中画面条件。
        self.assertIs(rulepack.followup_scene(self.RULES, "不含荧光剂甲醛", STATIC, STATIC),
                      self.SCENE)


class TearPackApplyTests(unittest.TestCase):
    @staticmethod
    def _seg(text, shot, audio_us):
        return {"text": text, "video": shot["video"], "source_start": shot["source_start"],
                "source_end": shot["source_end"], "temporary_shot_id": shot["shot_id"],
                "audio_duration_us": audio_us, "video_speed": 1.0,
                "evidence_intervals": [{"start": 4.0, "end": 5.0}]}

    def _apply(self, segments, shots, manifest=None):
        matched = {"ok": True, "manifest": {"segments": segments},
                   "recommendations": [{"index": i + 1, "top3": []} for i in range(len(segments))]}
        locate = mock.Mock(return_value={"ok": False, "reason": "PHASE_VISION_FAILED"})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("orchestrator.usage_ledger.other_task_counts", return_value={}):
            report = scene_rules.apply(manifest or MANIFEST, matched, {"shots": shots}, Path(tmp),
                                       locate=locate)
        return report, matched["manifest"]["segments"], locate

    def test_static_pack_swaps_to_unpack_shot_from_pool(self):
        report, segments, locate = self._apply(
            [self._seg("独立包装干净卫生", STATIC, 4 * US)], [STATIC, TEAR])
        seg = segments[0]
        self.assertEqual(seg["temporary_shot_id"], "tr1")
        self.assertEqual(seg["scene_rule"]["id"], "tear_pack")
        self.assertEqual(seg["scene_rule"]["status"], "applied")
        self.assertTrue(seg["scene_rule"]["swapped"])
        self.assertEqual(report["rows"][0]["trigger"], "text")
        # 带撕开动作的候选排在袋面静态原镜头前面，第一次就换上。
        self.assertEqual(report["rows"][0]["attempts"][0]["shot_id"], "tr1")
        locate.assert_not_called()
        # 拆包接面层展示的连续镜头：窗口从撕开一直盖到展开面层。
        window = seg["window_solution"]
        self.assertLessEqual(window["source_start"], 1.0)
        self.assertGreaterEqual(window["source_end"], 5.0)

    def test_short_sentence_still_covers_unpack_phase(self):
        _report, segments, _locate = self._apply(
            [self._seg("撕开独立包装拿出来", TEAR, int(1.5 * US))], [TEAR])
        seg = segments[0]
        self.assertEqual(seg["scene_rule"]["status"], "applied")
        window = seg["window_solution"]
        self.assertLessEqual(window["source_start"], 1.0)
        self.assertGreaterEqual(window["source_end"], 3.0)

    def test_no_unpack_footage_keeps_shot_and_records_gap(self):
        report, segments, _locate = self._apply(
            [self._seg("便携装独立小包", STATIC, 3 * US)], [STATIC])
        seg = segments[0]
        self.assertFalse(seg.get("visual_missing"))
        self.assertEqual(seg["temporary_shot_id"], "pk1")
        self.assertEqual(seg["scene_rule"]["status"], "unmet")
        self.assertEqual(seg["scene_rule"]["reason"], scene_rules.SCENE_NO_FOOTAGE)
        self.assertIn("补拍拆包", seg["scene_rule"]["operator_note"])
        self.assertEqual(report["rows"][0]["status"], "unmet")

    def test_packaging_followup_after_static_pack_forces_unpack(self):
        static2 = {**STATIC, "shot_id": "pk2", "video": "D:/m/pack2.mp4"}
        report, segments, _locate = self._apply(
            [self._seg("这款卫生巾真的好用", STATIC, 2 * US),
             self._seg("每包都是密封的", static2, 4 * US)],
            [STATIC, static2, TEAR])
        first, second = segments
        self.assertNotIn("scene_rule", first)
        self.assertEqual(second["temporary_shot_id"], "tr1")
        self.assertEqual(second["scene_rule"]["status"], "applied")
        row = next(r for r in report["rows"] if r["index"] == 2)
        self.assertEqual(row["trigger"], "prior_static_pack")

    def test_back_to_back_pack_display_prefers_unpack(self):
        """产品展示连着两句袋面：第二句改拆包，避免一直拍外包装。"""
        static2 = {**STATIC, "shot_id": "pk2", "video": "D:/m/pack2.mp4"}
        report, segments, _locate = self._apply(
            [self._seg("自从用了一次这个小暖心本草卫生巾", STATIC, 2 * US),
             self._seg("实在是对比之后太香了", static2, 4 * US)],
            [STATIC, static2, TEAR])
        self.assertEqual(segments[0]["temporary_shot_id"], "pk1")
        self.assertEqual(segments[1]["temporary_shot_id"], "tr1")
        row = next(r for r in report["rows"] if r["index"] == 2)
        self.assertEqual(row["trigger"], "prior_static_pack")
        self.assertEqual(row["scene"], "tear_pack")

    def test_cta_packaging_sentence_keeps_outer_pack(self):
        static2 = {**STATIC, "shot_id": "pk2", "video": "D:/m/pack2.mp4"}
        report, segments, _locate = self._apply(
            [self._seg("这款卫生巾真的好用", STATIC, 2 * US),
             self._seg("包装好看赶紧下单囤起来", static2, 4 * US)],
            [STATIC, static2, TEAR])
        self.assertEqual(segments[1]["temporary_shot_id"], "pk2")
        self.assertNotIn("scene_rule", segments[1])
        self.assertEqual(report["rows"], [])

    def test_fluorescence_claim_not_stolen_by_unpack_followup(self):
        """「不含荧光剂」选中袋面时，不能被连续袋面触发拐去拆包。"""
        fluoro = {
            "shot_id": "fl1", "status": "ready_for_matching", "video": "D:/m/fluoro.mp4",
            "source_start": 0.0, "source_end": 8.0, "duration": 8.0,
            "description": "紫光检测笔照射巾体显示读数", "action": "荧光检测",
            "action_complete": True, "action_phase": "perform",
        }
        static2 = {**STATIC, "shot_id": "pk2", "video": "D:/m/pack2.mp4"}
        report, segments, _locate = self._apply(
            [self._seg("敏感肌姐妹也能放心用", STATIC, 2 * US),
             self._seg("不含荧光剂甲醛", static2, 4 * US)],
            [STATIC, static2, TEAR, fluoro])
        row = next(r for r in report["rows"] if r["index"] == 2)
        self.assertEqual(row["scene"], "fluorescence")
        self.assertNotEqual(row.get("trigger"), "prior_static_pack")
        self.assertEqual(segments[1]["scene_rule"]["id"], "fluorescence")


# 拆包动作约 4.5 秒：单句 1.5 秒即使用 1.6× 也放不下，必须借上下句。
# 故意不带「展开面层」续段，避免组窗口被 extend 拉到 6s+ 后仍装不下。
LONG_TEAR = {
    **TEAR, "shot_id": "tr_long", "video": "D:/m/tear_long.mp4",
    "action_segments": [
        {"start": 1.0, "end": 5.5, "action": "撕开包装取出巾体", "result": "巾体被取出"},
    ],
}


class TearPackGroupTests(unittest.TestCase):
    RULES = rulepack.load("weipin")

    @staticmethod
    def _script(lines):
        return [{"text": t, "video": "D:/m/a.mp4", "temporary_shot_id": "s1",
                 "audio_duration_us": int(s * US)} for t, s in lines]

    def test_tear_pack_rule_has_group_config(self):
        scene = next(s for s in self.RULES["scenes"] if s["id"] == "tear_pack")
        self.assertEqual(scene["group_bridge"], 1)
        self.assertEqual(scene["group_min_s"], 2.5)
        self.assertEqual(scene["group_extend_max"], 2)
        self.assertEqual(scene["group_max"], 3)

    def test_consecutive_unpack_sentences_form_one_group(self):
        segments = self._script([("独立包装更方便", 1.5), ("撕开就能取出", 1.4),
                                 ("百分百的纯棉面层", 1.3)])
        groups = scene_rules._split_groups(segments, self.RULES, {"s1": TEAR}, category="weipin")
        tear = [g for g in groups if g["scene"]["id"] == "tear_pack"]
        self.assertEqual([g["positions"] for g in tear], [[0, 1]])

    def test_short_unpack_extends_free_neighbour_not_fluorescence(self):
        segments = self._script([("独立包装干净卫生", 1.5), ("姐妹们冲", 1.2),
                                 ("真的好用", 1.0)])
        groups = scene_rules._split_groups(segments, self.RULES, {"s1": TEAR}, category="weipin")
        tear = [g for g in groups if g["scene"]["id"] == "tear_pack"]
        self.assertEqual(tear[0]["positions"], [0, 1])

        blocked = self._script([("独立包装干净卫生", 1.5), ("不含荧光剂甲醛", 1.2)])
        groups = scene_rules._split_groups(blocked, self.RULES, {"s1": TEAR}, category="weipin")
        tear = [g for g in groups if g["scene"]["id"] == "tear_pack"]
        self.assertEqual(tear[0]["positions"], [0])

        surface = self._script([("独立包装干净卫生", 1.5), ("百分百的纯棉面层", 1.2)])
        groups = scene_rules._split_groups(surface, self.RULES, {"s1": TEAR}, category="weipin")
        tear = [g for g in groups if g["scene"]["id"] == "tear_pack"]
        self.assertEqual(tear[0]["positions"], [0])

    def test_long_enough_unpack_sentence_does_not_force_merge(self):
        segments = self._script([("撕开独立包装拿出来", 3.0), ("姐妹们冲", 1.2)])
        groups = scene_rules._split_groups(segments, self.RULES, {"s1": TEAR}, category="weipin")
        tear = [g for g in groups if g["scene"]["id"] == "tear_pack"]
        self.assertEqual(tear[0]["positions"], [0])

    def test_short_unpack_plays_continuously_across_neighbours(self):
        other = {**STATIC, "shot_id": "pk2", "video": "D:/m/pack2.mp4"}
        matched = {
            "ok": True,
            "manifest": {"segments": [
                {"text": "独立包装更卫生", "video": STATIC["video"],
                 "source_start": 0.0, "source_end": 10.0, "temporary_shot_id": "pk1",
                 "audio_duration_us": int(1.5 * US), "video_speed": 1.0,
                 "evidence_intervals": [{"start": 4.0, "end": 5.0}]},
                {"text": "姐妹们冲", "video": other["video"],
                 "source_start": 0.0, "source_end": 10.0, "temporary_shot_id": "pk2",
                 "audio_duration_us": int(1.5 * US), "video_speed": 1.0,
                 "evidence_intervals": [{"start": 4.0, "end": 5.0}]},
            ]},
            "recommendations": [{"index": 1, "top3": []}, {"index": 2, "top3": []}],
        }
        locate = mock.Mock(return_value={"ok": False, "reason": "PHASE_VISION_FAILED"})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("orchestrator.usage_ledger.other_task_counts", return_value={}):
            report = scene_rules.apply(
                MANIFEST, matched, {"shots": [STATIC, other, LONG_TEAR]}, Path(tmp),
                locate=locate)
        row = next(r for r in report["rows"] if r.get("scene") == "tear_pack")
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["indices"], [1, 2])
        segs = matched["manifest"]["segments"]
        self.assertEqual({s["temporary_shot_id"] for s in segs}, {"tr_long"})
        self.assertEqual(segs[0]["scene_rule"]["group"], [1, 2])
        self.assertEqual(segs[1]["scene_rule"]["group"], [1, 2])
        # 同一条素材连续播：第二句源区间紧接第一句之后。
        w0, w1 = segs[0]["window_solution"], segs[1]["window_solution"]
        self.assertLessEqual(w0["source_start"], 1.1)
        self.assertAlmostEqual(w0["source_end"], w1["source_start"], delta=0.05)
        self.assertGreaterEqual(w1["source_end"], 5.0)


class ShotLabelRecognitionTests(unittest.TestCase):
    """无文件夹名时，靠画面描述补齐人工那几类卖点标签。"""

    RULES = rulepack.load("weipin")

    def test_analysis_rules_cover_human_sellpoints(self):
        texts = " ".join(rulepack.prompt_rules({"category": "weipin"}, "analysis"))
        for needle in ("抚摸面层", "弹力腰围", "立体护边", "超薄", "多片展开", "荧光测试"):
            self.assertIn(needle, texts, needle)

    def test_annotate_from_description_not_path(self):
        waist = {
            "video": r"D:\pool\random\IMG_1.MOV",
            "description": "双手撑开裤腰橡筋并拉伸，可见腰围弹性形变",
            "action": "拉伸", "evidence_tags": ["完整展示产品"], "visual_tags": ["白色拉拉裤"],
        }
        added = rulepack.annotate_shot_labels(waist, self.RULES)
        self.assertIn("腰腹卖点", added)
        self.assertIn("腰腹卖点", waist["evidence_tags"])
        # 路径里即使有「面层卖点」也不能当标签来源
        fake = {
            "video": r"D:\pool\效果展示\面层卖点\IMG_2.MOV",
            "description": "手持未拆封紫色包装袋正面展示",
            "action": "举起", "evidence_tags": ["完整展示包装"], "visual_tags": ["包装袋"],
        }
        self.assertEqual(rulepack.annotate_shot_labels(fake, self.RULES), [])
        self.assertNotIn("面层卖点", fake.get("evidence_tags") or [])

    def test_surface_fluorescence_and_count_labels(self):
        surface = {
            "description": "手掌在巾体表面反复抚摸展示质感",
            "evidence_tags": ["手掌抚摸巾体表面"], "visual_tags": [],
        }
        self.assertIn("面层卖点", rulepack.annotate_shot_labels(surface, self.RULES))
        # 普通计时器红字 ≠ 荧光检测
        timer = {
            "description": "计时器亮出红色数字，放在白色巾体上方",
            "evidence_tags": ["电子设备"], "visual_tags": [],
        }
        self.assertNotIn("荧光测试", rulepack.annotate_shot_labels(timer, self.RULES))
        fluoro = {
            "description": "检测笔贴着巾体照射，屏幕出现荧光测试读数",
            "evidence_tags": ["检测笔"], "visual_tags": [],
        }
        self.assertIn("荧光测试", rulepack.annotate_shot_labels(fluoro, self.RULES))
        sheets = {
            "description": "双手将多片巾体扇形展开，可见片数",
            "evidence_tags": ["扇形展开"], "visual_tags": [],
        }
        # 先靠分析规则写出「多片展开」；这里模拟已写出后的标签补全
        sheets["description"] = "双手将巾体多片展开呈扇形"
        sheets["evidence_tags"] = ["多片展开"]
        self.assertIn("片数展示", rulepack.annotate_shot_labels(sheets, self.RULES))

    def test_negation_and_pack_print_do_not_emit_action_labels(self):
        denied = {
            "description": "未看到腰头橡筋，只展示包装正面",
            "evidence_tags": ["完整展示包装"], "visual_tags": [],
        }
        self.assertEqual(rulepack.annotate_shot_labels(denied, self.RULES), [])
        printed = {
            "description": "包装正面印着弹力腰围字样，未展示产品本体",
            "evidence_tags": ["完整展示包装"], "visual_tags": [],
        }
        self.assertEqual(rulepack.annotate_shot_labels(printed, self.RULES), [])
        no_side = {
            "description": "没有立体护边，仅拉伸腰围展示弹性",
            "evidence_tags": [], "visual_tags": [],
        }
        added = rulepack.annotate_shot_labels(no_side, self.RULES)
        self.assertIn("腰腹卖点", added)
        self.assertNotIn("防侧漏", added)
        self.assertNotIn("立体护边", added)

    def test_derived_labels_can_be_revoked_on_reannotate(self):
        shot = {
            "description": "双手撑开裤腰橡筋并拉伸",
            "evidence_tags": ["完整展示产品"], "visual_tags": [],
        }
        self.assertIn("腰腹卖点", rulepack.annotate_shot_labels(shot, self.RULES))
        self.assertTrue(str(shot["shot_label_sources"].get("腰腹卖点", "")).startswith("derived:"))
        shot["description"] = "手持未拆封紫色包装袋正面展示"
        shot["evidence_tags"] = ["完整展示包装"]
        shot["visual_tags"] = []
        # 只撤回派生标签；模型原事实保留。规则不再命中时清空派生。
        rulepack.annotate_shot_labels(shot, self.RULES)
        self.assertNotIn("腰腹卖点", shot.get("evidence_tags") or [])
        self.assertNotIn("腰腹卖点", (shot.get("shot_label_sources") or {}))

    def test_scene_rules_force_waist_and_sheet_count(self):
        pack = {"description": "手持包装", "action": "举起"}
        waist_shot = {"description": "拉伸弹力腰围", "evidence_tags": ["腰腹卖点", "弹力腰围"]}
        self.assertEqual(rulepack.scene_for(self.RULES, "加高加宽的弹力腰围", pack)["id"],
                         "waist_band")
        self.assertTrue(rulepack.shot_has_visual(
            next(s for s in self.RULES["scenes"] if s["id"] == "waist_band"), waist_shot))
        self.assertEqual(rulepack.scene_for(self.RULES, "每包里面有4片", pack)["id"],
                         "sheet_count")


class SubtitleDefaultTests(unittest.TestCase):
    def test_text_ops_default_is_minus_055(self):
        source = (PACKAGE_ROOT / "jianying-editor/scripts/core/text_ops.py").read_text(encoding="utf-8")
        self.assertRegex(source, r"(?m)^DEFAULT_SUBTITLE_Y = -0\.55$")
        self.assertRegex(source, r"(?m)^DEFAULT_SUBTITLE_X = 0\.0$")
        self.assertNotRegex(source, r"(?m)^DEFAULT_SUBTITLE_Y = -1000")

    def test_engine_and_validator_agree(self):
        engine = (PACKAGE_ROOT / "doubao-jianying-orchestrator/orchestrator/engine.py").read_text(
            encoding="utf-8")
        self.assertIn('"subtitle_transform_y": -0.55,', engine)
        self.assertNotRegex(engine, r'"subtitle_transform_y":\s*-(0\.8|1000)')
        material = {"content": '{"text": "字幕", "styles": [{"size": 8.0, "bold": true}]}'}
        expected = {"subtitle_style_required": True}

        def issues(y):
            segment = {"clip": {"transform": {"x": 0.0, "y": y}}}
            return draft_safety._subtitle_style_issues(material, segment, 1, expected)

        self.assertEqual(issues(-0.55), [])
        self.assertTrue(issues(-0.8))
        self.assertTrue(issues(-1000.0))

    def test_release_docs_use_minus_055(self):
        rules = (PACKAGE_ROOT / "WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json").read_text(encoding="utf-8")
        self.assertIn('"position": {"x": 0, "y": -0.55}', rules)
        for name in ("RULES.md", "jianying-editor/SKILL.md"):
            text = (PACKAGE_ROOT / name).read_text(encoding="utf-8")
            self.assertTrue(re.search(r"Y=-0\.55", text), name)


if __name__ == "__main__":
    unittest.main()
