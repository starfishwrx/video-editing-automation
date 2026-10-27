from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import vision_analyzer  # noqa: E402

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover - Pillow ships with the Windows runtime
    Image = None


def _portrait(path: Path, color: tuple[int, int, int]) -> str:
    Image.new("RGB", (108, 192), color).save(path)
    return str(path)


class SampleTimesTest(unittest.TestCase):
    def test_long_take_gets_frames_across_the_whole_shot(self):
        times = vision_analyzer.sample_times(0.0, 27.8)
        self.assertEqual(len(times), vision_analyzer.SAMPLE_MAX + 1)
        self.assertLess(times[0], 2.0)
        self.assertAlmostEqual(times[-1], 27.65, places=2)
        gaps = [b - a for a, b in zip(times, times[1:-1])]
        self.assertTrue(all(gap < 3.6 for gap in gaps), gaps)

    def test_medium_take_covers_each_second_and_a_half(self):
        times = vision_analyzer.sample_times(0.0, 9.4)
        self.assertEqual(len(times), 8)
        self.assertTrue(any(1.2 <= t <= 4.0 for t in times))
        self.assertTrue(any(6.0 <= t <= 7.8 for t in times))
        self.assertTrue(all(0.0 <= t <= 9.4 for t in times))

    def test_short_shot_keeps_at_least_two_frames(self):
        times = vision_analyzer.sample_times(3.0, 3.6)
        self.assertGreaterEqual(len(times), 2)
        self.assertTrue(all(3.0 <= t <= 3.6 for t in times))


class AgreedTextTest(unittest.TestCase):
    def test_only_phrases_both_reads_share_survive(self):
        reads = [["70PCS XL码", "0致敏 0刺激 0荧光", "加强", "EDI纯水"],
                 ["70PCS", "XL码", "O致敏·O刺激·O荧光", "加厚", "EDI 纯水"]]
        self.assertEqual(vision_analyzer.agreed_text(reads),
                         ["70PCS", "XL码", "0致敏", "0刺激", "0荧光", "EDI纯水"])

    def test_text_seen_in_one_take_only_is_not_evidence(self):
        shots = [{"text_agreed": ["EDI纯水", "0增白剂"], "pointed_agreed": ["0增白剂"]},
                 {"text_agreed": ["EDI 纯水", "不连抽"], "pointed_agreed": []},
                 {"text_agreed": ["不连抽"]},
                 {"readable_claims": ["untouched"]}]
        with mock.patch.dict(os.environ, {"JY_VISION_TEXT_MIN_SHOTS": "2"}):
            vision_analyzer.apply_cross_shot_text(shots)
        self.assertEqual(shots[0]["readable_claims"], ["EDI纯水"])
        self.assertEqual(shots[0]["pointed_claims"], [])
        self.assertEqual(shots[1]["readable_claims"], ["EDI 纯水", "不连抽"])
        self.assertEqual(shots[3]["readable_claims"], ["untouched"])

    def test_single_read_is_kept_as_is(self):
        self.assertEqual(vision_analyzer.agreed_text([["不连抽", "不连抽"]]), ["不连抽"])
        self.assertEqual(vision_analyzer.agreed_text([]), [])


class PackBatchesTest(unittest.TestCase):
    def _group(self, shot_id: str, count: int, orientation: str) -> list[dict]:
        return [{"shot_id": shot_id, "orientation": orientation, "timestamp": float(i)}
                for i in range(count)]

    def test_shots_are_never_split_and_orientations_never_mix(self):
        groups = [self._group("a", 5, "portrait"), self._group("b", 5, "portrait"),
                  self._group("c", 4, "landscape"), self._group("d", 3, "portrait")]
        batches = vision_analyzer._pack_batches(groups, 12)
        for batch in batches:
            self.assertLessEqual(len(batch), 12)
            self.assertEqual(len({item["orientation"] for item in batch}), 1)
        located = {}
        for index, batch in enumerate(batches):
            for item in batch:
                located.setdefault(item["shot_id"], set()).add(index)
        self.assertTrue(all(len(where) == 1 for where in located.values()), located)


