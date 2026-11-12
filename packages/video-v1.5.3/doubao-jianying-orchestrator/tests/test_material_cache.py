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
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import ai_editor, ai_editor_contract as contract, material_cache, shot_analyzer, timing_contract
from orchestrator import vision_analyzer as vision
from orchestrator.application import cli
from test_v152_resume_integrity import SHOT_ID, _first_pass, _text


class MaterialCacheTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
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
        other = self.root / "other.mp4"
        other.write_bytes(b"second source")
        sources = [str(self.video), str(other)]
        manifest = {**self.manifest, "material_sources": sources}
        tasks_ready = threading.Barrier(2)
        second_finished = threading.Event()

        def run(index):
            tasks_ready.wait(5)
            return material_cache.analyze_product(manifest, self.root, self.root / f"task{index}", vision=False)

        def prepare(manifest, *args, **kwargs):
            if manifest["material_sources"] == [sources[0]]:
                self.assertTrue(second_finished.wait(5), "second source never ran in parallel")
            result = self.raw(manifest, *args, **kwargs)
            if manifest["material_sources"] == [sources[1]]:
                second_finished.set()
            return result

        with mock.patch.dict(os.environ, {"JY_VISION_PREP_SLOTS": "2"}), \
                mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=prepare) as cut, \
                ThreadPoolExecutor(2) as pool:
            results = list(pool.map(run, (1, 2)))
        self.assertCountEqual([c.args[0]["material_sources"][0] for c in cut.call_args_list], sources)
        self.assertEqual(sorted(r["material_cache"]["sources_hit"] for r in results), [0, 2])
        for result in results:
            self.assertTrue(result["material_cache"]["ready"])
            self.assertEqual([row["video"] for row in result["shots"]], sources)
            self.assertEqual([row["shot_id"] for row in result["shots"]],
                             ["source_001_shot_001", "source_002_shot_001"])

    def test_failed_parallel_source_can_resume_from_completed_peer(self):
        other = self.root / "other.mp4"
        other.write_bytes(b"second source")
        queued = self.root / "last.mp4"
        queued.write_bytes(b"queued source")
        sources = [str(self.video), str(other), str(queued)]
        manifest = {**self.manifest, "material_sources": sources}
        report = self.root / "product"
        draining = threading.Event()

        class ObservedPool(ThreadPoolExecutor):
            def shutdown(self, *args, **kwargs):
                draining.set()
                return super().shutdown(*args, **kwargs)

        def fail_one(manifest, *args, **kwargs):
            source = manifest["material_sources"][0]
            if source == str(other):
                raise TimeoutError("source failed")
            self.assertEqual(source, str(self.video), "queued source started after failure")
            self.assertTrue(draining.wait(5), "failure hidden behind slower first source")
            return self.raw(manifest, *args, **kwargs)

        with mock.patch.dict(os.environ, {"JY_VISION_PREP_SLOTS": "2"}), \
                mock.patch.object(material_cache, "ThreadPoolExecutor", ObservedPool), \
                mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=fail_one) as cut, \
                mock.patch.object(vision, "enrich_with_vision") as enrich:
            with self.assertRaisesRegex(TimeoutError, "source failed"):
                material_cache.analyze_product(manifest, self.root, report)
            self.assertCountEqual([c.args[0]["material_sources"][0] for c in cut.call_args_list], sources[:2])
            enrich.assert_not_called()
        self.assertFalse((report / "shot_candidates.json").exists())
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=self.raw) as retry:
            recovered = material_cache.analyze_product(manifest, self.root, report, vision=False)
        self.assertTrue(recovered["ok"])
        self.assertEqual(recovered["material_cache"]["sources_hit"], 1)
        self.assertCountEqual([c.args[0]["material_sources"][0] for c in retry.call_args_list], sources[1:])

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
            vision._vision_cache_path(self.root / "a").write_text("{}", encoding="utf8")
            retained = material_cache.analyze_product(self.manifest, self.root, self.root / "a")
            self.assertTrue(retained["material_cache"]["ready"])
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(frames.call_count, 1)
        self.assertEqual(api.call_count, 3)
        self.assertTrue(first["material_cache"]["ready"] and warm["material_cache"]["ready"])

    def test_duration_refresh_keeps_cached_frames_and_visual_observations(self):
        def response(sheet, *args, **kwargs):
            return _text(["80抽"]) if sheet.name.startswith("text_") else (True, _first_pass())
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=self.raw) as prepare, \
                mock.patch.object(vision, "_sample_frames", return_value=[{
                    "path": str(self.frame), "timestamp": 7.4, "suffix": "t1"}]) as frames, \
                mock.patch.object(vision, "_orientation", return_value="portrait"), \
                mock.patch.object(vision, "_write_sheet", return_value=True), \
                mock.patch.object(vision, "_run_vision_with_retry", side_effect=response) as api, \
                mock.patch.object(timing_contract, "probe_material_duration_us", return_value={
                    "duration_us": 9_399_667, "source": "ffprobe_video_stream", "confidence": "HIGH"}) as probe:
            first = material_cache.analyze_product(self.manifest, self.root, self.root / "a")
            warm = material_cache.analyze_product(self.manifest, self.root, self.root / "b", ffprobe="ffprobe")
            again = material_cache.analyze_product(self.manifest, self.root, self.root / "c", ffprobe="ffprobe")
        self.assertEqual((prepare.call_count, frames.call_count, api.call_count, probe.call_count), (1, 1, 3, 1))
        self.assertEqual(first["shots"][0]["source_end"], warm["shots"][0]["source_end"])
        self.assertEqual(warm["shots"][0]["material_duration_us"], 9_399_667)
        self.assertEqual(warm["shots"][0]["analysis_cache_key"], first["shots"][0]["analysis_cache_key"])
        self.assertEqual(again["material_cache"]["sources_hit"], 1)

    def test_large_duration_change_reprepares_only_affected_source(self):
        other = self.root / "other.mp4"
        other.write_bytes(b"other source")
        manifest = {**self.manifest, "material_sources": [str(self.video), str(other)]}
        def fresh(*args, **kwargs):
            result = self.raw(*args, **kwargs)
            result["shots"][0].update(source_end=9.3, material_duration_us=9_300_000,
                                      material_duration_confidence="HIGH")
            return result
        def probe(source, **kwargs):
            return {"duration_us": 9_300_000 if Path(source).name == self.video.name else 9_399_667,
                    "source": "ffprobe_video_stream", "confidence": "HIGH"}
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=self.raw):
            material_cache.analyze_product(manifest, self.root, self.root / "a", vision=False)
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=fresh) as prepare, \
                mock.patch.object(timing_contract, "probe_material_duration_us", side_effect=probe):
            result = material_cache.analyze_product(manifest, self.root, self.root / "b", ffprobe="ffprobe", vision=False)
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(result["material_cache"]["sources_hit"], 1)
        self.assertEqual([row["material_duration_us"] for row in result["shots"]], [9_300_000, 9_399_667])

    def test_unconfirmed_duration_or_changed_source_cannot_refresh_cache(self):
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=self.raw):
            material_cache.analyze_product(self.manifest, self.root, self.root / "a", vision=False)
        with mock.patch.object(timing_contract, "probe_material_duration_us", return_value={
                "duration_us": 9_399_667, "source": "container_duration", "confidence": "LOW"}), \
                self.assertRaisesRegex(ValueError, "MATERIAL_DURATION_BOUND_UNCONFIRMED"):
            material_cache.analyze_product(self.manifest, self.root, self.root / "b", ffprobe="ffprobe", vision=False)
        def changed(*args, **kwargs):
            self.video.write_bytes(b"source changed during probing")
            return {"duration_us": 9_399_667, "source": "ffprobe_video_stream", "confidence": "HIGH"}
        with mock.patch.object(timing_contract, "probe_material_duration_us", side_effect=changed), \
                self.assertRaisesRegex(ValueError, "MATERIAL_SOURCE_CHANGED_DURING_PREPARATION"):
            material_cache.analyze_product(self.manifest, self.root, self.root / "c", ffprobe="ffprobe", vision=False)

    def test_low_duration_with_incomplete_preparation_never_calls_vision(self):
        def incomplete(*args, **kwargs):
            result = self.raw(*args, **kwargs)
            result.update(ok=False, issues=[{"type": "source_frame_failed"}])
            result["shots"][0]["material_duration_confidence"] = "LOW"
            return result
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=incomplete), \
                mock.patch.object(vision, "enrich_with_vision") as api, \
                self.assertRaisesRegex(ValueError, "MATERIAL_DURATION_BOUND_UNCONFIRMED"):
            material_cache.analyze_product(self.manifest, self.root, self.root / "a", ffprobe="ffprobe")
        api.assert_not_called()

    def test_inline_and_recovery_file_both_refresh_exact_duration_before_selection(self):
        old = {"ok": True, "shots": [{"shot_id": "s", "video": str(self.video),
               "source_start": 0, "source_end": 4.167, "material_duration_us": 4_167_000}]}
        file = self.root / "shot_candidates.json"
        file.write_text(json.dumps(old), encoding="utf-8")
        for manifest in ({"shot_analysis": old}, {"shot_analysis_file": str(file)}):
            with self.subTest(source=list(manifest)), \
                    mock.patch.object(cli, "environment_report", return_value={"ffprobe": "ffprobe"}), \
                    mock.patch.object(timing_contract, "probe_material_duration_us", return_value={
                        "duration_us": 4_166_667, "source": "ffprobe_video_stream", "confidence": "HIGH"}) as probe:
                result = cli._run_shot_analysis({**manifest, "vision_analysis": False},
                                                self.root / "manifest.json", self.root / "report")
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(result["shots"][0]["material_duration_us"], 4_166_667)
            self.assertEqual(result["shots"][0]["source_end"], 4.167)
        self.assertEqual(old["shots"][0]["material_duration_us"], 4_167_000)
        self.assertEqual(json.loads(file.read_text(encoding="utf-8")), old)

    def test_external_analysis_blocks_unconfirmed_or_stale_geometry_before_vision(self):
        analysis = {"ok": True, "shots": [{"shot_id": "s", "video": str(self.video),
                    "source_start": 0, "source_end": 4.167, "material_duration_us": 4_167_000}]}
        for result in ({"duration_us": 4_166_667, "source": "last_video_pts", "confidence": "LOW"},
                       {"duration_us": 4_100_000, "source": "ffprobe_video_stream", "confidence": "HIGH"}):
            with self.subTest(probe=result), mock.patch.object(cli, "environment_report", return_value={"ffprobe": "ffprobe"}), \
                    mock.patch.object(timing_contract, "probe_material_duration_us", return_value=result), \
                    mock.patch.object(vision, "enrich_with_vision") as api:
                got = cli._run_shot_analysis({"shot_analysis": analysis}, self.root / "manifest.json", self.root / "report")
            self.assertFalse(got["ok"])
            self.assertEqual(got["pending_items"][0]["type"], "MATERIAL_DURATION_RECHECK_FAILED")
            api.assert_not_called()

    def test_match_shots_refreshes_external_analysis_and_blocks_low_before_model(self):
        source = self.root / "manifest.json"
        source.write_text(json.dumps({"selection_mode": "ai_editor"}), encoding="utf-8")
        analysis = self.root / "analysis.json"
        analysis.write_text(json.dumps({"shots": [{"shot_id": "s", "video": str(self.video),
            "source_start": 0, "source_end": 4.167, "material_duration_us": 4_167_000}]}), encoding="utf-8")
        args = SimpleNamespace(input=str(source), report_dir=str(self.root / "report"),
                               analysis=str(analysis), topk=3, require_selection=False)
        for confidence in ("HIGH", "LOW"):
            with self.subTest(confidence=confidence), \
                    mock.patch.object(cli, "environment_report", return_value={"ffprobe": "ffprobe"}), \
                    mock.patch.object(timing_contract, "probe_material_duration_us", return_value={
                        "duration_us": 4_166_667, "source": "ffprobe_video_stream", "confidence": confidence}), \
                    mock.patch.object(ai_editor, "prepare", side_effect=lambda m, a, r: a) as prepare, \
                    mock.patch.object(shot_analyzer, "match_manifest", return_value={"ok": True, "manifest": {}}), \
                    mock.patch.object(cli, "_emit", side_effect=lambda value, code=0: code):
                code = cli.cmd_match_shots(args)
            if confidence == "HIGH":
                self.assertEqual(code, 0)
                self.assertEqual(prepare.call_args.args[1]["shots"][0]["material_duration_us"], 4_166_667)
            else:
                self.assertEqual(code, 1)
                prepare.assert_not_called()

    def test_explicit_empty_evidence_rejection_is_cached_but_not_matchable(self):
        response = json.loads(_first_pass())
        response["shots"][0].update(description="空厨房台面，无产品或动作", visual_tags=["厨房台面"],
                                    evidence_tags=[], action_complete=False, confidence=.95)
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=self.raw), \
                mock.patch.object(vision, "_sample_frames", return_value=[{
                    "path": str(self.frame), "timestamp": 7.4, "suffix": "t1"}]), \
                mock.patch.object(vision, "_orientation", return_value="portrait"), \
                mock.patch.object(vision, "_write_sheet", return_value=True), \
                mock.patch.object(vision, "_run_vision_with_retry", return_value=(True, json.dumps(response))) as api:
            first = material_cache.analyze_product(self.manifest, self.root, self.root / "a")
            warm = material_cache.analyze_product(self.manifest, self.root, self.root / "b")
        self.assertEqual(api.call_count, 1)
        self.assertTrue(first["material_cache"]["ready"] and warm["material_cache"]["ready"])
        for result in (first, warm):
            shot = result["shots"][0]
            self.assertEqual(shot["evidence_tags"], [])
            self.assertFalse(shot["action_complete"])
            self.assertEqual(shot["status"], "pending_analysis")
            self.assertTrue(vision._reusable_visual_observation(shot))

    def test_empty_evidence_does_not_certify_missing_or_positive_action(self):
        for action in (True, None):
            with self.subTest(action=action), mock.patch.dict(os.environ, {"JY_VISION_CACHE_DIR": str(self.root / str(action))}):
                response = json.loads(_first_pass())
                response["shots"][0].update(evidence_tags=[], action_complete=action)
                with mock.patch.object(vision, "_sample_frames", return_value=[{
                        "path": str(self.frame), "timestamp": 7.4, "suffix": "t1"}]), \
                        mock.patch.object(vision, "_orientation", return_value="portrait"), \
                        mock.patch.object(vision, "_write_sheet", return_value=True), \
                        mock.patch.object(vision, "_run_vision_with_retry", return_value=(True, json.dumps(response))):
                    result = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / str(action))
                shot = result["shots"][0]
                self.assertEqual(shot["status"], "pending_analysis")
                self.assertFalse(vision._reusable_visual_observation(shot))

    def analysis(self):
        return {"shots": [{"shot_id": SHOT_ID, "video": str(self.video), "source_start": 0,
            "source_end": 9.4, "frame_path": str(self.frame), "frame_paths": [str(self.frame)],
            "frame_exists": True, "status": "pending_analysis"}]}

    def seed_visual(self, **extra):
        row = json.loads(_first_pass())["shots"][0]
        row.update(status="ready_for_matching", analysis_confidence=.8, action_complete=True,
                   visual_observation_complete=True, **extra)
        key = vision._vision_cache_key(self.manifest, self.analysis()["shots"][0])
        cache = vision._vision_cache_path(self.root / "a")
        vision._save_vision_cache(cache, {key: row})

    def test_explicit_prior_model_reuse_keeps_provenance_and_does_not_promote_cache(self):
        with mock.patch.dict(os.environ, {"JY_VISION_TRANSPORT": "volc_glm", "JY_VISION_MODEL": "glm-5.3-flash"}):
            self.seed_visual(text_pass="not_applicable")
            old_key = vision._vision_cache_key(self.manifest, self.analysis()["shots"][0])
        env = {"JY_VISION_TRANSPORT": "qwen", "JY_QWEN_MODEL": "qwen-test",
               "JY_QWEN_BASE_URL": "https://example.test/v1", "JY_QWEN_API_KEY": "test",
               "JY_REUSE_VISION_MODEL": "glm-5.3-flash", "JY_REUSE_VISION_TRANSPORT": "volc_glm"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(vision, "_run_vision_with_retry") as api:
            qwen_key = vision._vision_cache_key(self.manifest, self.analysis()["shots"][0])
            result = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "a")
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
            result = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "a")
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
            first = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "a")
            second = vision.enrich_with_vision({**self.manifest, "product_id": "other"}, self.analysis(), self.root / "b")
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
            first = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "a")
            second = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "b")
        self.assertEqual(api.call_count, 2)
        decode.assert_not_called()
        self.assertTrue(first["ok"] and second["ok"])
        self.assertEqual(second["vision_analysis"]["text_cache_hits"], 1)

    def test_failed_ocr_keeps_visual_and_is_not_matchable(self):
        self.seed_visual(text_pass="failed")
        with mock.patch.object(vision, "_write_sheet", return_value=True), \
                mock.patch.object(vision, "_orientation", return_value="portrait"), \
                mock.patch.object(vision, "_run_vision_with_retry", return_value=(False, "empty response")) as api:
            failed = vision.enrich_with_vision(self.manifest, self.analysis(), self.root / "a")
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
            failed = vision.enrich_with_vision(self.manifest, analysis, self.root / "a")
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
