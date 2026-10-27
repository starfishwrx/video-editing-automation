"""Stage boundaries and durable completion under synthetic, offline concurrency."""
import contextlib
import json
import os
import re
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import vision_analyzer as vision


class VisionStageConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.report = self.root / "report"
        self.video = self.root / "synthetic.mp4"
        self.video.write_bytes(b"synthetic")
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "JY_SLOT_DIR": str(self.root / "slots"),
            "JY_VISION_CACHE_DIR": str(self.root / "cache"),
            "JY_VISION_TRANSPORT": "volc_glm", "JY_VISION_BATCH_WORKERS": "20",
            "JY_VISION_PREP_WORKERS": "2", "JY_VISION_PREP_SLOTS": "2",
            "JY_VISION_SHEET_TILES": "2", "JY_VISION_TEXT_PASS": "0",
            "JY_VISION_TEXT_WORKERS": "20", "JY_VISION_TEXT_READS": "2",
        }))
        self.stack.enter_context(mock.patch.object(vision, "_orientation", return_value="landscape"))
        self.frames = [{"path": str(self.root / f"frame{i}.jpg"), "timestamp": i * 0.5,
                        "suffix": f"t{i}"} for i in (1, 2)]
        for frame in self.frames:
            Path(frame["path"]).write_bytes(b"synthetic")
        self.stack.enter_context(mock.patch.object(vision, "_sample_frames", return_value=self.frames))
        self.manifest = {"product": "synthetic product"}

    def shots(self, count):
        return [{"shot_id": f"s{i:02d}", "video": str(self.video), "source_start": 0,
                 "source_end": 2, "frame_exists": True, "status": "pending_analysis"}
                for i in range(count)]

    def answer(self, index, *, text=False):
        return True, json.dumps({"shots": [{"shot_id": f"s{index:02d}",
            "description": "synthetic observation", "visual_tags": ["product"],
            "evidence_tags": ["visible product"], "action_complete": True,
            "confidence": 0.8, "evidence_time": 0.5,
            "text_tiles": [1] if text else []}]})

    def test_all_media_prepared_before_twenty_calls_and_fast_results_checkpoint(self):
        prepared = set()
        lock = threading.Lock()
        api_barrier = threading.Barrier(20)
        checkpointed = threading.Event()
        original_persist = vision._persist
        state = {"active": 0, "peak": 0}

        def sheet(items, output, **kwargs):
            with lock:
                prepared.add(output.name)
            return True

        def persist(report, analysis):
            original_persist(report, analysis)
            # A fast response must become durable while batch 1 is still in flight.
            if not checkpointed.is_set() and analysis["shots"][1].get("status") == "ready_for_matching":
                disk = json.loads((report / "shot_candidates.json").read_text(encoding="utf-8"))
                self.assertFalse(disk["ok"])
                checkpointed.set()

        def api(sheet, prompt, output, **kwargs):
            self.assertEqual(len(prepared), 20)
            progress = json.loads((self.report / "vision_progress.json").read_text(encoding="utf-8"))
            self.assertEqual(progress["batch_workers"], 20)
            index = int(re.search(r"shot_id=s(\d+)", prompt).group(1))
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                api_barrier.wait(timeout=10)
                if index == 0:
                    self.assertTrue(checkpointed.wait(10), "fast result was trapped behind slow batch")
                return self.answer(index)
            finally:
                with lock:
                    state["active"] -= 1

        with mock.patch.object(vision, "_write_sheet", side_effect=sheet), \
                mock.patch.object(vision, "_run_vision_with_retry", side_effect=api), \
                mock.patch.object(vision, "_persist", side_effect=persist):
            result = vision.enrich_with_vision(self.manifest, {"shots": self.shots(20)}, self.report)
        self.assertEqual(state["peak"], 20)
        self.assertTrue(result["ok"])

    def test_media_slots_limit_preparation_without_limiting_api_workers(self):
        state = {"active": 0, "peak": 0}
        barrier = threading.Barrier(2)
        lock = threading.Lock()

        def prepare(index):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            barrier.wait(timeout=10)
            with lock:
                state["active"] -= 1
            return index, index

        with mock.patch.dict(os.environ, {"JY_VISION_PREP_WORKERS": "4", "JY_VISION_PREP_SLOTS": "2"}):
            self.assertEqual(len(vision._prepare_indexed(list(range(4)), prepare)), 4)
        self.assertEqual(state["peak"], 2)
        self.assertEqual(vision._batch_workers(20), 20)

    def test_text_sheets_finish_before_reads_and_share_task_api_ceiling(self):
        prepared = set()
        lock = threading.Lock()
        state = {"active": 0, "peak": 0, "reads": 0}

        def sheet(items, output, **kwargs):
            with lock:
                prepared.add(output.name)
            return True

        def api(sheet, prompt, output, **kwargs):
            if not sheet.name.startswith("text_"):
                index = int(re.search(r"shot_id=s(\d+)", prompt).group(1))
                return self.answer(index, text=True)
            self.assertEqual(sum(name.startswith("text_") for name in prepared), 6)
            with lock:
                state["active"] += 1
                state["reads"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.02)
            with lock:
                state["active"] -= 1
            return True, json.dumps({"readable_claims": ["verified"], "pointed_text": []})

        with mock.patch.dict(os.environ, {"JY_VISION_TEXT_PASS": "1", "JY_VISION_BATCH_WORKERS": "3"}), \
                mock.patch.object(vision, "_write_sheet", side_effect=sheet), \
                mock.patch.object(vision, "_run_vision_with_retry", side_effect=api):
            result = vision.enrich_with_vision(self.manifest, {"shots": self.shots(6)}, self.report)
        self.assertGreater(state["peak"], 1)
        self.assertLessEqual(state["peak"], 3)
        self.assertEqual(state["reads"], 12)
        self.assertTrue(all(row["text_pass"] == "hires" for row in result["shots"]))

    def test_failed_sheet_never_calls_api_or_certifies_the_shot(self):
        with mock.patch.object(vision, "_write_sheet", return_value=False), \
                mock.patch.object(vision, "_run_vision_with_retry") as api:
            result = vision.enrich_with_vision(self.manifest, {"shots": self.shots(1)}, self.report)
        api.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertEqual(result["shots"][0]["status"], "pending_analysis")
        self.assertEqual(result["vision_analysis"]["errors"][0]["type"], "VISION_SHEET_FAILED")

    def test_cross_batch_response_cannot_modify_another_shot(self):
        def api(sheet, prompt, *args, **kwargs):
            return self.answer(1 if "shot_id=s00" in prompt else 0)

        with mock.patch.object(vision, "_write_sheet", return_value=True), \
                mock.patch.object(vision, "_run_vision_with_retry", side_effect=api):
            result = vision.enrich_with_vision(self.manifest, {"shots": self.shots(2)}, self.report)
        self.assertFalse(result["ok"])
        self.assertTrue(all(row["status"] == "pending_analysis" for row in result["shots"]))


if __name__ == "__main__":
    unittest.main()
