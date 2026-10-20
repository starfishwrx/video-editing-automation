"""1.5.5 coverage budget: one gap supplement, then zero on the same protocol."""
from __future__ import annotations

import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import coverage_gap, indexed_planner
from orchestrator.material_store import MaterialStore
from test_indexed_pipeline import IndexedPipelineTests


SEMANTIC = {"拆封", "抽取", "吸水", "透光", "擦拭", "包装", "相反语义"}


class CoverageEvalTests(unittest.TestCase):
    def test_thirty_cases_and_head_miss_keep_boundary_and_request_budget(self):
        semantic = [case for case in coverage_gap.EVAL_CASES if case["category"] in SEMANTIC]
        head = [case for case in coverage_gap.EVAL_CASES if case["id"] == "head-miss-141s"]
        self.assertEqual(len(semantic), 30)
        self.assertEqual(set(case["category"] for case in semantic), SEMANTIC)
        self.assertEqual(len(head), 1)
        prior: set[str] = set()
        reports = [coverage_gap.evaluate_case(case, prior_signatures=prior) for case in coverage_gap.EVAL_CASES]
        by_id = {row["id"]: row for row in reports}
        for case, row in zip(coverage_gap.EVAL_CASES, reports):
            self.assertLessEqual(row["remote_requests"], 1, case["id"])
            self.assertEqual(row["remote_requests"], case["expect_remote"], case["id"])
            self.assertEqual(row["segment_complete"], case["expect_complete"], case["id"])
            self.assertLessEqual(row["sample_count"], coverage_gap.MAX_GAP_FRAMES, case["id"])
            self.assertTrue(row["samples_inside_gap"], case["id"])
            if case["kind"] == "positive" and case.get("reference"):
                self.assertIsNotNone(row["boundary"], case["id"])
                self.assertTrue(row["boundary"]["within_0_5s"], (case["id"], row["boundary"]))
            if case["kind"] == "negative":
                self.assertNotEqual(row["evidence_role"], "direct_evidence", case["id"])
        head_row = by_id["head-miss-141s"]
        self.assertEqual(head_row["remote_requests"], 1)
        self.assertTrue(head_row["sample_us"])
        self.assertTrue(all(stamp < 8_820_000 for stamp in head_row["sample_us"]))
        self.assertEqual(head_row["intervals_us"], [(0, 8_820_000)])
        again = coverage_gap.evaluate_case(head[0], prior_signatures=prior)
        self.assertEqual(again["remote_requests"], 0)
        self.assertEqual(coverage_gap.gap_signature("same", [(0, 10)]),
                         coverage_gap.gap_signature("same", [(0, 10)]))

    def test_gap_ffmpeg_command_decodes_only_the_uncovered_span(self):
        command = coverage_gap.gap_ffmpeg_command(
            "ffmpeg", "clip.mp4", 0, 8_820_000, 12, "gap_%03d.jpg")
        self.assertEqual(command.count("-i"), 1)
        self.assertEqual(command[command.index("-ss") + 1], "0.000000")
        self.assertEqual(command[command.index("-t") + 1], "8.820000")
        self.assertEqual(command[command.index("-frames:v") + 1], "12")
        self.assertNotIn("141", command[command.index("-t") + 1])

    def test_extract_gap_frames_invokes_ffmpeg_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "clip.mp4"
            video.write_bytes(b"synthetic")
            created = []

            def fake_run(command, **kwargs):
                pattern = Path(command[-1])
                for index in range(1, 13):
                    path = pattern.with_name(f"gap_{index:03d}.jpg")
                    Image.new("RGB", (8, 8), "white").save(path)
                    created.append(path)
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(coverage_gap.subprocess, "run", side_effect=fake_run) as run:
                result = coverage_gap.extract_gap_frames(video, [(0, 8_820_000)], root / "out")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(result["decode_commands"], 1)
            self.assertLessEqual(len(result["frames"]), 12)
            self.assertTrue(all(frame["timestamp_us"] < 8_820_000 for frame in result["frames"]))