class PromptTest(unittest.TestCase):
    def test_prompt_carries_category_rules_and_frame_times(self):
        items = [{"shot_id": "s1", "timestamp": 1.25, "source_start": 0.0, "source_end": 9.4},
                 {"shot_id": "s1", "timestamp": 6.5, "source_start": 0.0, "source_end": 9.4}]
        from orchestrator import rulepack
        rules = rulepack.prompt_rules({"category": "纸品"}, "analysis")
        self.assertTrue(any("挂钩" in rule for rule in rules))
        prompt = vision_analyzer._vision_prompt(items, 1, 1, rules)
        self.assertIn("挂钩", prompt)
        self.assertIn("1.25s", prompt)
        self.assertIn("action_segments", prompt)
        self.assertIn("text_tiles", prompt)
        self.assertNotIn("mid 与 end", prompt)


@unittest.skipIf(Image is None, "Pillow is required for contact sheets")
class SheetTest(unittest.TestCase):
    def test_fit_keeps_portrait_frames_unstretched(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            frame = _portrait(root / "f.jpg", (250, 250, 250))
            sheet = root / "sheet.jpg"
            ok = vision_analyzer._write_sheet(
                [{"path": frame, "label": "#1 0.5s"}], sheet, columns=1,
                tile_width=360, tile_height=360 + 34, fit=True, font_size=22)
            self.assertTrue(ok)
            image = Image.open(sheet).convert("RGB")
            self.assertEqual(image.size, (360, 394))
            # 108x192 scaled into 360x360 is 202 wide, so the left edge is padding.
            self.assertLess(sum(image.getpixel((10, 180))), 200)
            self.assertGreater(sum(image.getpixel((180, 180))), 600)


@unittest.skipIf(Image is None, "Pillow is required for contact sheets")
class EnrichV3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "IMG_3332.MOV"
        self.video.write_bytes(b"source")
        self.frames = [
            {"path": _portrait(self.root / f"f{i}.jpg", (40 * i, 80, 120)),
             "timestamp": t, "suffix": f"t{i}"}
            for i, t in enumerate((0.7, 2.0, 3.4, 4.7, 6.0, 7.4, 8.7), 1)]
        self.manifest = {"category": "纸品", "product": "纯水湿厕纸", "product_id": "p41"}
        self.env = mock.patch.dict(os.environ, {"JY_VISION_CACHE_DIR": str(self.root / "cache"),
                                                "JY_VISION_TEXT_PASS": "1",
                                                "JY_VISION_TEXT_MIN_SHOTS": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def _shot(self) -> dict:
        return {"shot_id": "source_001_shot_001", "video": str(self.video),
                "source_start": 0.0, "source_end": 9.4, "frame_exists": True,
                "status": "pending_analysis", "description": ""}

    def _first_pass(self, **overrides) -> str:
        row = {
            "shot_id": "source_001_shot_001",
            "description": "先把包装挂到挂钩上，再单手抽出一张，盒口留出下一张的头",
            "visual_tags": ["挂钩", "单手抽取"], "evidence_tags": ["挂到挂钩上", "单手抽出一张"],
            "action_complete": False, "head_waste": 0.0, "tail_waste": 0.1,
            "evidence_time": 7.4,
            "evidence_intervals": [{"start": 5.5, "end": 9.0}],
            "action_segments": [
                {"start": 1.2, "end": 4.0, "action": "挂到挂钩上", "result": "包装挂在墙上"},
                {"start": 6.0, "end": 7.8, "action": "单手抽出一张", "result": "盒口留出下一张的头"}],
            "text_tiles": [3],
            "confidence": 0.8, "action": "抽出", "object": "湿厕纸", "result": "留出下一张的头",
            "role": "usage_demo", "evidence_strength": "B", "action_phase": "perform",
            "setup_id": "wall_hook", "has_subject": True,
            "readable_claims": ["不蓬翘"], "pointing_to_text": False,
            "pointing_action": "", "gesture": "",
        }
        row.update(overrides)
        return json.dumps({"shots": [row]}, ensure_ascii=False)

    def _run(self, responses: list[tuple[bool, str]]):
        prompts: list[str] = []

        def fake(sheet, prompt, *_a, **_kw):
            prompts.append(prompt)
            return responses.pop(0)

        with mock.patch.object(vision_analyzer, "_sample_frames", return_value=self.frames), \
                mock.patch.object(vision_analyzer, "_run_vision_with_retry", side_effect=fake):
            result = vision_analyzer.enrich_with_vision(
                self.manifest, {"shots": [self._shot()]}, self.root / "task" / "report",
                profile="volc")
        return result, prompts

    def _cache(self) -> dict:
        path = self.root / "cache" / "material_vision.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def _text(self, claims: list[str]) -> tuple[bool, str]:
        return True, json.dumps({"shot_id": "source_001_shot_001", "readable_claims": claims,
                                 "pointed_text": []}, ensure_ascii=False)

    def test_located_demo_is_usable_with_a_short_window_and_hires_text(self):
        result, prompts = self._run([(True, self._first_pass()),
                                     self._text(["EDI纯水", "不连抽", "0酒精"]),
                                     self._text(["EDI纯水", "不连抽", "0致敏"])])
        row = result["shots"][0]
        self.assertEqual(row["status"], "ready_for_matching")
        self.assertTrue(row["action_complete"])
        self.assertEqual(row["action_complete_source"], "evidence_interval")
        self.assertEqual([seg["action"] for seg in row["action_segments"]],
                         ["挂到挂钩上", "单手抽出一张"])
        interval = row["evidence_intervals"][0]
        self.assertLessEqual(interval["end"] - interval["start"],
                             vision_analyzer.EVIDENCE_MAX_S + 1e-6)
        self.assertTrue(interval["start"] <= 7.4 <= interval["end"])
        self.assertEqual(interval["frame_time"], 7.4)
        self.assertEqual(row["readable_claims"], ["EDI纯水", "不连抽"])
        self.assertEqual(row["text_pass"], "hires")
        self.assertIn("挂钩", prompts[0])
        self.assertIn("逐字抄录", prompts[1])
        self.assertEqual(len(prompts), 3)
        cached = next(iter(self._cache().values()))
        self.assertEqual(cached["readable_claims"], ["EDI纯水", "不连抽"])
        self.assertNotIn("frame_path", cached["evidence_intervals"][0])

    def test_model_false_with_unfinished_segment_stays_out(self):
        first = self._first_pass(
            description="手抽取一张湿巾，动作尚未完成，画面结束",
            result=None, readable_claims=[], pointing_to_text=False, text_tiles=[],
            action_segments=[{"start": 6.0, "end": 7.8, "action": "抽取进行中", "result": None}],
            evidence_intervals=[{"start": 6.0, "end": 7.8}], evidence_time=7.4)
        result, _ = self._run([(True, first)])
        row = result["shots"][0]
        self.assertEqual(row["status"], "pending_analysis")
        self.assertFalse(row["action_complete"])
        self.assertEqual(row["action_complete_source"], "model")

    def test_audited_frame_outside_the_finished_segment_stays_out(self):
        first = self._first_pass(readable_claims=[], text_tiles=[], evidence_time=2.0,
                                 evidence_intervals=[{"start": 1.2, "end": 3.0}],
                                 action_segments=[{"start": 6.0, "end": 7.8, "action": "单手抽出一张",
                                                   "result": "盒口留出下一张的头"}])
        result, _ = self._run([(True, first)])
        self.assertEqual(result["shots"][0]["status"], "pending_analysis")

    def test_failed_text_pass_keeps_visual_cache_but_cannot_certify(self):
        result, _ = self._run([(True, self._first_pass()), (False, "timed out")])
        row = result["shots"][0]
        self.assertEqual(row["status"], "ready_for_matching")
        self.assertEqual(row["readable_claims"], [])
        self.assertEqual(row["text_pass"], "failed")
        self.assertEqual(result["text_pass"]["failed"], 1)
        self.assertEqual(next(iter(self._cache().values()))["text_pass"], "failed")
        self.assertFalse(result["ok"])

    def test_display_only_shot_without_located_demo_stays_out(self):
        first = self._first_pass(evidence_intervals=[], action_segments=[], text_tiles=[],
                                 evidence_strength="C", role="usage_demo",
                                 readable_claims=[])
        result, prompts = self._run([(True, first)])
        row = result["shots"][0]
        self.assertFalse(row["action_complete"])
        self.assertEqual(row["status"], "pending_analysis")
        self.assertEqual(len(prompts), 1)

    def test_second_run_hits_the_v3_cache(self):
        self._run([(True, self._first_pass()), self._text(["EDI纯水"]), self._text(["EDI纯水"])])
        result, prompts = self._run([])
        self.assertEqual(prompts, [])
        self.assertEqual(result["vision_analysis"]["cache_hits"], 1)
        self.assertEqual(result["vision_analysis"]["analysis_version"], "product-visual-v3")
        self.assertEqual(result["shots"][0]["readable_claims"], ["EDI纯水"])

    def test_changed_category_rules_reanalyze_cached_shots(self):
        self._run([(True, self._first_pass()), self._text(["EDI纯水"]), self._text(["EDI纯水"])])
        with mock.patch.object(vision_analyzer, "_analysis_rules", return_value=["新增：看清是否散开"]), \
                mock.patch.object(vision_analyzer, "_keyed_rules", return_value=["新增：看清是否散开"]):
            result, prompts = self._run([(True, self._first_pass()),
                                         self._text(["EDI纯水"]), self._text(["EDI纯水"])])
        self.assertEqual(result["vision_analysis"]["cache_hits"], 0)
        self.assertIn("新增：看清是否散开", prompts[0])

    def _with_recheck(self, words: list[str]):
        rule = "新增：挂钩要写清挂的是哪种"
        return (mock.patch.object(vision_analyzer, "_analysis_rules", return_value=[rule]),
                mock.patch.object(vision_analyzer, "_rule_checks", return_value={"r1": words}))

    def test_recheck_rule_keeps_cache_for_unrelated_shots(self):
        self._run([(True, self._first_pass()), self._text(["EDI纯水"]), self._text(["EDI纯水"])])
        rules, checks = self._with_recheck(["对光"])
        with rules, checks:
            result, prompts = self._run([])
        self.assertEqual(prompts, [])
        self.assertEqual(result["vision_analysis"]["cache_hits"], 1)

    def test_recheck_rule_reanalyzes_only_matching_shots_once(self):
        self._run([(True, self._first_pass()), self._text(["EDI纯水"]), self._text(["EDI纯水"])])
        rules, checks = self._with_recheck(["挂钩"])
        with rules, checks, mock.patch.dict(os.environ, {"JY_VISION_RECHECK": "vision"}):
            result, prompts = self._run([(True, self._first_pass()),
                                         self._text(["EDI纯水"]), self._text(["EDI纯水"])])
            self.assertEqual(result["vision_analysis"]["rule_rechecked"], 1)
            self.assertIn("新增：挂钩要写清挂的是哪种", prompts[0])
            again, prompts = self._run([])
        self.assertEqual(prompts, [])
        self.assertEqual(again["shots"][0]["rule_checks"], ["r1"])

    def test_recheck_reuses_ready_observation_by_default(self):
        self._run([(True, self._first_pass()), self._text(["EDI纯水"]), self._text(["EDI纯水"])])
        rules, checks = self._with_recheck(["挂钩"])
        with rules, checks, mock.patch.dict(os.environ, {"JY_VISION_RECHECK": "reuse"}):
            result, prompts = self._run([])
        self.assertEqual(prompts, [])
        self.assertEqual(result["vision_analysis"]["rule_rechecked"], 0)
        self.assertEqual(result["vision_analysis"]["rule_recheck_reused"], 1)
        self.assertEqual(result["vision_analysis"]["cache_hits"], 1)
        self.assertEqual(result["shots"][0]["rule_checks"], ["r1"])

    def test_observations_made_under_every_rule_are_adopted(self):
        rule = "新增：挂钩要写清挂的是哪种"
        with mock.patch.object(vision_analyzer, "_analysis_rules", return_value=[rule]), \
                mock.patch.object(vision_analyzer, "_keyed_rules", return_value=[rule]):
            self._run([(True, self._first_pass()), self._text(["EDI纯水"]), self._text(["EDI纯水"])])
        rules, checks = self._with_recheck(["挂钩"])
        with rules, checks:
            result, prompts = self._run([])
        self.assertEqual(prompts, [])
        self.assertEqual(result["shots"][0]["rule_checks"], ["r1"])


if __name__ == "__main__":
    unittest.main()
