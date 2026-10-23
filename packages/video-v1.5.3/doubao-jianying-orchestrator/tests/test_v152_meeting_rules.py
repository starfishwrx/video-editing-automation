"""v1.5.2：2026-10-12 会议纪要 + 卫品AI智能混剪通行指令。"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import (  # noqa: E402
    phase_timing,
    rulepack,
    scene_rules,
    shot_notes,
    timing_contract as tc,
    usage_ledger,
    visual_match,
    voice_tts,
)
from orchestrator.engine import OrchestrationEngine, _parent_id, to_us  # noqa: E402

US = tc.US


class TransitionDefaultTests(unittest.TestCase):
    def _engine(self, manifest):
        eng = OrchestrationEngine.__new__(OrchestrationEngine)
        eng.m = manifest
        eng._transition_for = mock.Mock()
        eng._same_span = mock.Mock(return_value=False)
        return eng

    def test_no_transition_unless_enabled(self):
        clips = [object(), object(), object()]
        eng = self._engine({})
        eng.plan_transitions(clips)
        eng._transition_for.assert_not_called()

    def test_explicit_enable_still_works(self):
        clips = [object(), object()]
        eng = self._engine({"auto_transition": True})
        eng.plan_transitions(clips)
        self.assertEqual(eng._transition_for.call_count, 1)


class RulepackTests(unittest.TestCase):
    def test_detect_category(self):
        self.assertEqual(rulepack.detect_category({"product": "萌睡裤安睡裤"})[0], "weipin")
        self.assertEqual(rulepack.detect_category({"draft_name": "纯水湿厕纸-rev-1"})[0], "zhipin")
        self.assertEqual(rulepack.detect_category({"product": "湿厕纸", "category": "卫品"})[0],
                         "weipin")
        self.assertEqual(rulepack.detect_category({"product": "保温杯"})[0], "general")
        self.assertEqual(rulepack.detect_category(
            {"material_sources": [r"D:\素材\日用卫生巾\a.mp4"]})[0], "weipin")

    def test_detect_category_from_script(self):
        self.assertEqual(rulepack.detect_category(
            {"product": "优雅", "segments": [{"text": "现在谁来大姨妈还在囤卫生巾啊"}]}),
            ("weipin", "full_script:卫生巾"))
        self.assertEqual(rulepack.detect_category(
            {"product": "优雅", "full_script": "卫生巾和纸巾一起囤"})[0], "general")
        self.assertEqual(rulepack.detect_category({"product": "优雅"})[0], "general")

    def test_manifest_defaults(self):
        weipin = rulepack.apply_manifest_defaults({"product": "夜用卫生巾"})
        self.assertEqual(weipin["voice_tempo"], 1.2)
        self.assertFalse(weipin["auto_transition"])
        self.assertEqual(weipin["tail_pad_s"], 0.0)
        self.assertEqual(weipin["rulepack"]["rulepacks"], ["global", "weipin"])
        preset_speed = rulepack.apply_manifest_defaults({"product": "夜用卫生巾", "voice": "紫薇1.2倍速"})
        # 名称倍速走 ffmpeg atempo；SAMI 不认后缀，不能再误置 1.0
        self.assertEqual(preset_speed["voice_tempo"], 1.2)
        self.assertEqual(rulepack.voice_name_speed("憨熊1.1倍速"), 1.1)
        explicit = rulepack.apply_manifest_defaults(
            {"product": "夜用卫生巾", "voice": "紫薇1.2倍速", "voice_tempo": 1.1})
        self.assertEqual(explicit["voice_tempo"], 1.1)
        zhipin = rulepack.apply_manifest_defaults({"product": "湿厕纸", "tail_pad_s": 0.1,
                                                  "auto_transition": True})
        self.assertEqual(zhipin["voice_tempo"], 1.3)
        self.assertEqual(zhipin["tail_pad_s"], 0.1)
        self.assertTrue(zhipin["auto_transition"])

    def test_gap_budget_global_no_gap(self):
        # max_gap_frames=1 → 句尾留白为 0，相邻配音时间线首尾相接
        # （各句内部仍可保留 edge_keep 自然静音，那不是句间气口）
        self.assertEqual(rulepack.gap_tail_pad_s(1, fps=30), 0.0)
        zhipin = rulepack.apply_manifest_defaults({"product": "湿厕纸"})
        self.assertEqual(zhipin["tail_pad_s"], 0.0)
        self.assertEqual(zhipin["rulepack"]["voice_max_gap_frames"], 1)
        weipin = rulepack.apply_manifest_defaults({"product": "夜用卫生巾"})
        self.assertEqual(weipin["tail_pad_s"], 0.0)
        self.assertEqual(weipin["rulepack"]["voice_max_gap_frames"], 1)

    def test_scene_needs_text_and_shot(self):
        rules = rulepack.load("weipin")
        pour = {"action": "倾倒", "object": "蓝色液体+巾体", "description": "往卫生巾上倒水"}
        self.assertEqual(rulepack.scene_for(rules, "吸水实验看看瞬吸效果", pour)["id"], "absorb_test")
        self.assertIsNone(rulepack.scene_for(rules, "吸水实验看看瞬吸效果", {"action": "举起"}))
        # 对光轻薄/透气：描述里写「吸水层」也不能当成倒液实验。
        backlight = {"description": "将巾体举到灯光前，光线透过巾体，可见内部压纹与吸水层结构",
                     "evidence_tags": ["对光展示光线透过巾体", "吸水层"]}
        absorb = next(s for s in rules["scenes"] if s["id"] == "absorb_test")
        self.assertFalse(rulepack.shot_has_visual(absorb, backlight))
        self.assertIsNone(rulepack.scene_for(rules, "16倍的瞬吸技术", backlight))
        self.assertEqual(rulepack.scene_for(rules, "面层很柔软", {"action": "举起"})["id"],
                         "surface_layer")
        self.assertEqual(rulepack.scene_for(rules, "这款很好用",
                                            {"pointing_to_text": True})["id"], "point_front")
        self.assertEqual([s["id"] for s in rulepack.load("zhipin")["scenes"]],
                         ["stack_product", "show_product", "usage_action", "fluorescence"])


class WasteTrimTests(unittest.TestCase):
    def _req(self, **kw):
        base = dict(source_in_us=0, source_out_us=10 * US, audio_duration_us=3 * US,
                    tail_pad_us=0, head_guard_us=300_000, tail_guard_us=200_000,
                    speed_min=1.0, speed_max=1.0)
        base.update(kw)
        return tc.WindowRequest(**base)

    def test_guards_follow_waste_with_cap(self):
        self.assertEqual(tc.waste_guards_us(0, 10 * US, 0.3, 0.2, 300_000, 200_000),
                         (3 * US, 2 * US))
        self.assertEqual(tc.waste_guards_us(0, 10 * US, 0.0, 0.0, 300_000, 200_000),
                         (300_000, 200_000))
        head, tail = tc.waste_guards_us(0, 10 * US, 0.9, 0.9, 300_000, 200_000)
        self.assertLessEqual(head + tail, 8 * US + 1)
        self.assertGreaterEqual(head, 300_000)

    def test_waste_applied_moves_window(self):
        sol = tc.solve_window_waste_aware(self._req(), 0.4, 0.0)
        self.assertTrue(sol.ok)
        self.assertGreaterEqual(sol.source_start_us, 4 * US)
        self.assertEqual(sol.detail["waste_trim"]["status"], tc.WASTE_TRIM_APPLIED)

    def test_waste_relaxed_when_infeasible(self):
        sol = tc.solve_window_waste_aware(self._req(audio_duration_us=8 * US), 0.4, 0.2)
        self.assertTrue(sol.ok)
        self.assertEqual(sol.detail["waste_trim"]["status"], tc.WASTE_TRIM_RELAXED)

    def test_speed_override(self):
        self.assertEqual(tc.effective_speed_range("direct_evidence", time_sensitive=True),
                         (1.0, 1.0))
        self.assertEqual(tc.effective_speed_range("direct_evidence", time_sensitive=True,
                                                  override=[1.6, 1.0]), (1.0, 1.6))
        self.assertEqual(tc.effective_speed_range("usage_demo", override=None), (0.92, 1.15))


class ShotNotesTests(unittest.TestCase):
    SENTENCES = ["姐妹们看过来", "这款安睡裤吸水超快", "背面信息写得清清楚楚"]

    def test_parse(self):
        notes = "吸水超快：要有倒水的完整过程\n第3句：用背标特写\n整体节奏要快\n不存在的句子：x"
        parsed = shot_notes.parse(notes, self.SENTENCES)
        self.assertEqual(parsed["by_index"][2], "要有倒水的完整过程")
        self.assertEqual(parsed["by_index"][3], "用背标特写")
        self.assertEqual(parsed["general"], ["整体节奏要快"])
        self.assertEqual(parsed["unmatched"], ["不存在的句子：x"])

    def test_apply_to_segments(self):
        manifest = {"segments": [{"text": t} for t in self.SENTENCES],
                    "shot_notes": [{"text": "姐妹们", "note": "开场要有人"}]}
        out = shot_notes.apply(manifest)
        self.assertEqual(out["segments"][0]["shot_note"], "开场要有人")
        self.assertNotIn("shot_note", out["segments"][1])
        self.assertNotIn("visual_requirements", out["segments"][0])

    def test_note_spanning_split_sentences_binds_to_each(self):
        sentences = ["卫生巾包装撕开的过程当中", "要包含撕包装抽取产品展示产品", "下一句"]
        parsed = shot_notes.parse(
            "卫生巾包装撕开的过程当中要包含撕包装抽取产品展示产品：必须完整展示动作", sentences)
        self.assertEqual(parsed["by_index"], {1: "必须完整展示动作", 2: "必须完整展示动作"})
        self.assertEqual(parsed["unmatched"], [])

    def test_remark_field_env(self):
        with mock.patch.dict("os.environ", {"JY_REMARK_FIELD": "画面要求"}):
            out = shot_notes.apply({"segments": [{"text": "你好世界"}], "画面要求": "你好：手拿产品"})
        self.assertEqual(out["segments"][0]["shot_note"], "手拿产品")


class UsageLedgerTests(unittest.TestCase):
    def test_record_and_penalties(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            seg = [{"video": r"D:\m\a.mp4", "source_start": 1.0},
                   {"video": r"D:\m\b.mp4", "source_start": 2.0, "multi_shot": True,
                    "span_parts": [{"video": r"D:\m\b.mp4", "scene_source_start": 2.0},
                                   {"video": r"D:\m\c.mp4", "scene_source_start": 0.0}]},
                   {"visual_missing": True}]
            v1 = {"usage_ledger_path": path, "record_id": "rec1", "draft_name": "任务-rev-1"}
            first = usage_ledger.record(v1, seg)
            self.assertEqual(first["recorded"], 3)
            v2 = {**v1, "draft_name": "任务-rev-2"}
            usage = usage_ledger.load_usage(v2)
            self.assertEqual(usage[usage_ledger.shot_key(r"D:\m\a.mp4", 1.0)], usage_ledger.SAME_TASK)
            other = {"usage_ledger_path": path, "record_id": "rec9", "draft_name": "别的任务"}
            self.assertEqual(set(usage_ledger.load_usage(other).values()), {usage_ledger.OTHER_TASK})
            self.assertEqual(usage_ledger.load_usage(v1), {})
            second = usage_ledger.record(v2, seg[:1])
            self.assertEqual(second["overlap"][0]["draft"], "任务-rev-1")
            self.assertEqual(second["overlap"][0]["ratio"], 1.0)
            usage_ledger.record(v1, seg[:1])
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(len(data["shots"][usage_ledger.shot_key(r"D:\m\a.mp4", 1.0)]), 2)
            self.assertNotIn(usage_ledger.shot_key(r"D:\m\c.mp4", 0.0), data["shots"])

    def test_ledger_path(self):
        manifest = {"material_sources": [r"D:\m\B.mp4", r"D:\m\a.mp4"]}
        path = usage_ledger.ledger_path(manifest, Path(r"D:\work\task\report"))
        self.assertEqual(path.parent, Path(r"D:\work\.cache"))
        self.assertTrue(path.name.startswith("shot_usage_"))
        self.assertIsNone(usage_ledger.ledger_path({}, Path(r"D:\work\task\report")))
        retry = usage_ledger.ledger_path(manifest, Path(r"D:\work\task\retry1\report"))
        self.assertEqual(retry, path)

    def test_same_file_names_in_different_pools_do_not_share(self):
        a = {"material_sources": [r"D:\sku_a\IMG_001.MOV"]}
        b = {"material_sources": [r"D:\sku_b\IMG_001.MOV"]}
        self.assertNotEqual(usage_ledger.pool_hash(a), usage_ledger.pool_hash(b))
        self.assertNotEqual(usage_ledger.shot_key(r"D:\sku_a\IMG_001.MOV", 0),
                            usage_ledger.shot_key(r"D:\sku_b\IMG_001.MOV", 0))
        self.assertEqual(usage_ledger.shot_key(r"\\nas\pool\IMG_001.MOV", 0),
                         usage_ledger.shot_key("//NAS/pool/img_001.mov", 0))


class PhaseTimingTests(unittest.TestCase):
    SCENE = rulepack.load("weipin")["scenes"][0]

    def test_sample_times(self):
        times = phase_timing.sample_times(10.0, 20.0)
        self.assertEqual(len(times), phase_timing.MAX_FRAMES)
        self.assertGreater(times[0], 10.0)
        self.assertLess(times[-1], 20.0)
        self.assertEqual(len(phase_timing.sample_times(0.0, 1.0)), 3)

    def test_validate(self):
        times = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
        payload = {"complete": True, "phases": [
            {"name": "pour", "start": 0.0, "end": 1.0},
            {"name": "absorb", "start": 2.0, "end": 3.0},
            {"name": "wipe", "start": 4.0, "end": 5.0}]}
        result = phase_timing.validate(payload, self.SCENE, times)
        self.assertTrue(result["ok"] and result["complete"])
        self.assertEqual(result["phases"]["pour"], [-0.5, 1.5])
        self.assertEqual(result["phases"]["wipe"], [3.5, 5.5])
        bounded = phase_timing.validate(payload, self.SCENE, times, bounds=(0.0, 5.2))
        self.assertEqual(bounded["phases"]["pour"], [0.0, 1.5])
        self.assertEqual(bounded["phases"]["wipe"], [3.5, 5.2])
        partial = phase_timing.validate({"complete": True, "phases": payload["phases"][:2]},
                                        self.SCENE, times)
        self.assertFalse(partial["complete"])
        bad = phase_timing.validate({"phases": [{"name": "pour", "start": 0, "end": 9}]},
                                    self.SCENE, times)
        self.assertEqual(bad["reason"], "PHASE_TIME_OUT_OF_RANGE")
        order = phase_timing.validate({"phases": [{"name": "pour", "start": 3, "end": 4},
                                                  {"name": "absorb", "start": 1, "end": 2}]},
                                      self.SCENE, times)
        self.assertEqual(order["reason"], "PHASE_ORDER_INVALID")

    def test_disabled(self):
        with mock.patch.dict("os.environ", {"JY_PHASE_TIMING": "0"}):
            self.assertEqual(phase_timing.locate({}, self.SCENE, Path("."))["reason"],
                             "PHASE_TIMING_DISABLED")


SHOT = {"shot_id": "s1", "video": "D:/m/absorb.mp4", "source_start": 10.0, "source_end": 30.0,
        "material_duration_us": 40 * US, "status": "ready_for_matching",
        "action": "倾倒", "object": "蓝色液体+巾体", "description": "往安睡裤上倒蓝色液体，再用纸巾按压",
        "role": "direct_evidence", "evidence_strength": "A", "action_complete": True}
PHASES = {"pour": [11.0, 14.0], "absorb": [14.0, 20.0], "wipe": [20.0, 23.0]}
MANIFEST = {"tail_pad_s": 0.08, "fps": 30, "head_trim_s": 0.3, "tail_trim_s": 0.2}


class SplitSpeedTests(unittest.TestCase):
    SCENE = rulepack.load("weipin")["scenes"][0]

    def _check(self, plan, audio_us):
        self.assertTrue(plan["ok"], plan)
        parts = plan["parts"]
        timeline = audio_us + 80_000
        self.assertEqual(sum(p["segment_us"] for p in parts), timeline)
        for a, b in zip(parts, parts[1:]):
            self.assertEqual(a["source_end_us"], b["source_start_us"])
        for p in parts:
            lo, hi = p["speed_range"]
            self.assertTrue(lo - 1e-6 <= p["speed"] <= hi + 1e-6, p)
            material = p["source_end_us"] - p["source_start_us"]
            self.assertLessEqual(abs(material / p["speed"] - p["segment_us"]), 33_334)
        absorb = next(p for p in parts if p["phase"] == "absorb")
        self.assertLessEqual(absorb["segment_us"], 3 * US + 33_334)
        return parts

    def test_fits_with_absorb_fast_forward(self):
        parts = self._check(scene_rules.plan_split(SHOT, PHASES, self.SCENE, audio_us=8_920_000,
                                                   manifest=MANIFEST), 8_920_000)
        self.assertAlmostEqual(parts[0]["speed"], 1.0, places=3)
        self.assertAlmostEqual(parts[2]["speed"], 1.0, places=3)
        self.assertEqual(parts[0]["source_start_us"], 11 * US)

    def test_short_sentence_speeds_up(self):
        parts = self._check(scene_rules.plan_split(SHOT, PHASES, self.SCENE, audio_us=5_000_000,
                                                   manifest=MANIFEST), 5_000_000)
        self.assertGreater(parts[0]["speed"], 1.0)
        self.assertLessEqual(parts[0]["speed"], 1.6 + 1e-6)

    def test_short_sentence_uses_pour_burst_instead_of_failing(self):
        # 约 4.7s 口播 + 只有倒水/吸收（无擦拭）：常规倒水 1.6× 不够，提到 burst 4× 仍用真实验。
        shot = {**SHOT, "source_start": 0.0, "source_end": 70.0, "material_duration_us": 70 * US}
        phases = {"pour": [1.0, 8.0], "absorb": [8.0, 65.0]}
        plan = scene_rules.plan_split(shot, phases, self.SCENE, audio_us=4_670_000, manifest=MANIFEST)
        self.assertTrue(plan["ok"], plan)
        pour = next(p for p in plan["parts"] if p["phase"] == "pour")
        self.assertGreater(pour["speed"], 1.6)
        self.assertLessEqual(pour["speed"], 4.0 + 1e-6)

    def test_too_short_sentence_fails(self):
        plan = scene_rules.plan_split(SHOT, PHASES, self.SCENE, audio_us=int(0.5 * US), manifest=MANIFEST)
        self.assertEqual(plan["reason"], scene_rules.SCENE_ACTION_TOO_LONG)

    def test_long_sentence_extends_material(self):
        parts = self._check(scene_rules.plan_split(SHOT, PHASES, self.SCENE, audio_us=12 * US,
                                                   manifest=MANIFEST), 12 * US)
        self.assertGreater(parts[-1]["source_end_us"], 23 * US)

    def test_sentence_longer_than_scene(self):
        plan = scene_rules.plan_split(SHOT, PHASES, self.SCENE, audio_us=40 * US, manifest=MANIFEST)
        self.assertEqual(plan["reason"], scene_rules.SCENE_SENTENCE_TOO_LONG)


class SceneApplyTests(unittest.TestCase):
    def _matched(self, text, audio_us, shot=SHOT, top3=None):
        seg = {"text": text, "video": shot["video"], "source_start": shot["source_start"],
               "source_end": shot["source_end"], "temporary_shot_id": shot["shot_id"],
               "audio_duration_us": audio_us, "video_speed": 1.0, "head_waste": 0.2,
               "evidence_intervals": [{"start": 19.0, "end": 21.0}]}
        return {"ok": True, "manifest": {"segments": [seg]},
                "recommendations": [{"index": 1, "top3": top3 or []}]}

    def test_absorb_split_applied(self):
        manifest = {**MANIFEST, "product": "萌睡裤", "delivery_mode": "preview"}
        matched = self._matched("吸水实验，倒下去瞬吸", 8 * US)
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": PHASES})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(manifest, matched, {"shots": [SHOT]}, Path(tmp), locate=locate)
            self.assertTrue((Path(tmp) / "scene_rules_report.json").is_file())
        seg = matched["manifest"]["segments"][0]
        self.assertEqual(report["applied"], 1)
        self.assertTrue(seg["multi_shot"])
        self.assertEqual([p["detail"]["scene_phase"] for p in seg["span_parts"]],
                         ["pour", "absorb", "wipe"])
        self.assertEqual(seg["scene_rule"]["status"], "applied")
        self.assertEqual(seg["head_waste"], 0.0)

    def test_slice_plan_follows_sentence_bounds(self):
        plan = scene_rules.plan_split(SHOT, PHASES, SplitSpeedTests.SCENE, audio_us=8_920_000,
                                      manifest=MANIFEST)
        total = plan["timeline_us"]
        members = scene_rules.slice_plan(plan, [3 * US, total - 3 * US], fps=30)
        self.assertEqual([sum(c["segment_us"] for c in m) for m in members], [3 * US, total - 3 * US])
        chunks = [c for m in members for c in m]
        for a, b in zip(chunks, chunks[1:]):
            self.assertEqual(a["source_end_us"], b["source_start_us"])
        self.assertEqual(chunks[0]["source_start_us"], plan["parts"][0]["source_start_us"])
        near = plan["parts"][0]["segment_us"] + 10_000
        snapped = scene_rules.slice_plan(plan, [near, total - near], fps=30)
        self.assertEqual([c["phase"] for c in snapped[0]], ["pour"])
        self.assertIsNone(scene_rules.slice_plan(plan, [US, US], fps=30))

    def test_consecutive_absorb_sentences_share_one_experiment(self):
        manifest = {**MANIFEST, "product": "萌睡裤", "delivery_mode": "preview"}
        matched = self._matched("吸水实验，倒下去", 3 * US)
        second = dict(matched["manifest"]["segments"][0], text="瞬吸不回渗",
                      audio_duration_us=4 * US)
        matched["manifest"]["segments"].append(second)
        matched["manifest"]["segments"].append(dict(second, text="趁现在赶紧囤"))
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": PHASES})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(manifest, matched, {"shots": [SHOT]}, Path(tmp), locate=locate)
        first, second, third = matched["manifest"]["segments"]
        self.assertEqual(report["rows"][0]["indices"], [1, 2])
        self.assertEqual(locate.call_count, 1)
        self.assertEqual(first["scene_rule"]["group"], [1, 2])
        phases = []
        for seg in (first, second):
            if seg.get("multi_shot"):
                phases += [p["detail"]["scene_phase"] for p in seg["span_parts"]]
            else:
                phases.append(seg["evidence_intervals"][0]["scene_phase"])
        self.assertEqual(phases[0], "pour")
        self.assertEqual(phases[-1], "wipe")
        self.assertIn("absorb", phases)
        self.assertNotIn("scene_rule", third)

    def test_too_long_becomes_visual_missing_in_preview(self):
        # 0.5s 连 burst 4× 也装不下完整倒水+擦拭，才留黑。
        manifest = {**MANIFEST, "product": "萌睡裤", "delivery_mode": "preview"}
        matched = self._matched("吸水实验，倒下去瞬吸", int(0.5 * US))
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": PHASES})
        with tempfile.TemporaryDirectory() as tmp:
            scene_rules.apply(manifest, matched, {"shots": [SHOT]}, Path(tmp), locate=locate)
        seg = matched["manifest"]["segments"][0]
        self.assertTrue(seg["visual_missing"])
        self.assertEqual(seg["visual_missing_reason_code"], scene_rules.SCENE_ACTION_TOO_LONG)
        self.assertEqual(matched["visual_missing_count"], 1)
        self.assertEqual(matched["visual_missing_matches"][0]["source"], "scene_rules")

    def test_formal_keeps_shot_when_unmet(self):
        manifest = {**MANIFEST, "product": "萌睡裤", "delivery_mode": "formal"}
        matched = self._matched("吸水实验，倒下去瞬吸", int(0.5 * US))
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": PHASES})
        with tempfile.TemporaryDirectory() as tmp:
            scene_rules.apply(manifest, matched, {"shots": [SHOT]}, Path(tmp), locate=locate)
        seg = matched["manifest"]["segments"][0]
        self.assertFalse(seg.get("visual_missing"))
        self.assertEqual(seg["scene_rule"]["status"], "unmet")

    def test_phase_unverified_keeps_original(self):
        manifest = {**MANIFEST, "product": "萌睡裤", "delivery_mode": "preview"}
        matched = self._matched("吸水实验，倒下去瞬吸", 8 * US)
        locate = mock.Mock(return_value={"ok": False, "reason": "PHASE_VISION_FAILED"})
        with tempfile.TemporaryDirectory() as tmp:
            scene_rules.apply(manifest, matched, {"shots": [SHOT]}, Path(tmp), locate=locate)
        seg = matched["manifest"]["segments"][0]
        self.assertFalse(seg.get("visual_missing"))
        self.assertEqual(seg["scene_rule"]["reason"], scene_rules.SCENE_PHASE_UNVERIFIED)

    def test_full_window_covers_action_with_hold(self):
        shot = {**SHOT, "shot_id": "p1", "action": "指向", "description": "手指着产品正面",
                "pointing_to_text": True}
        manifest = {**MANIFEST, "product": "日用卫生巾", "delivery_mode": "preview"}
        matched = self._matched("这款真的很好用", 3 * US, shot=shot)
        locate = mock.Mock(return_value={"ok": True, "complete": True,
                                         "phases": {"action": [12.0, 13.5]}})
        with tempfile.TemporaryDirectory() as tmp:
            scene_rules.apply(manifest, matched, {"shots": [shot]}, Path(tmp), locate=locate)
        seg = matched["manifest"]["segments"][0]
        self.assertEqual(seg["scene_rule"]["id"], "point_front")
        window = seg["window_solution"]
        self.assertLessEqual(window["source_start"], 12.0)
        self.assertGreaterEqual(window["source_end"], 14.5 - 1e-6)
        self.assertEqual(seg["evidence_intervals"][0]["end"], 14.5)

    def test_surface_swaps_to_candidate_with_surface(self):
        plain = {**SHOT, "shot_id": "x1", "action": "举起", "object": "包装",
                 "description": "手举包装", "role": "product_display"}
        surface = {**SHOT, "shot_id": "x2", "video": "D:/m/surface.mp4", "action": "抚摸",
                   "object": "面层", "description": "面层纹理特写", "role": "product_display",
                   "frame_time": 20.0}
        manifest = {**MANIFEST, "product": "日用卫生巾", "delivery_mode": "preview"}
        matched = self._matched("面层软软的", 2 * US, shot=plain,
                                top3=[{"shot_id": "x2", "eligible": True}])
        with tempfile.TemporaryDirectory() as tmp:
            scene_rules.apply(manifest, matched, {"shots": [plain, surface]}, Path(tmp),
                              locate=mock.Mock())
        seg = matched["manifest"]["segments"][0]
        self.assertEqual(seg["temporary_shot_id"], "x2")
        self.assertTrue(seg["scene_rule"]["swapped"])

    def test_long_pointing_swaps_to_shot_without_pointing(self):
        pointing = {**SHOT, "shot_id": "p1", "action": "指向", "description": "手指着产品正面",
                    "pointing_to_text": True, "role": "product_display"}
        plain = {**SHOT, "shot_id": "p2", "video": "D:/m/plain.mp4", "action": "举起",
                 "object": "包装", "description": "手举包装", "role": "product_display",
                 "frame_time": 20.0, "head_waste": 0.1, "tail_waste": 0.0}
        manifest = {**MANIFEST, "product": "日用卫生巾", "delivery_mode": "preview"}
        matched = self._matched("我是真没想到", 1 * US, shot=pointing,
                                top3=[{"shot_id": "p2", "eligible": True}])
        locate = mock.Mock(return_value={"ok": True, "complete": True,
                                         "phases": {"action": [11.0, 19.0]}})
        with tempfile.TemporaryDirectory() as tmp:
            scene_rules.apply(manifest, matched, {"shots": [pointing, plain]}, Path(tmp),
                              locate=locate)
        seg = matched["manifest"]["segments"][0]
        self.assertFalse(seg.get("visual_missing"))
        self.assertEqual(seg["temporary_shot_id"], "p2")
        self.assertEqual(seg["head_waste"], 0.1)
        self.assertEqual(locate.call_count, 1)

    def _long_pointing(self, action, pool=()):
        pointing = {**SHOT, "shot_id": "p1", "action": "指向", "description": "手指着产品正面",
                    "pointing_to_text": True, "role": "product_display"}
        manifest = {**MANIFEST, "product": "日用卫生巾", "delivery_mode": "preview"}
        matched = self._matched("我是真没想到", 1 * US, shot=pointing)
        matched["manifest"]["segments"][0]["selection_mode"] = "degraded_fallback"
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": {"action": action}})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(manifest, matched, {"shots": [pointing, *pool]}, Path(tmp),
                                       locate=locate)
        return report["rows"][0], matched["manifest"]["segments"][0]

    def test_long_pointing_uses_frames_after_pointing(self):
        row, seg = self._long_pointing([11.0, 19.0])
        self.assertEqual((row["status"], row["mode"]), ("applied", "settled"))
        self.assertGreaterEqual(seg["window_solution"]["source_start"], 19.0)

    def test_formal_rejects_settled_when_action_too_long(self):
        """正式稿不得用动作结束后的定住画面冒充完整动作。"""
        pointing = {**SHOT, "shot_id": "p1", "action": "指向", "description": "手指着产品正面",
                    "pointing_to_text": True, "role": "product_display"}
        manifest = {**MANIFEST, "product": "日用卫生巾", "delivery_mode": "formal"}
        matched = self._matched("我是真没想到", 1 * US, shot=pointing)
        locate = mock.Mock(return_value={
            "ok": True, "complete": True, "phases": {"action": [1.0, 15.0]}})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(manifest, matched, {"shots": [pointing]}, Path(tmp),
                                       locate=locate)
        row = report["rows"][0]
        seg = matched["manifest"]["segments"][0]
        self.assertEqual(row["status"], "unmet")
        self.assertNotEqual(row.get("mode"), "settled")
        self.assertNotEqual(row.get("mode"), "lead_in")
        self.assertFalse(seg.get("visual_missing"))
        self.assertEqual(seg["scene_rule"]["status"], "unmet")

    def test_long_pointing_uses_frames_before_pointing(self):
        row, seg = self._long_pointing([13.0, 29.9])
        self.assertEqual((row["status"], row["mode"]), ("applied", "lead_in"))
        self.assertLessEqual(seg["window_solution"]["source_end"], 13.0 + 1e-6)

    def test_pointing_over_whole_shot_takes_an_unused_pool_shot(self):
        spare = {**SHOT, "shot_id": "p3", "video": "D:/m/spare.mp4", "action": "举起",
                 "object": "包装", "description": "手举包装", "role": "product_display",
                 "frame_time": 20.0}
        row, seg = self._long_pointing([10.0, 30.0], pool=[spare])
        self.assertEqual(row["status"], "applied")
        self.assertFalse(seg.get("visual_missing"))
        self.assertEqual(seg["temporary_shot_id"], "p3")

    def test_absorb_group_skips_backlight_breathability_shots(self):
        """瞬吸句不能把透光轻薄镜头当吸水实验演。"""
        backlight = {**SHOT, "shot_id": "light1", "video": "D:/m/light.mp4",
                     "action": "举起", "object": "巾体",
                     "description": "手持巾体翻面后举到灯光前透光，可见内部吸水层",
                     "evidence_tags": ["对光展示", "光线透过巾体", "吸水层结构"],
                     "role": "usage_demo"}
        pour = {**SHOT, "shot_id": "pour1", "video": "D:/m/pour.mp4",
                "description": "手持玻璃杯向巾体倾倒蓝色液体后离开",
                "evidence_tags": ["倾倒蓝色液体", "液体被吸收"]}
        manifest = {**MANIFEST, "product": "日用卫生巾", "category": "weipin",
                    "delivery_mode": "preview"}
        matched = self._matched("16倍的瞬吸技术", 2 * US, shot=backlight,
                                top3=[{"shot_id": "pour1", "eligible": True}])
        matched["manifest"]["segments"].append({
            **matched["manifest"]["segments"][0], "text": "吸的又多又快",
            "temporary_shot_id": "light1", "audio_duration_us": 2 * US})
        matched["recommendations"].append({"index": 2, "top3": [{"shot_id": "pour1", "eligible": True}]})
        phases = {"pour": [1.0, 3.0], "absorb": [3.0, 8.0], "wipe": [8.0, 10.0]}
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": phases})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(manifest, matched, {"shots": [backlight, pour]},
                                       Path(tmp), locate=locate)
        row = next(r for r in report["rows"] if r.get("scene") == "absorb_test")
        self.assertNotEqual(row.get("shot_id"), "light1")
        attempted = {a["shot_id"] for a in row.get("attempts") or []}
        self.assertNotIn("light1", attempted)

    def _two_pointing(self, second_video, action, pool=(), budget=24):
        first = {**SHOT, "shot_id": "p1", "video": "D:/a/IMG_1.MOV", "action": "指向",
                 "description": "手指着产品正面", "pointing_to_text": True, "role": "product_display"}
        second = {**first, "shot_id": "p2", "video": second_video}
        manifest = {**MANIFEST, "product": "日用卫生巾", "delivery_mode": "preview"}
        matched = self._matched("我是真没想到", 1 * US, shot=first)
        seg = dict(matched["manifest"]["segments"][0], selection_mode="degraded_fallback")
        matched["manifest"]["segments"] = [
            seg, dict(seg, text="真的很好用", video=second["video"], temporary_shot_id="p2")]
        matched["recommendations"].append({"index": 2, "top3": []})
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": {"action": action}})
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(scene_rules, "_source_budget", return_value=budget):
            report = scene_rules.apply(manifest, matched, {"shots": [first, second, *pool]},
                                       Path(tmp), locate=locate)
        return report["rows"], matched["manifest"]["segments"]

    def test_fallback_skips_a_copy_of_a_file_another_sentence_uses(self):
        rows, segs = self._two_pointing("D:/b/img_1.mov", [11.0, 19.0])
        names = [Path(s["video"]).name.casefold() for s in segs if not s.get("visual_missing")]
        self.assertEqual(len(names), len(set(names)))
        self.assertIn(scene_rules.SCENE_SOURCE_DUPLICATE, [a["reason"] for a in rows[0]["attempts"]])

    def test_pool_fill_respects_the_source_budget(self):
        spare = {**SHOT, "shot_id": "p3", "video": "D:/m/spare.mp4", "action": "举起",
                 "object": "包装", "description": "手举包装", "role": "product_display",
                 "frame_time": 20.0}
        rows, segs = self._two_pointing("D:/b/IMG_2.MOV", [10.0, 30.0], pool=[spare], budget=1)
        self.assertNotIn("D:/m/spare.mp4", [s.get("video") for s in segs])
        self.assertIn(scene_rules.SCENE_SOURCE_BUDGET, [a["reason"] for a in rows[0]["attempts"]])

    def test_evidence_frame_time_follows_interval(self):
        from orchestrator.shot_analyzer import candidate_evidence_interval
        shot = {"source_start": 0.0, "source_end": 7.2, "frame_time": 3.6,
                "evidence_intervals": [{"start": 6.8, "end": 7.2}]}
        self.assertEqual(candidate_evidence_interval(shot), (6.8, 7.2, 7.0))
        shot["evidence_intervals"][0]["frame_time"] = 7.05
        self.assertEqual(candidate_evidence_interval(shot)[2], 7.05)

    def test_zhipin_has_no_scene_rules(self):
        manifest = {**MANIFEST, "product": "湿厕纸"}
        matched = self._matched("吸水实验，倒下去瞬吸", 8 * US)
        locate = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(manifest, matched, {"shots": [SHOT]}, Path(tmp), locate=locate)
        locate.assert_not_called()
        self.assertEqual(report["rows"], [])


HOLD = {**SHOT, "shot_id": "h1", "video": "D:/m/hold.mp4", "action": "举起", "object": "湿厕纸包装",
        "description": "手拿一包湿厕纸入画举到镜头前", "action_phase": "perform",
        "role": "product_display", "source_start": 0.0, "source_end": 12.0,
        "material_duration_us": 12 * US}


class ZhipinRuleTests(unittest.TestCase):
    MANIFEST = {**MANIFEST, "product": "纯水湿厕纸", "delivery_mode": "preview"}

    def _apply(self, seg_shot, audio_us, pool, phases, top3=None):
        matched = SceneApplyTests()._matched("这款湿厕纸真的很好用", audio_us, shot=seg_shot, top3=top3)
        locate = mock.Mock(side_effect=lambda shot, _scene, _dir: {
            "ok": True, "complete": shot["shot_id"] in phases,
            "phases": {"action": phases.get(shot["shot_id"], [9.0, 12.0])}})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(self.MANIFEST, matched, {"shots": pool}, Path(tmp), locate=locate)
        return report, matched["manifest"]["segments"][0]

    def test_only_moving_shots_trigger(self):
        rules = rulepack.load("zhipin")
        self.assertEqual(rulepack.scene_for(rules, "随便一句", HOLD)["id"], "show_product")
        stacking = {**HOLD, "action": "堆叠", "description": "多包湿巾一包包落入篮中堆成一堆"}
        self.assertEqual(rulepack.scene_for(rules, "随便一句", stacking)["id"], "stack_product")
        self.assertIsNone(rulepack.scene_for(rules, "随便一句", {**stacking, "action_phase": "static"}))
        self.assertIsNone(rulepack.scene_for(rulepack.load("weipin"), "随便一句", HOLD))
        wring = {**HOLD, "action": "拧挤", "description": "手持湿巾拧挤，可见水滴挤出"}
        self.assertIsNone(rulepack.scene_for(rules, "随便一句", wring))

    def test_cta_sentence_swaps_to_quantity_shot_from_pool(self):
        stack = {**HOLD, "shot_id": "st", "action": "投入", "description": "多包湿巾投入收纳篮",
                 "source_end": 4.0, "material_duration_us": 4 * US}
        basket = {**HOLD, "shot_id": "bk", "video": "D:/m/bk.mp4", "action": "无动作",
                  "action_phase": "static", "role": "context", "setup_id": "basket",
                  "description": "金色购物篮内平铺着大量蓝色包装湿巾", "visual_tags": ["多包", "湿巾"],
                  "evidence_tags": ["大量包装"], "frame_time": 4.0}
        single = {**basket, "shot_id": "sg", "video": "D:/m/sg.mp4", "setup_id": "single",
                  "description": "木桌上一包湿巾", "visual_tags": ["湿巾"], "evidence_tags": []}
        matched = SceneApplyTests()._matched("想要囤货的", int(1.2 * US), shot=stack)
        matched["manifest"]["segments"][0]["selection_mode"] = "auto_top1_with_reuse_guard"
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": {"action": [0.3, 3.9]}})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(self.MANIFEST, matched, {"shots": [stack, single, basket]},
                                       Path(tmp), locate=locate)
        row = report["rows"][0]
        self.assertEqual((row["scene"], row["status"], row["shot_id"]), ("stack_product", "applied", "bk"))
        self.assertEqual([a["shot_id"] for a in row["attempts"] if a.get("source") == "pool_fill"], ["bk"])

    def test_window_covers_entry_to_hold(self):
        report, seg = self._apply(HOLD, 3 * US, [HOLD], {"h1": [1.0, 3.5]})
        self.assertEqual(report["rows"][0]["scene"], "show_product")
        self.assertEqual(report["rows"][0]["mode"], "full")
        window = seg["window_solution"]
        self.assertLessEqual(window["source_start"], 1.0)
        self.assertGreaterEqual(window["source_end"], 3.8 - 1e-6)

    def test_complete_action_on_other_candidate_beats_settled(self):
        other = {**HOLD, "shot_id": "h2", "video": "D:/m/hold2.mp4", "setup_id": "other"}
        report, seg = self._apply(HOLD, 2 * US, [HOLD, other], {"h1": [1.0, 9.0], "h2": [2.0, 3.5]},
                                  top3=[{"shot_id": "h2", "eligible": True}])
        row = report["rows"][0]
        self.assertEqual((row["shot_id"], row["mode"]), ("h2", "full"))

    def test_too_long_action_falls_back_to_settled_frames(self):
        report, seg = self._apply(HOLD, 2 * US, [HOLD], {"h1": [1.0, 9.0]})
        row = report["rows"][0]
        self.assertEqual((row["status"], row["mode"]), ("applied", "settled"))
        self.assertGreaterEqual(seg["window_solution"]["source_start"], 9.0)
        self.assertFalse(seg.get("visual_missing"))

    def _filler(self, mode):
        short = {**HOLD, "source_end": 9.5, "material_duration_us": int(9.5 * US)}
        static_same = {**HOLD, "shot_id": "s1", "video": "D:/m/s1.mp4", "action": "无动作",
                       "action_phase": "static", "description": "木桌上静置一包湿厕纸",
                       "setup_id": "front", "frame_time": 4.0}
        static_new = {**static_same, "shot_id": "s2", "video": "D:/m/s2.mp4", "setup_id": "top"}
        used_one = {**static_same, "shot_id": "u1", "video": "D:/m/u1.mp4", "setup_id": "front"}
        matched = SceneApplyTests()._matched("真的绝", int(0.8 * US), shot=short)
        matched["manifest"]["segments"][0]["selection_mode"] = mode
        matched["manifest"]["segments"].append({"text": "数量有限", "video": "D:/m/u1.mp4",
                                                "temporary_shot_id": "u1"})
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": {"action": [1.0, 9.0]}})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(self.MANIFEST, matched,
                                       {"shots": [short, static_same, static_new, used_one]},
                                       Path(tmp), locate=locate)
        return report["rows"][0], matched["manifest"]["segments"][0]

    def test_filler_sentence_swaps_to_unused_static_shot(self):
        row, seg = self._filler("auto_top1_degraded_substitute")
        self.assertEqual((row["status"], row["shot_id"]), ("applied", "s2"))
        self.assertEqual(row["attempts"][-1]["source"], "pool_fill")
        self.assertFalse(seg.get("visual_missing"))

    def test_matched_sentence_still_goes_black(self):
        row, seg = self._filler("auto_top1_with_reuse_guard")
        self.assertEqual(row["status"], "visual_missing")
        self.assertTrue(seg["visual_missing"])

    def test_brand_sentence_takes_product_display_from_pool(self):
        static = {**HOLD, "shot_id": "pd", "video": "D:/m/pd.mp4", "action": "无动作",
                  "action_phase": "static", "setup_id": "desk", "frame_time": 4.0,
                  "description": "木桌上静置一包蓝色包装", "visual_tags": [], "evidence_tags": []}
        matched = SceneApplyTests()._matched("心相印这款悬挂式纯水湿厕纸", int(2.5 * US), shot=HOLD)
        matched["manifest"]["segments"][0]["selection_mode"] = "auto_top1_with_reuse_guard"
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": {"action": [0.0, 12.0]}})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(self.MANIFEST, matched, {"shots": [HOLD, static]},
                                       Path(tmp), locate=locate)
        row = report["rows"][0]
        self.assertEqual((row["status"], row["shot_id"]), ("applied", "pd"))
        self.assertEqual(row["attempts"][-1]["source"], "pool_fill")

    def test_swap_to_repeated_look_comes_after_pool(self):
        repeat = {**HOLD, "shot_id": "rp", "video": "D:/m/rp.mp4", "setup_id": "front"}
        earlier = {**repeat, "shot_id": "e1", "video": "D:/m/e1.mp4", "action_phase": "static"}
        static = {**HOLD, "shot_id": "s2", "video": "D:/m/s2.mp4", "action": "无动作",
                  "action_phase": "static", "setup_id": "top", "frame_time": 4.0,
                  "description": "木桌上静置一包湿厕纸"}
        matched = SceneApplyTests()._matched("真的绝", int(2 * US), shot=HOLD,
                                             top3=[{"shot_id": "rp", "eligible": True}])
        matched["manifest"]["segments"][0]["selection_mode"] = "auto_top1_degraded_substitute"
        matched["manifest"]["segments"].append({"text": "数量有限", "video": "D:/m/e1.mp4",
                                                "temporary_shot_id": "e1"})
        locate = mock.Mock(side_effect=lambda shot, _s, _d: {
            "ok": True, "complete": True,
            "phases": {"action": [1.0, 12.0] if shot["shot_id"] == "h1" else [1.0, 2.5]}})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(self.MANIFEST, matched, {"shots": [HOLD, repeat, earlier, static]},
                                       Path(tmp), locate=locate)
        row = report["rows"][0]
        self.assertEqual((row["status"], row["shot_id"]), ("applied", "s2"))
        self.assertNotIn("rp", [a["shot_id"] for a in row["attempts"]])

    def test_similar_looks_are_used_once(self):
        from orchestrator import shot_analyzer as sa

        def shot(name, setup):
            return {**HOLD, "shot_id": name, "video": f"D:/m/{name}.MOV", "source_start": 0.0,
                    "source_end": 8.0, "duration": 8.0, "frame_time": 4.0, "setup_id": setup,
                    "action_phase": "static", "status": "ready_for_matching",
                    "action_complete": True, "has_subject": True, "evidence_strength": "B",
                    "result": "包装完整", "visual_tags": ["湿厕纸"], "evidence_tags": ["湿厕纸包装"]}
        shots = [shot("A1", "front"), shot("A2", "front"), shot("B1", "side"), shot("C1", "top")]
        picks = {1: [("A1", 90)], 2: [("B1", 90)], 3: [("A2", 90), ("C1", 85)]}
        analysis = {"shots": shots, "semantic_matches": [
            {"index": i, "top3": [{"shot_id": s, "score": sc, "matched_claims": ["湿厕纸包装"]}
                                  for s, sc in rows]} for i, rows in picks.items()]}
        segments = [{"text": t, "audio_duration_us": int(1.5 * US)}
                    for t in ("这款湿厕纸", "包装很好看", "这款湿厕纸")]
        base = {"segments": segments, "head_trim_s": 0.3, "tail_trim_s": 0.2, "fps": 30,
                "max_source_reuse": 2, "delivery_mode": "formal"}

        def third(flag):
            res = sa.match_manifest({**base, "avoid_similar_shots": flag}, json.loads(json.dumps(analysis)))
            return [s["temporary_shot_id"] for s in res["segments"]]
        self.assertEqual(third(False)[2], "A2")
        self.assertEqual(third(True), ["A1", "B1", "C1"])

        carry_shots = [shot("A1", "front"), shot("A2", "front"), shot("B1", "side")]
        res = sa.match_manifest(
            {**base, "delivery_mode": "preview", "avoid_similar_shots": True,
             "segments": [{"text": t, "audio_duration_us": int(1.5 * US)}
                          for t in ("真的绝", "太香了", "冲就完了", "爱了爱了", "闭眼入")]},
            {"shots": carry_shots, "semantic_matches": []})
        carried = [s["temporary_shot_id"] for s in res["segments"]
                   if s.get("substitute_reason") == "UNUSED_READY_CARRY"]
        self.assertEqual(len(carried), 2)
        self.assertEqual(len({sa._look_key(next(x for x in carry_shots if x["shot_id"] == c))
                              for c in carried}), 2)
        self.assertTrue(rulepack.load("zhipin")["avoid_similar_shots"])
        self.assertFalse(rulepack.load("weipin")["avoid_similar_shots"])


class SentenceFitTests(unittest.TestCase):
    def setUp(self):
        from orchestrator import demo_actions
        self.da = demo_actions
        self.contract = demo_actions.sentence_contract(
            "用完直接丢马桶可溶解不堵管", category="纸品", product="纯水湿厕纸")

    def test_seen_dispersal_fits_flush_sentence(self):
        shot = {"description": "巾体被提起时下垂呈碎絮状，之后盆底和排水口附近散落半透明碎片"}
        self.assertEqual(self.da.shot_fit(shot, self.contract), 1)

    def test_denied_dispersal_does_not_fit(self):
        intact = {"description": "双手拉伸巾体，全程巾体未见破洞、散开或碎裂，形状保持完整",
                  "action_segments": [], "evidence_tags": ["巾体完整"]}
        drain = {"description": "排水口静置，排水口无可见碎片残留"}
        self.assertEqual(self.da.shot_fit(intact, self.contract), 0)
        self.assertEqual(self.da.shot_fit(drain, self.contract), 0)

    def test_result_sentences_prefer_shots_that_show_the_result(self):
        for text in ("用完直接丢马桶可溶解不堵管", "擦完清爽像水洗", "湿度刚刚好不滴水"):
            self.assertIn("effect", self.da.infer_intents(text, text, "纸品"), text)

    def test_printed_pull_claim_is_not_a_pull_shot(self):
        contract = self.da.sentence_contract("单手一抽一张不连张", category="纸品", product="纯水湿厕纸")
        package = {"description": "手持包装展示正面，手指指向包装文字", "readable_claims": ["不连抽"]}
        pull = {"description": "包装挂在挂钩上，手从开口处拉出巾体"}
        self.assertEqual(self.da.shot_fit(package, contract), 0)
        self.assertEqual(self.da.shot_fit(pull, contract), 1)

    def test_scene_window_uses_the_segment_the_sentence_talks_about(self):
        scene = next(s for s in rulepack.load("zhipin")["scenes"] if s["id"] == "usage_action")
        contract = self.da.sentence_contract("单手一抽一张不连张", category="纸品", product="纯水湿厕纸")
        shot = {"source_start": 0.0, "source_end": 6.0, "action_segments": [
            {"start": 0.7, "end": 1.5, "action": "翻开包装翻盖", "result": "翻盖打开"},
            {"start": 2.2, "end": 3.7, "action": "单手抽出一张湿巾", "result": "抽出一张"},
            {"start": 5.1, "end": 5.9, "action": "合上翻盖", "result": "翻盖合上"}]}
        located = scene_rules._segment_phases(scene, shot, contract)
        self.assertTrue(located["complete"])
        self.assertEqual(located["phases"]["action"], [1.45, 4.45])
        self.assertIsNone(scene_rules._segment_phases(scene, {"action_segments": []}, contract))
        wipe = self.da.sentence_contract("擦完清爽像水洗", category="纸品", product="纯水湿厕纸")
        self.assertIsNone(scene_rules._segment_phases(scene, shot, wipe))

    def test_pull_sentence_moves_evidence_from_hook_to_pull_segment(self):
        from orchestrator import shot_analyzer
        contract = self.da.sentence_contract("单手一抽一张不连张", category="纸品", product="纯水湿厕纸")
        shot = {"source_start": 0.0, "source_end": 9.4,
                "evidence_intervals": [{"start": 2.0, "end": 4.0, "frame_time": 3.0}],
                "action_segments": [{"start": 1.5, "end": 3.4, "action": "挂到挂钩上"},
                                    {"start": 7.0, "end": 9.4, "action": "抽取"}]}
        focused = shot_analyzer.focus_on_topic(shot, contract)
        self.assertEqual(focused["evidence_intervals"][0]["start"], 7.0)
        self.assertEqual(focused["evidence_intervals"][0]["end"], 9.4)
        self.assertEqual(shot["evidence_intervals"][0]["start"], 2.0)
        hang = self.da.sentence_contract("挂墙上不占卫生间台面", category="纸品", product="纯水湿厕纸")
        self.assertIs(shot_analyzer.focus_on_topic(shot, hang), shot)

    def test_scene_swap_narrows_long_evidence_for_short_sentence(self):
        static = {**HOLD, "shot_id": "st", "video": "D:/m/st.mp4", "action": "无动作",
                  "action_phase": "static", "source_start": 0.0, "source_end": 14.0,
                  "material_duration_us": 14 * US,
                  "evidence_intervals": [{"start": 6.0, "end": 8.5, "frame_time": 7.4}]}
        seg = {"text": "一定要备上这款", "audio_duration_us": int(1.2 * US)}
        result = scene_rules._plain_swap(seg, static, manifest=ZhipinRuleTests.MANIFEST,
                                         audio_us=int(1.2 * US))
        self.assertTrue(result["ok"], result)
        interval = result["update"]["evidence_intervals"][0]
        self.assertTrue(interval["start"] <= 7.4 <= interval["end"])

    def test_long_evidence_narrows_around_the_audited_frame(self):
        from orchestrator import shot_analyzer
        shot = {"source_start": 0.0, "source_end": 16.9,
                "evidence_intervals": [{"start": 9.5, "end": 12.0, "frame_time": 11.6}]}
        narrowed = shot_analyzer.narrow_evidence(shot, 2_249_575)
        interval = narrowed["evidence_intervals"][0]
        self.assertLessEqual(interval["end"] - interval["start"], 2.25)
        self.assertTrue(interval["start"] <= 11.6 <= interval["end"])
        self.assertGreaterEqual(interval["start"], 9.5)
        self.assertIsNone(shot_analyzer.narrow_evidence(shot, 3_000_000))

    def test_printed_flush_claim_is_not_a_flush_shot(self):
        back = {"description": "手持包装背面展示，背面可见可冲散字样",
                "readable_claims": ["可冲散", "可生物降解"]}
        self.assertEqual(self.da.shot_fit(back, self.contract), 0)
        self.assertTrue(self.da.printed_only_match(["包装背面可见可冲散、可生物降解字样"], self.contract))
        self.assertFalse(self.da.printed_only_match(["巾体在水中散成碎片"], self.contract))
        label = self.da.sentence_contract("EDI纯水配方", category="纸品", product="纯水湿厕纸")
        self.assertFalse(self.da.printed_only_match(["包装正面印有EDI纯水字样"], label))
        self.assertEqual(self.da.shot_fit({"readable_claims": ["EDI纯水"]}, label), 2)

    def test_rule_tag_hit_does_not_override_off_topic_shot(self):
        from orchestrator import shot_analyzer
        drain = {**HOLD, "shot_id": "dr", "video": "D:/m/dr.MOV", "source_start": 0.0,
                 "source_end": 4.5, "duration": 4.5, "frame_time": 1.0, "action": "排水",
                 "action_phase": "static", "status": "ready_for_matching", "action_complete": True,
                 "has_subject": True, "evidence_strength": "B",
                 "description": "水流经排水口流走，盆面干净", "result": "排水口无残留",
                 "visual_tags": ["洗手盆"], "evidence_tags": ["干净"]}
        manifest = {"segments": [{"text": "干纸总擦不干净", "audio_duration_us": int(1.5 * US)}],
                    "category": "纸品", "product_name": "纯水湿厕纸", "head_trim_s": 0.3,
                    "tail_trim_s": 0.2, "fps": 30, "max_source_reuse": 2, "delivery_mode": "formal"}
        res = shot_analyzer.match_manifest(manifest, {"shots": [drain], "semantic_matches": []})
        row = res["recommendations"][0]["top3"][0]
        self.assertFalse(row["eligible"])
        self.assertIn("OFF_TOPIC", row["reuse_block_reason"])

    def test_negation_in_one_field_does_not_leak_into_the_next(self):
        shot = {"description": "提起后巾体仍在手上，未见水龙头",
                "evidence_tags": ["散成碎片"]}
        self.assertEqual(self.da.shot_fit(shot, self.contract), 1)


class EngineHelpersTests(unittest.TestCase):
    def test_parent_zero_is_a_parent(self):
        self.assertEqual(_parent_id(SimpleNamespace(parent_index=0)), 0)
        self.assertEqual(_parent_id(SimpleNamespace(parent_index=None)), -1)
        self.assertEqual(_parent_id(SimpleNamespace(), -2), -2)

    def test_final_window_uses_source_end(self):
        records = [{"index": 1, "evidence_intervals": [{"start": 10.0, "end": 14.0}]}]
        sped = [{"video": "a.mp4", "source_start": 10.0, "source_end": 14.0, "duration": 2.5}]
        self.assertFalse(visual_match.validate_final_windows(sped, records).get("issues"))
        legacy = [{"video": "a.mp4", "source_start": 10.0, "duration": 2.5}]
        self.assertTrue(visual_match.validate_final_windows(legacy, records).get("issues"))


class VoicePostTests(unittest.TestCase):
    def test_tempo_normalize(self):
        self.assertEqual(voice_tts.normalize_tempo(None), 1.0)
        self.assertEqual(voice_tts.normalize_tempo("1.2"), 1.2)
        self.assertEqual(voice_tts.normalize_tempo(9), 2.0)
        name = voice_tts._post_name(Path("tts_abc.ogg"), 1.2, 25, True)
        self.assertEqual(name.name, "tts_abc.p1_k25_t120.ogg")

    def test_postprocess_trims_and_speeds_up(self):
        ffmpeg, ffprobe = voice_tts.find_ffmpeg()[:2]
        if not ffmpeg or not ffprobe:
            self.skipTest("ffmpeg/ffprobe not available")
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "tts_test.ogg"
            subprocess.run([str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.3",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2:sample_rate=48000",
                            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=0.3",
                            "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[a]",
                            "-map", "[a]", "-c:a", "opus", "-strict", "-2", "-b:a", "64k", str(src)],
                           check=True, capture_output=True)
            out = Path(voice_tts.postprocess_voice(src, tempo=1.2))
            self.assertNotEqual(out, src)
            duration = voice_tts._probe_seconds(out)
            self.assertLess(duration, 1.2)
            self.assertGreater(duration, 0.9)
            self.assertEqual(voice_tts.postprocess_voice(src, tempo=1.2), str(out))
            self.assertEqual(voice_tts.postprocess_voice(src, tempo=1.0, edge_trim=False), str(src))


class ExperimentGroupChainTests(unittest.TestCase):
    """场景规则 → 引擎装配 → 去重闸门：连续句共用一段实验的整条链路。"""

    MANIFEST = {**MANIFEST, "product": "萌睡裤", "delivery_mode": "preview"}

    def _planned(self):
        matched = SceneApplyTests()._matched("吸水实验，倒下去", 3 * US)
        first = matched["manifest"]["segments"][0]
        first.update({"visual_tags": ["蓝色液体", "卫生巾"], "evidence_tags": ["倒水", "吸收"]})
        matched["manifest"]["segments"].append(dict(first, text="瞬吸不回渗", audio_duration_us=4 * US))
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": PHASES})
        with tempfile.TemporaryDirectory() as tmp:
            scene_rules.apply(self.MANIFEST, matched, {"shots": [SHOT]}, Path(tmp), locate=locate)
        return matched["manifest"]["segments"]

    def _engine(self, segments, tmp):
        eng = OrchestrationEngine.__new__(OrchestrationEngine)
        eng.m = {**self.MANIFEST, "segments": segments}
        eng.pending, eng.delivery_mode = [], "preview"
        eng.resolve = lambda p: Path(p)
        eng.report_dir = Path(tmp)
        eng._autofill_script_voiceover = mock.Mock()
        eng._event = mock.Mock()
        return eng

    def test_second_sentence_keeps_its_own_assignment(self):
        segments = self._planned()
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(segments, tmp)
            clips = eng.build_clips()
            gate = eng._visual_duplicate_gate(clips)
        for parent, seg in enumerate(segments):
            mine = [c for c in clips if c.parent_index == parent]
            parts = seg["span_parts"] if seg.get("multi_shot") else [seg["window_solution"]]
            self.assertEqual({c.text for c in mine}, {seg["text"]})
            self.assertEqual([round(c.source_start, 3) for c in mine],
                             [round(p["source_start"], 3) for p in parts])
            if seg.get("multi_shot"):
                self.assertEqual(sum(to_us(c.duration) for c in mine),
                                 sum(to_us(p["segment_s"]) for p in parts))
        phases = [(c.evidence_intervals or [{}])[0].get("scene_phase") for c in clips]
        self.assertEqual((phases[0], phases[-1]), ("pour", "wipe"))
        self.assertIsNone(gate)

    def test_complete_experiment_found_outside_top3(self):
        partial = {**SHOT, "shot_id": "s1", "description": "卫生巾吸满蓝色液体"}
        full = {**SHOT, "shot_id": "pool1", "video": "D:/m/full.mp4", "duration": 20.0,
                "description": "往卫生巾上倒蓝色液体，再用纸巾按压"}
        unrelated = {**SHOT, "shot_id": "pack", "video": "D:/m/pack.mp4", "description": "手举包装",
                     "action": "举起", "object": "包装"}
        taken = {**full, "shot_id": "taken", "video": "D:/m/taken.mp4"}
        matched = SceneApplyTests()._matched("吸水实验，倒下去", 3 * US, shot=partial)
        matched["manifest"]["segments"].append(
            dict(matched["manifest"]["segments"][0], text="瞬吸不回渗", audio_duration_us=4 * US))
        matched["manifest"]["segments"].append({"text": "包装好看", "video": "D:/m/taken.mp4",
                                                "temporary_shot_id": "taken"})

        def locate(shot, _scene, _dir):
            if shot["shot_id"] == "pool1":
                return {"ok": True, "complete": True, "phases": PHASES}
            return {"ok": True, "complete": False, "phases": {"absorb": [14.0, 20.0]}}

        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(self.MANIFEST, matched,
                                       {"shots": [partial, full, unrelated, taken]}, Path(tmp),
                                       locate=mock.Mock(side_effect=locate))
        row = report["rows"][0]
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["shot_id"], "pool1")
        self.assertEqual([(a["shot_id"], a["source"]) for a in row["attempts"]],
                         [("pool1", "pool_search")])
        first, second = matched["manifest"]["segments"][:2]
        self.assertEqual({first["video"], second["video"]}, {"D:/m/full.mp4"})

    def _selling_point_script(self, pool):
        chip = {**SHOT, "shot_id": "chip", "video": "D:/m/chip.mp4", "action": "举起",
                "object": "卫生巾芯片", "description": "手拿卫生巾展示本草芯片"}
        matched = SceneApplyTests()._matched("它家用的是这种本草芯片", 3 * US, shot=chip)
        seg = matched["manifest"]["segments"][0]
        for text, audio in (("内含高分子吸水因子吸收更快更干净", 3 * US),
                            ("吸完还不反渗不回流", 4 * US), ("量大的姐妹真的闭眼冲", 2 * US)):
            matched["manifest"]["segments"].append(dict(seg, text=text, audio_duration_us=audio))
        return chip, matched, {"shots": [chip, *pool]}

    def test_selling_point_sentences_get_the_experiment(self):
        full = {**SHOT, "shot_id": "pool1", "video": "D:/m/full.mp4", "duration": 20.0,
                "description": "往卫生巾上倒蓝色液体，再用纸巾按压"}
        _chip, matched, analysis = self._selling_point_script([full])
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": PHASES})
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(self.MANIFEST, matched, analysis, Path(tmp), locate=locate)
        row = report["rows"][0]
        # 「量大」也是吸水命中句，与吸收/反渗同一实验组连续播。
        self.assertEqual((row["indices"], row["trigger"], row["status"]),
                         ([2, 3, 4], "text", "applied"))
        self.assertEqual([(a["shot_id"], a["source"]) for a in row["attempts"]],
                         [("pool1", "pool_search")])
        segments = matched["manifest"]["segments"]
        self.assertEqual([s["video"] for s in segments],
                         ["D:/m/chip.mp4", "D:/m/full.mp4", "D:/m/full.mp4", "D:/m/full.mp4"])

    def test_selling_point_without_experiment_footage_goes_black(self):
        _chip, matched, analysis = self._selling_point_script([])
        locate = mock.Mock()
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(self.MANIFEST, matched, analysis, Path(tmp), locate=locate)
        locate.assert_not_called()
        row = report["rows"][0]
        self.assertEqual((row["status"], row["reason"]),
                         ("visual_missing", scene_rules.SCENE_NO_FOOTAGE))
        segments = matched["manifest"]["segments"]
        self.assertEqual([bool(s.get("visual_missing")) for s in segments],
                         [False, True, True, True])

    def test_selling_point_without_footage_keeps_shots_in_formal(self):
        _chip, matched, analysis = self._selling_point_script([])
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply({**self.MANIFEST, "delivery_mode": "formal"}, matched,
                                       analysis, Path(tmp), locate=mock.Mock())
        self.assertEqual(report["rows"][0]["status"], "unmet")
        self.assertFalse(any(s.get("visual_missing") for s in matched["manifest"]["segments"]))

    def test_experiment_without_wipe_plays_pour_and_absorb(self):
        pour_absorb = {**SHOT, "shot_id": "pa", "video": "D:/m/pa.mp4", "source_start": 0.0,
                       "source_end": 48.5, "material_duration_us": int(48.5 * US),
                       "description": "木托盘上卫生巾平铺并倒入蓝色液体显示吸收"}
        full = {**SHOT, "shot_id": "full", "video": "D:/m/full.mp4", "duration": 5.0,
                "description": "往卫生巾上倒蓝色液体，再用纸巾按压"}
        partial = {"ok": True, "complete": False,
                   "phases": {"pour": [1.2, 5.8], "absorb": [5.8, 43.5]}}

        def run(pool, full_ok):
            _chip, matched, analysis = self._selling_point_script(pool)

            def locate(shot, _scene, _dir):
                if shot["shot_id"] == "full" and full_ok:
                    return {"ok": True, "complete": True, "phases": PHASES}
                return partial
            with tempfile.TemporaryDirectory() as tmp:
                report = scene_rules.apply(self.MANIFEST, matched, analysis, Path(tmp),
                                           locate=mock.Mock(side_effect=locate))
            return report["rows"][0], matched["manifest"]["segments"]

        row, segments = run([pour_absorb], False)
        self.assertEqual((row["status"], row["shot_id"]), ("partial", "pa"))
        self.assertEqual(row["reason"], "SCENE_PHASE_MISSING:wipe")
        self.assertIn("纸巾擦拭", row["operator_note"])
        phases = []
        for seg in segments[1:3]:
            self.assertEqual(seg["scene_rule"]["status"], "partial")
            phases += ([p["detail"]["scene_phase"] for p in seg["span_parts"]] if seg.get("multi_shot")
                       else [seg["evidence_intervals"][0]["scene_phase"]])
        self.assertEqual(phases[0], "pour")
        self.assertIn("absorb", phases)
        self.assertNotIn("wipe", phases)
        row, _segments = run([pour_absorb, full], True)
        self.assertEqual((row["status"], row["shot_id"]), ("applied", "full"))

    def test_long_absorb_fast_forwards_up_to_twenty_times(self):
        shot = {**SHOT, "source_start": 0.0, "source_end": 48.5, "material_duration_us": int(48.5 * US)}
        plan = scene_rules.plan_split(shot, {"pour": [1.2, 5.8], "absorb": [5.8, 43.5]},
                                      SplitSpeedTests.SCENE, audio_us=7 * US, manifest=MANIFEST)
        self.assertTrue(plan["ok"], plan)
        self.assertEqual(plan["missing_phases"], ["wipe"])
        absorb = next(p for p in plan["parts"] if p["phase"] == "absorb")
        self.assertGreater(absorb["speed"], 6.0)
        self.assertLessEqual(absorb["segment_us"], 3 * US + 33_334)
        self.assertEqual(sum(p["segment_us"] for p in plan["parts"]), 7 * US + 80_000)

    def test_absorb_ending_at_material_end_never_overruns(self):
        shot = {**SHOT, "source_start": 0.0, "source_end": 43.537, "material_duration_us": 43_537_000}
        phases = {"pour": [1.197, 5.763], "absorb": [5.767, 43.537]}
        for audio_us in range(5_000_000, 8_000_000, 37_000):
            plan = scene_rules.plan_split(shot, phases, SplitSpeedTests.SCENE, audio_us=audio_us,
                                          manifest=MANIFEST)
            self.assertTrue(plan["ok"], (audio_us, plan))
            parts = plan["parts"]
            self.assertLessEqual(parts[-1]["source_end_us"], 43_537_000)
            for a, b in zip(parts, parts[1:]):
                self.assertEqual(a["source_end_us"], b["source_start_us"])
            for p in parts:
                material = p["source_end_us"] - p["source_start_us"]
                self.assertLessEqual(abs(material / p["speed"] - p["segment_us"]), 33_334)

    def test_dry_result_sentence_follows_but_never_starts_a_group(self):
        rules = rulepack.load("weipin")
        seg = {"video": "D:/m/a.mp4", "temporary_shot_id": "s1"}
        texts = ["穿着干爽透气", "吸收力老强了", "瞬间吸收不带反渗的", "就特别的干爽", "趁划算多囤"]
        groups = scene_rules._split_groups([dict(seg, text=t) for t in texts], rules, {"s1": SHOT})
        self.assertEqual([g["positions"] for g in groups], [[1, 2, 3]])

    @staticmethod
    def _script(lines):
        seg = {"video": "D:/m/a.mp4", "temporary_shot_id": "s1"}
        return [dict(seg, text=t, audio_duration_us=int(s * US)) for t, s in lines]

    def test_absorb_group_spans_follow_up_sentences(self):
        rules = rulepack.load("weipin")
        segments = self._script([("轻薄的同时", 1.18), ("吸收力还嘎嘎牛", 1.55), ("你就是姨妈量大的", 1.66),
                                 ("也能快速兜住", 1.32), ("表面干干爽爽的", 1.56), ("一点也不反渗", 1.25),
                                 ("而且这升级后的面层", 1.79)])
        groups = scene_rules._split_groups(segments, rules, {"s1": SHOT}, category="weipin")
        # 「量大」本身是吸水命中句；干爽/兜住续进组。面层句阻断。组最长 5 句。
        absorb = [g["positions"] for g in groups if g["scene"]["id"] == "absorb_test"]
        self.assertEqual(absorb, [[1, 2, 3, 4, 5]])

    def test_absorb_related_sentences_stay_one_group_up_to_five(self):
        rules = rulepack.load("weipin")
        scene = next(s for s in rules["scenes"] if s["id"] == "absorb_test")
        self.assertEqual(scene["group_max"], 5)
        segments = self._script([
            ("吸收速度很快", 1.0), ("锁在中央不扩散", 1.0), ("量大也不怕", 1.0),
            ("表面干干爽爽", 1.0), ("一点也不反渗", 1.0),
        ])
        groups = scene_rules._split_groups(segments, rules, {"s1": SHOT}, category="weipin")
        absorb = [g for g in groups if g["scene"]["id"] == "absorb_test"]
        self.assertEqual(len(absorb), 1)
        self.assertEqual(absorb[0]["positions"], [0, 1, 2, 3, 4])
        # 透气主题不得吞进吸水组
        blocked = self._script([("吸收速度很快", 1.0), ("超透气不闷热", 1.0), ("一点也不反渗", 1.0)])
        groups = scene_rules._split_groups(blocked, rules, {"s1": SHOT}, category="weipin")
        absorb = [g for g in groups if g["scene"]["id"] == "absorb_test"]
        self.assertEqual([g["positions"] for g in absorb], [[0], [2]])

    def test_absorb_group_takes_the_next_absorb_sentence(self):
        rules = rulepack.load("weipin")
        segments = self._script([("轻薄透气的同时", 1.48), ("吸收力也没落下", 1.75),
                                 ("你看这肉眼可见的吸进去了", 2.72), ("来姨妈囤上几包", 1.45),
                                 ("都会更安心", 1.16)])
        groups = scene_rules._split_groups(segments, rules, {"s1": SHOT}, category="weipin")
        self.assertEqual([g["positions"] for g in groups], [[1, 2]])

    def test_absorb_group_bridges_one_unrelated_sentence(self):
        rules = rulepack.load("weipin")
        segments = self._script([("吸收力老强了", 2.0), ("姐妹们", 0.8), ("一点也不反渗", 1.5), ("赶紧下单", 1.0)])
        groups = scene_rules._split_groups(segments, rules, {"s1": SHOT}, category="weipin")
        self.assertEqual([g["positions"] for g in groups], [[0, 1, 2]])

    def test_short_absorb_group_extends_into_free_neighbours_only(self):
        rules = rulepack.load("weipin")
        segments = self._script([("吸收力超强", 1.5), ("姐妹们冲", 1.2), ("真的好用", 1.0), ("再来一包", 1.0)])
        groups = scene_rules._split_groups(segments, rules, {"s1": SHOT}, category="weipin")
        self.assertEqual([g["positions"] for g in groups], [[0, 1]])
        segments = self._script([("吸收力超强", 1.5), ("看这背面的说明", 1.2), ("真的好用", 1.0)])
        groups = scene_rules._split_groups(segments, rules, {"s1": SHOT}, category="weipin")
        self.assertEqual([g["positions"] for g in groups], [[0]])
        segments = self._script([("吸收力超强", 1.5), ("姐妹们冲", 1.2)])
        segments[1]["visual_missing"] = True
        groups = scene_rules._split_groups(segments, rules, {"s1": SHOT})
        self.assertEqual([g["positions"] for g in groups], [[0]])

    def _borrow(self, first_s, second_s):
        other = {**SHOT, "shot_id": "s2", "video": "D:/m/other.mp4", "action": "举起",
                 "object": "包装", "description": "手拿包装"}
        matched = SceneApplyTests()._matched("吸收力超强", int(first_s * US))
        matched["manifest"]["segments"].append(
            {"text": "姐妹们冲", "video": other["video"], "source_start": 10.0, "source_end": 30.0,
             "temporary_shot_id": "s2", "audio_duration_us": int(second_s * US)})
        # 倒水 17s：单句 3.6s 连 burst 4× 仍不够，借 1s 后够；借 0.2s 仍不够。
        phases = {"pour": [11.0, 28.0], "absorb": [28.0, 30.0]}
        locate = mock.Mock(side_effect=lambda shot, _scene, _dir: (
            {"ok": True, "complete": False, "phases": phases} if shot["shot_id"] == "s1"
            else {"ok": False, "reason": "NO_PHASES"}))
        with tempfile.TemporaryDirectory() as tmp:
            report = scene_rules.apply(self.MANIFEST, matched, {"shots": [SHOT, other]}, Path(tmp),
                                       locate=locate)
        return report["rows"][0], matched["manifest"]["segments"]

    def test_too_short_group_borrows_the_next_sentence(self):
        row, segments = self._borrow(3.6, 1.0)
        self.assertEqual((row["indices"], row.get("borrowed"), row["status"]),
                         ([1, 2], [2], "partial"))
        self.assertEqual({s["video"] for s in segments}, {"D:/m/absorb.mp4"})

    def test_borrowing_that_still_fails_keeps_the_neighbour(self):
        row, segments = self._borrow(3.6, 0.2)
        self.assertEqual((row["indices"], row["status"]), ([1], "visual_missing"))
        self.assertNotIn("borrowed", row)
        self.assertTrue(segments[0].get("visual_missing"))
        self.assertEqual(segments[1]["video"], "D:/m/other.mp4")

    def test_replayed_interval_is_still_a_duplicate(self):
        segments = self._planned()
        second = segments[1]
        first = segments[0]
        replay = first["span_parts"] if first.get("multi_shot") else None
        if replay:
            second.update({"multi_shot": True, "span_parts": [dict(p) for p in replay]})
        else:
            second["window_solution"] = dict(first["window_solution"])
        with tempfile.TemporaryDirectory() as tmp:
            eng = self._engine(segments, tmp)
            gate = eng._visual_duplicate_gate(eng.build_clips())
        self.assertEqual(gate["status"], "DUPLICATE_VISUAL_SHOT")

    def test_unmet_scene_rule_is_uncertified(self):
        matched = SceneApplyTests()._matched("吸水实验，倒下去瞬吸", int(0.5 * US))
        locate = mock.Mock(return_value={"ok": True, "complete": True, "phases": PHASES})
        with tempfile.TemporaryDirectory() as tmp:
            scene_rules.apply({**self.MANIFEST, "delivery_mode": "formal"}, matched,
                              {"shots": [SHOT]}, Path(tmp), locate=locate)
            eng = self._engine(matched["manifest"]["segments"], tmp)
            eng.preview_uncertified_reasons = []
            eng.build_clips()
            eng._report_unmet_scene_rules()
        self.assertEqual(len(eng.preview_uncertified_reasons), 1)
        self.assertIn("SCENE_ACTION_TOO_LONG", eng.preview_uncertified_reasons[0])


class PostDraftQcMappingTests(unittest.TestCase):
    """后验 QC：一句拆成多段物理画面时，按句子收齐它的全部片段再验。"""

    def _run(self, claims, physical, *, verdict="DIRECT"):
        from orchestrator import draft_visual_qc as qc, vision_analyzer
        with tempfile.TemporaryDirectory() as tmp:
            td = Path(tmp)
            draft, report = td / "draft", td / "report"
            draft.mkdir()
            report.mkdir()
            materials, segments, cursor = [], [], 0
            for i, material in enumerate(physical):
                media = td / f"{material}.mp4"
                media.touch()
                if material not in [m["id"] for m in materials]:
                    materials.append({"id": material, "path": str(media)})
                segments.append({"material_id": material,
                                 "source_timerange": {"start": i * US, "duration": US},
                                 "target_timerange": {"start": cursor, "duration": US}})
                cursor += US
            (draft / "draft_info.json").write_text(json.dumps({
                "duration": cursor, "materials": {"videos": materials},
                "tracks": [{"name": "video", "segments": segments}]}), encoding="utf-8")
            (report / "shot_match_report.json").write_text(
                json.dumps({"segments": claims}, ensure_ascii=False), encoding="utf-8")
            prompts = []

            def model(*args, **_kw):
                prompts.append(args[2])
                count = args[2].count("- S")
                return True, json.dumps({"response_version": qc.RESPONSE_VERSION, "segments": [
                    {"index": i + 1, "match_level": verdict, "visual_ok": True, "confidence": 1.0,
                     "observed": "合成测试帧中的完整动作", "reason": "合成观察与测试句匹配"}
                    for i in range(count)], "overall": "PASS"})

            argv = ["qc", "--draft", str(draft), "--report-dir", str(report),
                    "--output", str(td / "qc.json")]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(qc, "_ffmpeg", return_value="fake"), \
                    mock.patch.object(qc, "_extract", return_value=(True, "")), \
                    mock.patch.object(vision_analyzer, "_write_sheet", return_value=True), \
                    mock.patch.object(qc, "_call_qc_model", side_effect=model):
                qc.main()
            return json.loads((td / "qc.json").read_text(encoding="utf-8")), prompts

    SPLIT = {"text": "吸水实验，倒下去瞬吸", "temporary_shot_id": "absorb", "multi_shot": True,
             "span_parts": [{"shot_id": "absorb#pour"}, {"shot_id": "absorb#absorb"},
                            {"shot_id": "absorb#wipe"}]}
    PACK = {"text": "包装设计漂亮", "temporary_shot_id": "pack"}

    def test_split_action_maps_every_part(self):
        data, prompts = self._run([self.SPLIT, self.PACK], ["A", "A", "A", "B"])
        self.assertEqual(data["status"], "PASS", data["issues"])
        self.assertEqual(data["segment_map"], [{"claim_index": 1, "draft_segments": [1, 2, 3]},
                                               {"claim_index": 2, "draft_segments": [4]}])
        self.assertIn("parts=3", prompts[0])
        self.assertEqual(data["checks"][1]["count"], 6)

    def test_missing_part_is_still_a_count_mismatch(self):
        data, _ = self._run([self.SPLIT, self.PACK], ["A", "A", "B"])
        self.assertEqual(data["status"], "FAIL")
        self.assertTrue(any(i.startswith("SEGMENT_COUNT_MISMATCH") for i in data["issues"]))
        self.assertEqual(data["segment_map"], [])

    def test_unmet_scene_rule_blocks_shipping(self):
        unmet = {**self.PACK, "text": "吸水实验，倒下去瞬吸",
                 "scene_rule": {"id": "absorb_test", "status": "unmet",
                                "reason": "SCENE_ACTION_TOO_LONG"}}
        data, _ = self._run([unmet], ["A"])
        self.assertNotEqual(data["release_eligibility"], "SHIP_ALLOWED")
        self.assertIn("SCENE_RULE_UNMET:S01:absorb_test:SCENE_ACTION_TOO_LONG",
                      data["preview_only_reasons"])
        unverified = {**unmet, "scene_rule": {**unmet["scene_rule"], "reason": "SCENE_PHASE_UNVERIFIED"}}
        data, _ = self._run([unverified], ["A"])
        self.assertEqual(data["status"], "UNCERTIFIED")
        partial = {**unmet, "scene_rule": {**unmet["scene_rule"], "status": "partial",
                                           "reason": "SCENE_PHASE_MISSING:wipe"}}
        data, _ = self._run([partial], ["A"])
        self.assertEqual(data["status"], "UNCERTIFIED")
        self.assertIn("SCENE_RULE_UNMET:S01:absorb_test:SCENE_PHASE_MISSING:wipe", data["warnings"])


if __name__ == "__main__":
    unittest.main()
