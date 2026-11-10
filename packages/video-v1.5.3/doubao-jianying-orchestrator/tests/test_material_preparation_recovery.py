"""Fix 2026-11-10: MATERIAL_PREP_INCOMPLETE root causes (non-video source, unauditable vision answer)."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import material_cache, shot_analyzer, timing_contract
from orchestrator import vision_analyzer as vision

JPEG_AS_MOV = {"streams": [{"codec_name": "mjpeg", "codec_type": "video", "width": 90, "height": 160}],
               "format": {"nb_streams": 1, "format_name": "jpeg_pipe"}}


def _probe(payload, returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload), stderr="")


class NonVideoSourceTests(unittest.TestCase):
    def classify(self, payload=None, returncode=0, side_effect=None):
        run = mock.patch.object(shot_analyzer.subprocess, "run",
                                side_effect=side_effect, return_value=_probe(payload or {}, returncode))
        with run, mock.patch.object(shot_analyzer, "_decodable_single_image", return_value=True):
            return shot_analyzer.classify_non_video_source(Path("x.MOV"), ffprobe="ffprobe")

    def test_only_completely_decodable_single_image_can_be_skipped(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "picture.MOV"
            Image.new("RGB", (90, 160), "red").save(path, format="JPEG")
            self.assertTrue(shot_analyzer._decodable_single_image(path))
            path.write_bytes(path.read_bytes()[:300])
            self.assertFalse(shot_analyzer._decodable_single_image(path))
            with mock.patch.object(shot_analyzer.subprocess, "run", return_value=_probe(JPEG_AS_MOV)):
                self.assertIsNone(shot_analyzer.classify_non_video_source(path, ffprobe="ffprobe"))

    def test_jpeg_thumbnail_named_mov_is_still_image(self):
        result = self.classify(JPEG_AS_MOV)
        self.assertEqual(result["reason"], "still_image")
        self.assertEqual(result["format_name"], "jpeg_pipe")

    def test_damaged_mjpeg_movie_header_stays_blocking(self):
        payload = {"streams": [{"codec_type": "video", "codec_name": "mjpeg", "width": 90, "height": 160}],
                   "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}
        self.assertIsNone(self.classify(payload))

    def test_audio_only_file_has_no_video_stream(self):
        result = self.classify({"streams": [{"codec_type": "audio", "codec_name": "aac"}],
                                "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "3.0"}})
        self.assertEqual(result["reason"], "no_video_stream")

    def test_probe_errors_and_ambiguous_answers_stay_blocking(self):
        self.assertIsNone(self.classify(JPEG_AS_MOV, returncode=1))          # NAS/IO error
        self.assertIsNone(self.classify(side_effect=subprocess.TimeoutExpired("ffprobe", 30)))
        self.assertIsNone(self.classify(side_effect=OSError("network path not found")))
        self.assertIsNone(self.classify({"streams": [], "format": {}}))       # nothing readable
        broken_video = {"streams": [{"codec_type": "video", "codec_name": "hevc"}],
                        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}
        self.assertIsNone(self.classify(broken_video))                       # real codec, no duration
        motion_jpeg = {"streams": [{"codec_type": "video", "codec_name": "mjpeg", "nb_frames": "120",
                                    "duration": "4.0"}], "format": {"format_name": "avi", "duration": "4.0"}}
        self.assertIsNone(self.classify(motion_jpeg))                        # MJPEG video is video
        self.assertIsNone(shot_analyzer.classify_non_video_source(Path("x.MOV"), ffprobe=None))

    def analyze(self, root, classify):
        good, bad = root / "good.MOV", root / "IMG_1747.MOV"
        good.write_bytes(b"v"); bad.write_bytes(b"jpeg")
        def probe(path, **kwargs):
            if Path(path).name == "good.MOV":
                return {"duration_us": 3_000_000, "source": "ffprobe_video_stream", "confidence": "HIGH"}
            return {"duration_us": 0, "source": "probe_failed", "confidence": "LOW"}
        with mock.patch.object(timing_contract, "probe_material_duration_us", side_effect=probe), \
                mock.patch.object(shot_analyzer, "detect_scene_boundaries", return_value=[]), \
                mock.patch.object(shot_analyzer, "extract_frame", return_value=True), \
                mock.patch.object(shot_analyzer, "classify_non_video_source", side_effect=classify):
            return shot_analyzer.analyze_manifest({"material_sources": [str(good), str(bad)]}, root,
                                                  root / "report", ffprobe="ffprobe")

    def test_still_image_source_is_a_warning_not_an_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.analyze(Path(tmp), lambda *a, **k: {"reason": "still_image", "format_name": "jpeg_pipe"})
        self.assertEqual(result["issues"], [])
        self.assertEqual([w["type"] for w in result["warnings"]], ["source_skipped_not_video"])
        self.assertFalse(result["warnings"][0]["blocking"])
        self.assertTrue(result["warnings"][0]["video"].endswith("IMG_1747.MOV"))
        self.assertEqual({Path(s["video"]).name for s in result["shots"]}, {"good.MOV"})
        self.assertTrue(result["ok"])

    def test_unclassified_probe_failure_still_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.analyze(Path(tmp), lambda *a, **k: None)
        self.assertEqual([i["type"] for i in result["issues"]], ["source_probe_failed"])
        self.assertEqual(result["warnings"], [])
        self.assertFalse(result["ok"])


class _ProductBase(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        env = {k: v for k, v in os.environ.items() if not k.startswith("JY_")}
        env.update(JY_VISION_CACHE_DIR=str(self.root / "cache"), JY_SLOT_DIR=str(self.root / "slots"),
                   JY_VISION_TEXT_PASS="0")
        self.stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        self.frame = self.root / "frame.jpg"
        self.frame.write_bytes(b"frame")
        self.videos = []
        for name in ("a.MOV", "b.MOV"):
            path = self.root / name
            path.write_bytes(name.encode())
            self.videos.append(path)
        self.manifest = {"product_id": "p", "product": "prod", "material_sources": [str(v) for v in self.videos]}

    def raw(self, manifest, base, report, **kwargs):
        frame = report / "frame.jpg"
        frame.parent.mkdir(parents=True, exist_ok=True)
        frame.write_bytes(b"frame")
        return {"ok": True, "issues": [], "warnings": [], "shots": [{
            "shot_id": "source_001_shot_001", "video": manifest["material_sources"][0],
            "source_start": 0, "source_end": 3.0, "frame_exists": True, "frame_path": str(frame),
            "frame_paths": [str(frame)], "status": "pending_analysis"}]}


class NonVideoProductTests(_ProductBase):
    def test_product_ready_with_skipped_non_video_source(self):
        def raw(manifest, base, report, **kwargs):
            if manifest["material_sources"][0].endswith("b.MOV"):
                return {"ok": False, "shots": [], "issues": [], "warnings": [{
                    "type": "source_skipped_not_video", "video": manifest["material_sources"][0],
                    "blocking": False, "reason": "still_image"}]}
            return self.raw(manifest, base, report)
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=raw):
            result = material_cache.analyze_product(self.manifest, self.root, self.root / "r", vision=False)
        self.assertTrue(result["material_cache"]["ready"])
        self.assertEqual(result["material_cache"]["sources_skipped_not_video"], 1)
        self.assertEqual(result["issues"], [])
        self.assertEqual(len(result["shots"]), 1)
        self.assertEqual(result["warnings"][0]["type"], "source_skipped_not_video")

    def test_missing_or_unreadable_source_still_blocks(self):
        def raw(manifest, base, report, **kwargs):
            if manifest["material_sources"][0].endswith("b.MOV"):
                return {"ok": False, "shots": [], "issues": [{"type": "source_probe_failed",
                                                              "video": manifest["material_sources"][0]}]}
            return self.raw(manifest, base, report)
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=raw):
            result = material_cache.analyze_product(self.manifest, self.root, self.root / "r", vision=False)
        self.assertFalse(result["material_cache"]["ready"])
        self.videos[1].unlink()
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=self.raw):
            result = material_cache.analyze_product(self.manifest, self.root, self.root / "r2", vision=False)
        self.assertFalse(result["material_cache"]["ready"])
        self.assertEqual(result["issues"][0]["type"], "source_missing")


GOOD = {"description": "手拿包装正面朝镜头展示", "visual_tags": ["包装", "手部"], "evidence_tags": ["完整展示包装"],
        "action_complete": True, "head_waste": 0.0, "tail_waste": 0.0, "evidence_time": 1.0,
        "evidence_intervals": [{"start": 0.5, "end": 2.0}], "action_segments": [], "text_tiles": [],
        "confidence": 0.9, "action": "举起", "object": "包装", "result": None, "role": "product_display",
        "evidence_strength": "B", "action_phase": "static", "setup_id": "front", "has_subject": True,
        "readable_claims": [], "pointing_to_text": False, "pointing_action": None, "gesture": None}
BLACK = {**GOOD, "description": "该镜头对应的所有帧均为黑色填充格，未显示任何有效画面内容。",
         "visual_tags": [], "evidence_tags": [], "action_complete": False, "confidence": 1.0}
BAD_ID = "source_002_shot_001"


class UnauditableReaskTests(_ProductBase):
    def run_product(self, answers, report="r"):
        """answers: successive answers for asks that include BAD_ID ("FAIL" = transport failure)."""
        calls = []
        def respond(sheet, prompt, output, **kwargs):
            ids = [sid for sid in ("source_001_shot_001", BAD_ID) if f"shot_id={sid}" in prompt]
            calls.append({"ids": ids, "reask": "单独复核" in prompt, "output": Path(output).name})
            rows = []
            for sid in ids:
                if sid == BAD_ID:
                    asked = len([c for c in calls if BAD_ID in c["ids"]])
                    answer = answers[min(asked - 1, len(answers) - 1)]
                    if answer == "FAIL":
                        return False, "vision request failed [HTTP_RATE_LIMIT]"
                    rows.append({"shot_id": sid, **answer})
                else:
                    rows.append({"shot_id": sid, **GOOD})
            return True, json.dumps({"shots": rows}, ensure_ascii=False)
        with mock.patch.object(shot_analyzer, "analyze_manifest", side_effect=self.raw), \
                mock.patch.object(vision, "_sample_frames", return_value=[{
                    "path": str(self.frame), "timestamp": 1.0, "suffix": "t1"}]), \
                mock.patch.object(vision, "_orientation", return_value="portrait"), \
                mock.patch.object(vision, "_write_sheet", return_value=True), \
                mock.patch.object(vision, "_run_vision_with_retry", side_effect=respond):
            result = material_cache.analyze_product(self.manifest, self.root, self.root / report)
        return result, calls

    def cache(self):
        path = vision._vision_cache_path(self.root / "r")
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def bad_row(self, result):
        return next(r for r in result["shots"] if r["shot_id"] == BAD_ID)

    def test_unauditable_answer_is_reasked_alone_and_recovers(self):
        result, calls = self.run_product([BLACK, GOOD])
        self.assertTrue(result["material_cache"]["ready"])
        self.assertEqual([c["reask"] for c in calls], [False, True])
        self.assertEqual(calls[1]["ids"], [BAD_ID])               # single-shot sheet
        self.assertTrue(calls[1]["output"].startswith("vision_reask1_"))
        self.assertEqual(self.bad_row(result)["status"], "ready_for_matching")
        self.assertEqual(result["vision_reask"]["shots"][0]["outcome"], "recovered")
        self.assertFalse(result.get("warnings"))
        self.assertEqual(result["material_cache"]["shots_excluded_unauditable"], [])

    def test_unauditable_answer_is_never_cached_as_valid(self):
        result, calls = self.run_product([BLACK, BLACK, BLACK])
        self.assertEqual(len(calls), 3)                            # batch + 2 bounded re-asks
        key = self.bad_row(result)["analysis_cache_key"]
        self.assertNotIn(key, self.cache())
        good_key = next(r for r in result["shots"] if r["shot_id"] != BAD_ID)["analysis_cache_key"]
        self.assertIn(good_key, self.cache())
        # The next product pass asks again instead of reusing the empty answer.
        again, calls2 = self.run_product([GOOD], report="r2")
        self.assertEqual([c["ids"] for c in calls2], [[BAD_ID]])
        self.assertTrue(again["material_cache"]["ready"])
        self.assertNotIn("excluded_from_pool", self.bad_row(again))

    def test_still_unauditable_after_reasks_is_excluded_with_warning(self):
        result, _ = self.run_product([BLACK, BLACK, BLACK])
        row = self.bad_row(result)
        self.assertTrue(row["excluded_from_pool"])
        self.assertEqual(row["status"], "pending_analysis")
        self.assertEqual(result["warnings"][0]["type"], "shot_skipped_unauditable")
        self.assertFalse(result["warnings"][0]["blocking"])
        self.assertEqual(result["material_cache"]["shots_excluded_unauditable"], [BAD_ID])
        self.assertTrue(result["material_cache"]["ready"])

    def test_reask_transport_failure_keeps_blocking(self):
        result, calls = self.run_product([BLACK, "FAIL"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["material_cache"]["ready"])
        self.assertNotIn("excluded_from_pool", self.bad_row(result))
        self.assertEqual(result["vision_reask"]["shots"][0]["outcome"], "call_failed")

    def test_reask_disabled_keeps_blocking(self):
        os.environ["JY_VISION_REASK_LIMIT"] = "0"
        result, calls = self.run_product([BLACK, GOOD])
        self.assertEqual(len(calls), 1)
        self.assertFalse(result["material_cache"]["ready"])

    def test_flag_without_warning_does_not_relax_readiness(self):
        analysis = {"shots": [{"shot_id": "s1", "excluded_from_pool": True, "status": "pending_analysis"}],
                    "warnings": []}
        self.assertEqual(vision.logged_pool_exclusions(analysis), set())
        analysis["warnings"] = [{"type": "shot_skipped_unauditable", "shot_id": "s1"}]
        self.assertEqual(vision.logged_pool_exclusions(analysis), {"s1"})

    def test_exclusion_budget_is_bounded(self):
        self.assertEqual(vision._reask_max_shots(10), 1)
        self.assertEqual(vision._reask_max_shots(652), 6)
        self.assertEqual(vision._reask_max_shots(100000), 24)
        os.environ["JY_VISION_REASK_LIMIT"] = "9"
        self.assertEqual(vision._reask_limit(), 2)


if __name__ == "__main__":
    unittest.main()
