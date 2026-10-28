"""Offline recovery and reuse before media decoding and model requests."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import ai_editor, ai_editor_contract as contract, material_cache, shot_analyzer
from orchestrator import vision_analyzer as vision
from test_v152_resume_integrity import SHOT_ID, _first_pass, _text


class MaterialCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        env = {k: v for k, v in os.environ.items() if not k.startswith("JY_")}
        env.update(JY_VISION_CACHE_DIR=str(self.root / "cache"), JY_SLOT_DIR=str(self.root / "slots"),
                   JY_VISION_TEXT_PASS="1", JY_VISION_TEXT_MIN_SHOTS="1")
        self.stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        self.video = self.root / "source.mp4"
        self.video.write_bytes(b"same source")
        self.frame = self.root / "frame.jpg"
        self.frame.write_bytes(b"frame")
        self.manifest = {"product_id": "p", "product": "same product", "material_sources": [str(self.video)]}

    def raw(self, manifest, base, report, **kwargs):
        frame = report / "frame.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(b"frame")
        return {"ok": True, "shots": [{"shot_id": SHOT_ID, "video": manifest["material_sources"][0],
            "source_start": 0, "source_end": 9.4, "frame_exists": True,
            "frame_path": str(frame), "frame_paths": [str(frame)], "status": "pending_analysis"}], "issues": []}

    def test_concurrent_cold_media_is_prepared_once_before_observation(self):
        barrier = threading.Barrier(2)
        calls = []
        def run(index):
            barrier.wait(5)
            return material_cache.analyze_product(self.manifest, self.root, self.root / f"task{index}", vision=False)
        def prepare(*args, **kwargs):
            calls.append("prepare")
            return self.raw(*args, **kwargs)
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=prepare), ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, (1, 2)))
        self.assertEqual(calls, ["prepare"])
        self.assertEqual(sum(r["material_cache"]["sources_hit"] for r in results), 1)
        self.assertTrue(all(r["material_cache"]["ready"] for r in results))

    def test_source_change_only_prepares_changed_source(self):
        other = self.root / "other.mp4"
        other.write_bytes(b"other source")
        manifest = {**self.manifest, "material_sources": [str(self.video), str(other)]}
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=self.raw) as prepare:
            material_cache.analyze_product(manifest, self.root, self.root / "a", vision=False)
            warm = material_cache.analyze_product(manifest, self.root, self.root / "b", vision=False)
            self.assertEqual(prepare.call_count, 2)
            other.write_bytes(b"changed other source")
            changed = material_cache.analyze_product(manifest, self.root, self.root / "c", vision=False)
        self.assertEqual(prepare.call_count, 3)
        self.assertEqual(warm["material_cache"]["sources_hit"], 2)
        self.assertEqual(changed["material_cache"]["sources_hit"], 1)
        self.assertEqual(len({row["shot_id"] for row in changed["shots"]}), 2)

    def test_warm_product_skips_cutting_frames_and_all_observation_calls(self):
        def response(sheet, *args, **kwargs):
            return _text(["80抽"]) if sheet.name.startswith("text_") else (True, _first_pass())
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=self.raw) as prepare, \
                mock.patch.object(vision, "_sample_frames", return_value=[{
                    "path": str(self.frame), "timestamp": 7.4, "suffix": "t1"}]) as frames, \
                mock.patch.object(vision, "_orientation", return_value="portrait"), \
                mock.patch.object(vision, "_write_sheet", return_value=True), \
                mock.patch.object(vision, "_run_vision_with_retry", side_effect=response) as api:
            first = material_cache.analyze_product(self.manifest, self.root, self.root / "a")
            warm = material_cache.analyze_product(self.manifest, self.root, self.root / "b")
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(frames.call_count, 1)
        self.assertEqual(api.call_count, 3)
        self.assertTrue(first["material_cache"]["ready"] and warm["material_cache"]["ready"])

    def analysis(self):
        return {"shots": [{"shot_id": SHOT_ID, "video": str(self.video), "source_start": 0,
            "source_end": 9.4, "frame_path": str(self.frame), "frame_paths": [str(self.frame)],
            "frame_exists": True, "status": "pending_analysis"}]}

    def seed_visual(self, **extra):
        row = json.loads(_first_pass())["shots"][0]
        row.update(status="ready_for_matching", analysis_confidence=.8, action_complete=True,
                   visual_observation_complete=True, **extra)
        key = vision._vision_cache_key(self.manifest, self.analysis()["shots"][0], "volc")
        cache = vision._vision_cache_path(self.root / "a")
        vision._save_vision_cache(cache, {key: row})

    def test_explicit_prior_model_reuse_keeps_provenance_and_does_not_promote_cache(self):
        with mock.patch.dict(os.environ, {"JY_VISION_TRANSPORT": "volc_glm", "JY_VISION_MODEL": "glm-5.3-flash"}):
            self.seed_visual(text_pass="not_applicable")
            old_key = vision._vision_cache_key(self.manifest, self.analysis()["shots"][0], "volc")
        env = {"JY_VISION_TRANSPORT": "qwen", "JY_QWEN_MODEL": "qwen-test",
               "JY_QWEN_BASE_URL": "https://example.test/v1", "JY_QWEN_API_KEY": "test",
               "JY_REUSE_VISION_MODEL": "glm-5.3-flash", "JY_REUSE_VISION_TRANSPORT": "volc_glm"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(vision, "_run_vision_with_retry") as api:
            qwen_key = vision._vision_cache_key(self.manifest, self.analysis()["shots"][0], "volc")
            result = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "a", profile="volc")
        api.assert_not_called()
        shot = result["shots"][0]
        self.assertEqual(shot["analysis_cache_key"], old_key)
        self.assertEqual(shot["analysis_cache_origin"], {"transport": "volc_glm", "model": "glm-5.3-flash"})
        self.assertEqual(result["vision_analysis"]["prior_model_cache_hits"], 1)
        self.assertNotIn(qwen_key, vision._load_vision_cache(vision._vision_cache_path(self.root / "a")))

    def test_prior_model_reuse_keeps_action_interval_gate(self):
        with mock.patch.dict(os.environ, {"JY_VISION_TRANSPORT": "volc_glm", "JY_VISION_MODEL": "glm-5.3-flash"}):
            self.seed_visual(text_pass="not_applicable", action_complete_source="evidence_interval", evidence_intervals=[])
        env = {"JY_VISION_TRANSPORT": "qwen", "JY_QWEN_MODEL": "qwen-test",
               "JY_QWEN_BASE_URL": "https://example.test/v1", "JY_QWEN_API_KEY": "test",
               "JY_REUSE_VISION_MODEL": "glm-5.3-flash", "JY_REUSE_VISION_TRANSPORT": "volc_glm"}
        with mock.patch.dict(os.environ, env), mock.patch.object(vision, "_run_vision_with_retry") as api:
            result = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "a", profile="volc")
        api.assert_not_called()
        self.assertFalse(result["shots"][0]["action_complete"])
        self.assertEqual(result["shots"][0]["status"], "pending_analysis")

    def test_prior_model_does_not_reuse_unverified_text_or_changed_product(self):
        with mock.patch.dict(os.environ, {"JY_VISION_TRANSPORT": "volc_glm", "JY_VISION_MODEL": "glm-5.3-flash"}):
            self.seed_visual()
        env = {"JY_VISION_TRANSPORT": "qwen", "JY_QWEN_MODEL": "qwen-test",
               "JY_QWEN_BASE_URL": "https://example.test/v1", "JY_QWEN_API_KEY": "test",
               "JY_REUSE_VISION_MODEL": "glm-5.3-flash", "JY_REUSE_VISION_TRANSPORT": "volc_glm"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(vision, "_write_sheet", return_value=True), \
                mock.patch.object(vision, "_orientation", return_value="portrait"), \
                mock.patch.object(vision, "_sample_frames", return_value=[{"path": str(self.frame), "timestamp": 7.4, "suffix": "t1"}]), \
                mock.patch.object(vision, "_run_vision_with_retry", return_value=(False, "test failure")) as api:
            first = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "a", profile="volc")
            second = vision.enrich_with_vision({**self.manifest, "product_id": "other"}, self.analysis(), self.root / "b", profile="volc")
        self.assertGreaterEqual(api.call_count, 2)
        self.assertEqual(first["vision_analysis"]["prior_model_cache_hits"], 0)
        self.assertEqual(second["vision_analysis"]["prior_model_cache_hits"], 0)

    def test_old_visual_cache_without_ocr_only_reads_text_then_warm_calls_zero(self):
        self.seed_visual()
        def response(sheet, prompt, *args, **kwargs):
            self.assertTrue(sheet.name.startswith("text_"))
            return _text(["80抽"])
        with mock.patch.object(vision, "_write_sheet", return_value=True), \
                mock.patch.object(vision, "_orientation", return_value="portrait"), \
                mock.patch.object(vision, "_sample_frames") as decode, \
                mock.patch.object(vision, "_run_vision_with_retry", side_effect=response) as api:
            first = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "a", profile="volc")
            second = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "b", profile="volc")
        self.assertEqual(api.call_count, 2)
        decode.assert_not_called()
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(second["vision_analysis"]["text_cache_hits"], 1)

    def test_failed_ocr_keeps_visual_and_is_not_matchable(self):
        self.seed_visual(text_pass="failed")
        with mock.patch.object(vision, "_write_sheet", return_value=True), \
                mock.patch.object(vision, "_orientation", return_value="portrait"), \
                mock.patch.object(vision, "_run_vision_with_retry", return_value=(False, "empty response")) as api:
            failed = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "a", profile="volc")
        self.assertEqual(api.call_count, 1)
        self.assertFalse(failed["ok"])
        self.assertTrue(vision.needs_vision(failed["shots"][0]))
        self.assertTrue(next(iter(vision._load_vision_cache(vision._vision_cache_path(self.root / "a")).values()))["action_complete"])

    def test_visual_failure_defers_text_and_preserves_successful_observation(self):
        self.seed_visual()
        analysis = self.analysis()
        second = dict(analysis["shots"][0], shot_id="second", source_start=10, source_end=19)
        analysis["shots"].append(second)
        with mock.patch.object(vision, "_write_sheet", return_value=True), \
                mock.patch.object(vision, "_orientation", return_value="portrait"), \
                mock.patch.object(vision, "_sample_frames", return_value=[{
                    "path": str(self.frame), "timestamp": 17.4, "suffix": "t1"}]), \
                mock.patch.object(vision, "_run_vision_with_retry", return_value=(False, "empty response")) as api:
            failed = vision.enrich_with_vision(self.manifest, analysis, self.root / "a", profile="volc")
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["text_pass"]["status"], "DEFERRED_VISUAL_ERRORS")
        self.assertEqual(failed["text_pass"]["requested"], 1)
        self.assertEqual(api.call_count, 1)  # only failed visual batch, no text request
        self.assertEqual(failed["pending_items"][-1]["type"], "VISION_CALL_FAILED")
        cached = vision._load_vision_cache(vision._vision_cache_path(self.root / "a"))
        self.assertTrue(any(row.get("action_complete") for row in cached.values()))

    def test_candidate_frames_and_sheet_reuse_across_tasks_and_purposes(self):
        shot = self.analysis()["shots"][0]
        sentence = {"index": 1, "text": "first claim", "kind": "selling_point",
                    "selling_points": [{"id": "p1", "text": "claim"}], "scene_ids": []}
        result = {"verdict": "matched", "same_product": True, "basis": "visible_result",
                  "observation": "visible observation", "reason": "visible reason", "confidence": .8,
                  "supported_point_ids": ["p1"], "time_sensitive": False, "scene_ids": [],
                  "evidence_intervals_us": [{"start": 1000000, "end": 2000000}]}
        def extract(source, timestamp, target, **kwargs):
            target.write_bytes(b"prepared pixels")
            return True
        def sheet(items, target, **kwargs):
            target.write_bytes(b"prepared sheet")
            return True
        from orchestrator import platform_env
        with mock.patch.object(shot_analyzer, "extract_frame", side_effect=extract) as decode, \
                mock.patch.object(platform_env, "find_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
                mock.patch.object(vision, "_write_sheet", side_effect=sheet) as sheets, \
                mock.patch.object(ai_editor, "_call", return_value=result) as api, \
                mock.patch.object(contract, "file_sha256", side_effect=AssertionError("full file read")):
            a = ai_editor._review(self.manifest, sentence, shot, self.root / "a")
            b = ai_editor._review(self.manifest, {**sentence, "text": "different claim"}, shot, self.root / "b")
        self.assertEqual(decode.call_count, 8)
        self.assertEqual(sheets.call_count, 1)
        self.assertEqual(api.call_count, 2)  # The new purpose still requires its own judgment.
        self.assertEqual(a["sheet"], b["sheet"])
        self.assertNotEqual(api.call_args_list[0].args[0], api.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
