"""v1.5.2 r18：r17 审查的三处图文错配 + 跨任务镜头去重。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import (  # noqa: E402
    demo_actions as da,
    rulepack,
    scene_rules,
    shot_analyzer,
    timing_contract as tc,
    usage_ledger,
)

US = tc.US
PRODUCT = "纯水湿厕纸"


def _contract(text: str) -> dict:
    return da.sentence_contract(text, category="纸品", product=PRODUCT)


def _shot(shot_id: str, **fields) -> dict:
    row = {"shot_id": shot_id, "video": f"D:/m/{shot_id}.mp4", "source_start": 0.0,
           "source_end": 12.0, "duration": 12.0, "material_duration_us": 12 * US,
           "status": "ready_for_matching", "action_complete": True, "has_subject": True,
           "role": "usage_demo", "evidence_strength": "B", "action_phase": "perform",
           "head_waste": 0.0, "tail_waste": 0.0,
           "evidence_intervals": [{"start": 4.0, "end": 6.0, "frame_time": 5.0}]}
    row.update(fields)
    return row


def _manifest(text: str, audio_s: float = 2.0, **extra) -> dict:
    manifest = {"segments": [{"text": text, "audio_duration_us": int(audio_s * US),
                              "timing_certainty": "audio"}],
                "category": "纸品", "product_name": PRODUCT, "product": PRODUCT,
                "head_trim_s": 0.0, "tail_trim_s": 0.0, "fps": 30, "max_source_reuse": 2,
                "delivery_mode": "preview"}
    manifest.update(extra)
    return rulepack.apply_manifest_defaults(manifest)


class ContradictionTests(unittest.TestCase):
    def test_no_drip_sentence_rejects_a_squeeze_shot(self):
        squeeze = {"description": "双手把湿巾里的水挤进碗里，水滴落下", "action": "挤压",
                   "result": "挤出水", "readable_claims": ["70PCS"]}
        contract = _contract("湿度刚刚好不滴水")
        self.assertEqual(da.shot_fit(squeeze, contract), 0)
        self.assertTrue(da.contradiction(squeeze, contract).startswith("moisture"))
        dry = {"description": "手捏湿巾提起，没有水滴落下", "action": "提起", "result": "湿润不滴水"}
        self.assertEqual(da.contradiction(dry, contract), "")

    def test_negated_label_needs_the_same_direction(self):
        contract = _contract("无酒精")
        self.assertEqual(da.shot_fit({"readable_claims": ["含酒精"]}, contract), 0)
        self.assertTrue(da.contradiction({"readable_claims": ["含酒精"]}, contract))
        self.assertEqual(da.shot_fit({"readable_claims": ["无酒精"]}, contract), 2)
        self.assertEqual(da.shot_fit({"readable_claims": ["0酒精"]}, contract), 2)

    def test_sheet_count_needs_the_same_number(self):
        contract = _contract("70抽大容量")
        self.assertEqual(da.shot_fit({"readable_claims": ["170抽"]}, contract), 0)
        self.assertTrue(da.contradiction({"readable_claims": ["170抽"]}, contract))
        self.assertEqual(da.shot_fit({"readable_claims": ["70PCS", "10级净化"]}, contract), 2)
        bundle = _contract("一提3包")
        self.assertEqual(da.contradiction({"readable_claims": ["80抽"]}, bundle), "")

    def test_no_excess_water_rejects_a_dripping_shot(self):
        drip = {"action": "从袋口抽出湿厕纸", "result": "抽出一张湿厕纸，有水滴落入下方玻璃碗"}
        self.assertEqual(da.shot_fit(drip, _contract("没有多余水分")), 0)
        self.assertEqual(da.contradiction(drip, _contract("湿度刚刚好")), "")

    def test_pull_and_tough_contradictions(self):
        pull = _contract("单手一抽一张不连张")
        self.assertEqual(da.shot_fit({"action": "抽出", "result": "连着抽出两张"}, pull), 0)
        self.assertEqual(da.shot_fit({"action": "抽出", "result": "只抽出一张，未见连张"}, pull), 1)
        tough = _contract("纸张厚实不容易破")
        self.assertEqual(da.shot_fit({"action": "拉伸", "result": "巾体撕裂"}, tough), 0)
        self.assertEqual(da.shot_fit({"action": "拉伸", "result": "巾体未见破洞"}, tough), 1)

    def test_high_ai_score_cannot_rescue_a_contradicted_shot(self):
        text = "湿度刚刚好不滴水"
        squeeze = _shot("sq", action="挤压", object="湿巾", result="挤出水",
                        description="双手把湿巾里的水挤进碗里，水滴落下",
                        visual_tags=["挤压"], evidence_tags=["挤出水"])
        analysis = {"shots": [squeeze], "semantic_matches": [{"index": 1, "top3": [
            {"shot_id": "sq", "score": 95, "matched_claims": ["湿巾湿度展示"]}]}]}
        res = shot_analyzer.match_manifest(_manifest(text), analysis)
        row = res["recommendations"][0]["top3"][0]
        self.assertFalse(row["eligible"])
        self.assertIn("CONTRADICTED", row["reuse_block_reason"])
        seg = {"selection_mode": "auto_top1_with_reuse_guard"}
        shot_analyzer.annotate_sentence_fit(seg, _contract(text), squeeze)
        self.assertEqual(seg["sentence_fit"]["fit"], "off_topic")
        self.assertTrue(seg["sentence_fit"]["contradiction"])


class IncompleteActionTests(unittest.TestCase):
    def test_unfinished_segment_does_not_certify_completion(self):
        self.assertFalse(da_segment({"action": "抽取进行中", "result": None}))
        self.assertFalse(da_segment({"action": "抽取", "result": "动作尚未完成"}))
        self.assertTrue(da_segment({"action": "单手抽出一张", "result": "抽出一张"}))

    def test_scene_phases_do_not_invent_completion(self):
        scene = next(s for s in rulepack.load("zhipin")["scenes"] if s["id"] == "usage_action")
        contract = _contract("单手一抽一张不连张")
        shot = {"source_start": 0.0, "source_end": 9.4, "action_segments": [
            {"start": 6.0, "end": 7.8, "action": "抽取进行中", "result": None}]}
        self.assertIsNone(scene_rules._segment_phases(scene, shot, contract))


def da_segment(segment: dict) -> bool:
    from orchestrator.vision_analyzer import segment_completed
    return segment_completed(segment)


class GrazingWindowTests(unittest.TestCase):
    SHOT = _shot("s1", action="挂起", visual_tags=["挂起", "抽出"], evidence_tags=["挂起", "抽出"],
                 description="先挂起包装，再单手抽出一张湿巾", action_phase="static", setup_id="a",
                 action_segments=[
                     {"start": 0.0, "end": 5.8, "action": "挂起包装", "result": "挂墙"},
                     {"start": 5.9, "end": 8.0, "action": "单手抽出一张", "result": "抽出一张"}])

    def test_focus_moves_off_a_grazing_interval(self):
        focused = shot_analyzer.focus_on_topic(self.SHOT, _contract("单手一抽一张不连张"))
        interval = focused["evidence_intervals"][0]
        self.assertEqual((interval["start"], interval["end"]), (5.9, 8.0))
        self.assertEqual(interval["evidence_source"], "topic_segment")

    def test_focus_keeps_an_interval_centred_on_the_topic(self):
        shot = {**self.SHOT, "evidence_intervals": [{"start": 6.2, "end": 7.8, "frame_time": 7.0}]}
        self.assertIs(shot_analyzer.focus_on_topic(shot, _contract("单手一抽一张不连张")), shot)

    def test_selector_window_covers_the_pull_action(self):
        text = "单手一抽一张不连张"
        analysis = {"shots": [self.SHOT], "semantic_matches": [{"index": 1, "top3": [
            {"shot_id": "s1", "score": 90, "matched_claims": [self.SHOT["description"]]}]}]}
        matched = shot_analyzer.match_manifest(_manifest(text), analysis)
        window = matched["manifest"]["segments"][0]["window_solution"]
        self.assertTrue(window["ok"], window)
        self.assertGreaterEqual(window["source_start"], 5.85)
        self.assertGreaterEqual(window["source_end"], 7.95)


class PreviewNarrowTests(unittest.TestCase):
    SOFT = _shot("soft", role="direct_evidence", action="按压", object="卫生巾面层",
                 result="面层回弹柔软", description="手指按压卫生巾面层，展示柔软亲肤",
                 visual_tags=["面层", "柔软", "亲肤"], evidence_tags=["柔软亲肤"],
                 evidence_intervals=[{"start": 1.27, "end": 3.77, "frame_time": 1.272}])

    def _match(self, mode):
        analysis = {"shots": [self.SOFT], "semantic_matches": [{"index": 1, "top3": [
            {"shot_id": "soft", "score": 90, "matched_claims": ["柔软亲肤"]}]}]}
        manifest = _manifest("亲肤舒适", audio_s=1.0, category="weipin", product="萌睡裤",
                             product_name="萌睡裤", delivery_mode=mode)
        return shot_analyzer.match_manifest(manifest, analysis)

    def test_preview_narrows_long_evidence_instead_of_parking_at_zero(self):
        seg = self._match("preview")["manifest"]["segments"][0]
        self.assertTrue(seg["time_sensitive"])
        window = seg["window_solution"]
        self.assertTrue(window["ok"], window)
        self.assertLessEqual(window["source_start"], 1.272)
        self.assertGreaterEqual(window["source_end"], 1.272)
        self.assertTrue(seg["preview_unresolved"])
        self.assertIn("NARROWED_FOR_PREVIEW", seg["preview_reason"])

    def test_formal_still_reports_the_gap(self):
        matched = self._match("formal")
        self.assertIn("SHOT_TIMING_INFEASIBLE",
                      [p.get("type") for p in matched.get("pending_items") or []])


class CrossTaskLedgerTests(unittest.TestCase):
    def test_reservation_is_visible_then_released(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            seg = [{"video": r"D:\m\a.mp4", "source_start": 1.0}]
            mine = {"usage_ledger_path": path, "record_id": "rec1", "draft_name": "任务1-v1"}
            other = {"usage_ledger_path": path, "record_id": "rec2", "draft_name": "任务2-v1"}
            usage_ledger.record(mine, seg, reserved=True)
            key = usage_ledger.shot_key(r"D:\m\a.mp4", 1.0)
            self.assertEqual(usage_ledger.other_task_counts(other), {key: 1})
            self.assertNotIn("任务1-v1", json.loads(Path(path).read_text(encoding="utf-8")).get("drafts", {}))
            usage_ledger.release(mine)
            self.assertEqual(usage_ledger.other_task_counts(other), {})
            usage_ledger.record(mine, seg, reserved=True)
            usage_ledger.record(mine, seg)
            usage_ledger.release(mine)
            self.assertEqual(usage_ledger.other_task_counts(other), {key: 1})

    def test_stale_reservation_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.json"
            key = usage_ledger.shot_key(r"D:\m\a.mp4", 1.0)
            path.write_text(json.dumps({"shots": {key: [
                {"task": "rec1", "draft": "旧", "at": "2020-01-01T00:00:00", "reserved": True}]}}),
                encoding="utf-8")
            other = {"usage_ledger_path": str(path), "record_id": "rec2", "draft_name": "任务2-v1"}
            self.assertEqual(usage_ledger.other_task_counts(other), {})

    def _pick(self, text: str, used: str) -> str:
        shots = [_shot(sid, role="product_display", action="举起", object="湿厕纸包装",
                       action_phase="static", setup_id=f"setup_{sid}",
                       description="手拿一包湿厕纸举到镜头前展示", visual_tags=["包装展示"],
                       evidence_tags=["包装展示"], video=f"D:/m/{sid}.mp4")
                 for sid in ("a1", "b2")]
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            usage_ledger.record({"usage_ledger_path": path, "record_id": "rec9", "draft_name": "别的任务"},
                                [{"video": f"D:/m/{used}.mp4", "source_start": 0.0}])
            manifest = _manifest(text, usage_ledger_path=path, record_id="rec1", draft_name="本任务-v1")
            analysis = {"shots": shots, "semantic_matches": [{"index": 1, "top3": [
                {"shot_id": sid, "score": 80, "matched_claims": ["包装展示"]} for sid in ("a1", "b2")]}]}
            matched = shot_analyzer.match_manifest(manifest, analysis)
        return Path(matched["manifest"]["segments"][0]["video"]).stem

    def test_display_sentence_avoids_shots_other_tasks_used(self):
        self.assertEqual(self._pick("今天给大家推荐一款宝藏好物", used="a1"), "b2")
        self.assertEqual(self._pick("今天给大家推荐一款宝藏好物", used="b2"), "a1")

    def test_scene_swap_prefers_shots_other_tasks_used_less(self):
        shots = {sid: _shot(sid, role="product_display", description="手拿一包湿厕纸举到镜头前展示")
                 for sid in ("cur", "a1", "b2")}
        rec = {"top3": [{"shot_id": "a1", "eligible": True}, {"shot_id": "b2", "eligible": True}]}
        seg = {"temporary_shot_id": "cur"}
        others = {usage_ledger.shot_key("D:/m/a1.mp4", 0.0): 3}
        display = _contract("今天给大家推荐一款宝藏好物")
        picked = scene_rules._candidates(seg, rec, shots, set(), display, others)
        self.assertEqual([s["shot_id"] for s, _ in picked], ["cur", "b2", "a1"])
        demo = _contract("单手一抽一张不连张")
        self.assertEqual(scene_rules._other_uses(shots["a1"], demo, others), 0)

    def test_demo_contract_detection(self):
        self.assertTrue(da.is_demo_contract(_contract("单手一抽一张不连张")))
        self.assertFalse(da.is_demo_contract(_contract("EDI纯水配方")))
        self.assertFalse(da.is_demo_contract(_contract("今天给大家推荐一款宝藏好物")))


if __name__ == "__main__":
    unittest.main()
