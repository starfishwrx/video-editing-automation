"""Product observations and verified text are reusable across independent tasks."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
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
from orchestrator.file_safety import file_lock
from test_v152_resume_integrity import SHOT_ID, _first_pass, _text


class SharedProductVisionCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.work = self.root / "work"
        self.video = self.root / "same-material.mp4"
        self.video.write_bytes(b"stable-source")
        self.frame = self.root / "frame.jpg"
        self.frame.write_bytes(b"synthetic-frame")
        clean = {k: v for k, v in os.environ.items() if not k.startswith("JY_")}
        clean.update({"JY_VISION_TEXT_MIN_SHOTS": "1", "JY_VISION_TEXT_PASS": "1",
                      "JY_SLOT_DIR": str(self.root / "slots")})
        self.stack.enter_context(mock.patch.dict(os.environ, clean, clear=True))
        self.stack.enter_context(mock.patch.object(vision, "_orientation", return_value="portrait"))
        self.sample = self.stack.enter_context(mock.patch.object(vision, "_sample_frames", return_value=[
            {"path": str(self.frame), "timestamp": 7.4, "suffix": "t1"}]))
        self.stack.enter_context(mock.patch.object(vision, "_write_sheet", return_value=True))
        self.manifest = {"product_id": "p41", "product": "synthetic product", "category": "纸品"}

    def report(self, task):
        return self.work / task / "report" / "draft"

    def analysis(self, shot_id=SHOT_ID):
        return {"shots": [{"shot_id": shot_id, "video": str(self.video), "source_start": 0,
            "source_end": 9.4, "frame_exists": True, "status": "pending_analysis"}]}

    def response(self, sheet, prompt, *args, **kwargs):
        if sheet.name.startswith("text_"):
            return _text(["verified text"])
        return True, _first_pass()

    def run_task(self, task, manifest=None, analysis=None):
        return vision.enrich_with_vision(manifest or self.manifest,
            analysis or self.analysis(), self.report(task), profile="volc")

    def test_different_tasks_and_shot_ids_reuse_both_vision_and_verified_text(self):
        with mock.patch.object(vision, "_run_vision_with_retry", side_effect=self.response) as api:
            first = self.run_task("task_a", {**self.manifest, "task_id": "a", "full_script": "script a"})
            self.assertEqual(api.call_count, 3)
            self.sample.reset_mock()
            second = self.run_task("task_b", {**self.manifest, "task_id": "b", "full_script": "script b"},
                                   self.analysis("different-shot-id"))
            self.assertEqual(api.call_count, 3)
            self.sample.assert_not_called()
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(second["shots"][0]["shot_id"], "different-shot-id")
        self.assertEqual(second["shots"][0]["text_pass"], "hires")
        self.assertEqual(second["shots"][0]["readable_claims"], ["verified", "text"])
        self.assertEqual(second["vision_analysis"]["text_cache_hits"], 1)
        self.assertEqual(vision._vision_cache_path(self.report("task_a")),
                         vision._vision_cache_path(self.report("task_b")))

    def test_simultaneous_cold_tasks_have_one_producer(self):
        barrier = threading.Barrier(2)
        def run(task):
            barrier.wait(timeout=10)
            return self.run_task(task)

        def response(*args, **kwargs):
            time.sleep(0.03)
            return self.response(*args, **kwargs)

        with mock.patch.object(vision, "_run_vision_with_retry", side_effect=response) as api, \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, task) for task in ("task_a", "task_b")]
            results = [future.result(timeout=15) for future in futures]
        self.assertEqual(api.call_count, 3)  # One vision call and two independent text reads, total.
        self.assertEqual(sum(r["vision_analysis"]["cache_hits"] for r in results), 1)
        self.assertTrue(all(r["ok"] for r in results))

    def test_other_products_can_compute_concurrently(self):
        barrier = threading.Barrier(2)
        def response(sheet, *args, **kwargs):
            if not sheet.name.startswith("text_"):
                barrier.wait(timeout=10)
            return self.response(sheet, *args, **kwargs)

        with mock.patch.object(vision, "_run_vision_with_retry", side_effect=response) as api, \
                ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.run_task, f"task_{index}", {**self.manifest, "product_id": f"p{index}"})
                       for index in (1, 2)]
            self.assertTrue(all(f.result(timeout=15)["ok"] for f in futures))
        self.assertEqual(api.call_count, 6)

    def test_sibling_legacy_cache_is_imported_without_new_calls(self):
        row = json.loads(_first_pass())["shots"][0]
        row.update(status="ready_for_matching", action_complete=True, analysis_confidence=0.8,
                   text_pass="hires", text_agreed=["verified"], readable_claims=["verified"])
        key = vision._vision_cache_key(self.manifest, self.analysis()["shots"][0], "volc")
        legacy = self.work / "old_task" / ".cache" / "material_vision.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps({key: row}), encoding="utf-8")
        with mock.patch.object(vision, "_run_vision_with_retry", side_effect=AssertionError("duplicate API")):
            result = self.run_task("new_task")
        self.assertEqual(result["vision_analysis"]["text_cache_hits"], 1)
        self.assertTrue(vision._vision_cache_path(self.report("new_task")).is_file())

    def test_completed_rejection_is_cached_but_never_promoted(self):
        body = _first_pass(action_complete=False, action_segments=[], evidence_intervals=[], result=None,
                           evidence_strength="C", readable_claims=[], text_tiles=[], role="usage_demo")
        with mock.patch.object(vision, "_run_vision_with_retry", return_value=(True, body)) as api:
            first = self.run_task("task_a")
            second = self.run_task("task_b")
        self.assertEqual(api.call_count, 1)
        self.assertFalse(first["ok"] or second["ok"])
        self.assertEqual(second["shots"][0]["status"], "pending_analysis")
        self.assertFalse(second["shots"][0]["action_complete"])
        self.assertEqual(second["vision_analysis"]["rejected_cache_hits"], 1)

    def test_source_change_and_sku_change_require_new_observations(self):
        with mock.patch.object(vision, "_run_vision_with_retry", side_effect=self.response) as api:
            self.run_task("task_a")
            self.video.write_bytes(b"source-content-changed")
            self.run_task("task_b")
            self.run_task("task_c", {**self.manifest, "sku": "other-sku"})
        self.assertEqual(api.call_count, 9)

    def test_provider_failure_is_not_reusable(self):
        with mock.patch.object(vision, "_run_vision_with_retry", return_value=(False, "timeout")) as api:
            self.run_task("task_a")
            self.run_task("task_b")
        self.assertEqual(api.call_count, 2)

    def test_busy_product_timeout_never_falls_through_to_duplicate_calls(self):
        report = self.report("task_a")
        lock = vision._product_cache_lock(self.manifest, self.analysis(), vision._vision_cache_path(report))
        with file_lock(lock), mock.patch.dict(os.environ, {"JY_VISION_CACHE_WAIT_TIMEOUT": "0"}), \
                mock.patch.object(vision, "_run_vision_with_retry") as api:
            with self.assertRaises(TimeoutError):
                self.run_task("task_a")
        api.assert_not_called()

    def test_text_only_backfill_batches_library_merges_but_checkpoints_each_result(self):
        analysis = {"shots": []}
        cache = {}
        for index in range(19):
            shot = {**self.analysis()["shots"][0], "shot_id": f"text-{index}",
                    "source_start": index, "source_end": index + 9.4}
            analysis["shots"].append(shot)
            row = json.loads(_first_pass())["shots"][0]
            row.update(status="ready_for_matching", text_pass="pending",
                       analysis_confidence=.8)
            cache[vision._vision_cache_key(self.manifest, shot, "volc")] = row
        path = vision._vision_cache_path(self.report("text_only"))
        vision._save_vision_cache(path, cache)
        checkpoints = []
        original_persist = vision._persist

        def persist(directory, data):
            original_persist(directory, data)
            disk = json.loads((directory / "shot_candidates.json").read_text(encoding="utf-8"))
            checkpoints.append(sum(row.get("text_pass") == "hires" for row in disk["shots"]))

        def response(sheet, prompt, *args, **kwargs):
            self.assertTrue(sheet.name.startswith("text_"), "visual call repeated during text backfill")
            shot_id = re.search(r"shot_id=(text-\d+)", prompt).group(1)
            return True, json.dumps({"shot_id": shot_id, "readable_claims": ["verified"],
                                     "pointed_text": []})

        with mock.patch.object(vision, "_run_vision_with_retry", side_effect=response) as api, \
                mock.patch.object(vision, "_save_vision_cache", wraps=vision._save_vision_cache) as saves, \
                mock.patch.object(vision, "_persist", side_effect=persist):
            result = self.run_task("text_only", analysis=analysis)
        self.assertEqual(api.call_count, 38)  # Exactly two reads; no repeated vision.
        self.assertLess(saves.call_count, 19 // 2)
        self.assertTrue(set(range(1, 20)).issubset(checkpoints))
        disk = vision._load_vision_cache(path)
        self.assertTrue(all(row.get("text_pass") == "hires" for row in disk.values()))
        self.assertEqual(result["vision_analysis"]["cache_hits"], 19)

    def test_text_cache_tail_is_flushed_when_a_later_read_raises(self):
        original = vision._iter_indexed
        def interrupted(jobs, fn, workers):
            if fn.__name__ == "_read_text":
                yield fn(jobs[0])
                raise RuntimeError("synthetic interruption after one durable text result")
            yield from original(jobs, fn, workers)
        with mock.patch.object(vision, "_run_vision_with_retry", side_effect=self.response), \
                mock.patch.object(vision, "_iter_indexed", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                self.run_task("interrupted")
        cache = vision._load_vision_cache(vision._vision_cache_path(self.report("interrupted")))
        self.assertEqual(next(iter(cache.values()))["text_pass"], "hires")
        report = json.loads((self.report("interrupted") / "shot_candidates.json").read_text(encoding="utf-8"))
        self.assertEqual(report["shots"][0]["text_pass"], "hires")


if __name__ == "__main__":
    unittest.main()