class ReviewPlaybackTests(unittest.TestCase):
    def test_review_page_shows_source_time_and_playback_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "source.mp4"
            video.write_bytes(b"synthetic")
            preview = root / "actual_slice.mp4"
            preview.write_bytes(b"slice")
            plan = {"segments": [{"index": 1, "text": "从片头倒液体", "source_start_us": 200_000,
                                  "source_end_us": 1_900_000, "timeline_start_us": 0, "speed": 1.25,
                                  "video": str(video), "video_sha256": "abc", "reason": "缺口补帧后采用",
                                  "action_phases": [{"phase": "execute", "start_us": 200_000,
                                                     "end_us": 1_200_000, "action": "pour",
                                                     "evidence_role": "direct_evidence"}]}],
                    "groups": [{"id": "g1", "members": [1]}]}
            observer = {"groups": [{"id": "g1", "preview_file": str(preview), "preview_sha256": "ab",
                                    "observation": "看见倒液体", "visible_at_speed": True,
                                    "playback": {"action_truncated": False, "visible_at_speed": True,
                                                 "text_visible_during_narration": False}}],
                        "observations": [{"index": 1, "observation": "片头有液体"}]}
            page = indexed_planner._review_html(root, {}, plan, observer, [])
            text = page.read_text(encoding="utf-8")
            self.assertIn("原素材", text)
            self.assertIn("成片", text)
            self.assertIn("动作是否被截断：否", text)
            self.assertIn("变速后是否仍能看清：是", text)
            self.assertIn("关键文字是否在对应口播期间可见：否", text)
            self.assertIn("direct_evidence", text)


class HeadMissPipelineTests(IndexedPipelineTests):
    def test_head_miss_uses_one_supplement_and_rerun_understands_zero(self):
        self.probe_mock.return_value = 141.13
        store = MaterialStore(self.manifest["material_index_dir"])
        rows = store.ingest_sources([self.video], self.manifest["product_id"])
        material_id = rows[0]["material_id"]
        image = self.root / "late.jpg"
        Image.new("RGB", (48, 80), "red").save(image)
        frame = store.save_frame(material_id, 8_820_000, image, protocol="legacy-representative-v1")
        store.save_observation(
            material_id,
            {"description": "第一张证据帧在 8.82 秒，片头倒液体没有进入观察。", "usable": True,
             "action_segments": [{"start_us": 0, "end_us": 2_000_000, "action": "pour",
                                  "result": "liquid on sheet"}],
             "action_complete": True},
            product_id=self.manifest["product_id"], start_us=0, end_us=141_130_000, state="observed",
            verified_requirements=["wipe-start-to-clean-result"],
            provenance={"kind": "candidate-vision", "evidence": [frame["frame_id"]],
                        "evaluated_requirements": ["wipe-start-to-clean-result"]})

        def answering(request):
            self.calls.append(copy.deepcopy(request))
            if request["stage"] == "coverage_supplement":
                times = [int(image["timestamp_us"]) for image in request["images"]]
                self.assertTrue(times)
                self.assertLessEqual(len(times), 12)
                self.assertTrue(all(0 <= stamp < 8_820_000 for stamp in times))
                return {"description": "片头把液体倒到巾体上。", "action_phases": [
                    {"phase": "execute", "start_us": 200_000, "end_us": 1_200_000, "action": "pour",
                     "object": "liquid", "complete": True, "evidence_role": "direct_evidence"},
                    {"phase": "result", "start_us": 1_200_000, "end_us": 1_800_000, "action": "pour",
                     "object": "sheet", "result": "liquid on sheet", "complete": True,
                     "evidence_role": "direct_evidence"}]}
            return self.model(request)

        cold = self.run_pipeline(model=answering)
        self.assertTrue(cold["ok"], cold.get("issues"))
        self.assertEqual(cold["index_stats"]["coverage_supplement_calls"], 1)
        self.assertEqual(cold["index_stats"]["material_understanding_calls"], 1)
        self.assertEqual(cold["index_stats"]["coverage_supplement_calls_by_material"][material_id], 1)
        self.assertFalse(any(call["stage"] == "material_observation" for call in self.calls))
        self.manifest["selection_policy"] = {"prefer_continuous_action": True}
        hot = self.run_pipeline(model=answering, report="policy-rerun")
        self.assertTrue(hot["ok"], hot.get("issues"))
        self.assertEqual(hot["index_stats"]["material_understanding_calls"], 0)
        self.assertEqual(hot["index_stats"]["coverage_supplement_calls"], 0)
        page = Path(self.root / "policy-rerun" / "indexed" / "review.html")
        if page.is_file():
            text = page.read_text(encoding="utf-8")
            self.assertIn("动作是否被截断", text)
            self.assertIn("原素材", text)


if __name__ == "__main__":
    unittest.main()
