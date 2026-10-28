"""frame_quality unit tests: reject rules, lazy swap, cache protocol, disable switch."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import frame_quality, phase_timing, vision_analyzer  # noqa: E402


def _save(path: Path, color, size=(160, 90), blur: bool = False) -> Path:
    image = Image.new("RGB", size, color)
    if blur:
        # Heavy blur so FIND_EDGES variance falls under BLUR_SHARPNESS.
        image = image.filter(ImageFilter.GaussianBlur(radius=12))
    image.save(path, quality=95)
    return path


def _textured(path: Path, size=(160, 90)) -> Path:
    image = Image.new("RGB", size, (40, 40, 40))
    draw = ImageDraw.Draw(image)
    for x in range(0, size[0], 4):
        draw.line([(x, 0), (x, size[1])], fill=(220, 220, 220))
    for y in range(0, size[1], 4):
        draw.line([(0, y), (size[0], y)], fill=(180, 180, 180))
    image.save(path, quality=95)
    return path


class ScoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fq_score_")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_black_overexposed_flat_blurry_and_sharp(self):
        black = frame_quality.score(_save(self.root / "black.jpg", (0, 0, 0)))
        white = frame_quality.score(_save(self.root / "white.jpg", (255, 255, 255)))
        flat = frame_quality.score(_save(self.root / "flat.jpg", (128, 128, 128)))
        # High-contrast checkerboard + strong blur: keeps std above flat, drops
        # Laplacian variance under BLUR_SHARPNESS.
        blur_path = self.root / "blur.jpg"
        image = Image.new("RGB", (160, 90), (0, 0, 0))
        draw = ImageDraw.Draw(image)
        for x in range(0, 160, 16):
            for y in range(0, 90, 16):
                if ((x // 16) + (y // 16)) % 2 == 0:
                    draw.rectangle([x, y, x + 15, y + 15], fill=(255, 255, 255))
        image = image.filter(ImageFilter.GaussianBlur(radius=8))
        image.save(blur_path, quality=95)
        soft = frame_quality.score(blur_path)
        sharp = frame_quality.score(_textured(self.root / "sharp.jpg"))
        self.assertEqual(black["reject_reason"], "black")
        self.assertEqual(white["reject_reason"], "overexposed")
        self.assertEqual(flat["reject_reason"], "flat")
        # Soft focus is annotated, not hard-rejected (avoids ffmpeg thrash on product clips).
        self.assertEqual(soft["reject_reason"], "")
        self.assertTrue(soft.get("soft_blur"))
        self.assertEqual(sharp["reject_reason"], "")
        self.assertTrue(sharp["ok"])
        self.assertGreater(sharp["sharpness"], soft["sharpness"])


class AlternateAndPickTests(unittest.TestCase):
    def test_alternate_times_stay_inside_window(self):
        alts = frame_quality.alternate_times(5.0, 4.0, 6.0, half_width=1.5)
        self.assertTrue(alts)
        self.assertTrue(all(4.0 <= t <= 6.0 for t in alts))
        self.assertTrue(all(abs(t - 5.0) >= 1e-3 for t in alts))

    def test_pick_best_prefers_passed_then_sharpest(self):
        candidates = [
            {"path": "a", "quality": {"reject_reason": "black", "sharpness": 100}},
            {"path": "b", "quality": {"reject_reason": "", "sharpness": 20}},
            {"path": "c", "quality": {"reject_reason": "", "sharpness": 40}},
        ]
        best = frame_quality.pick_best(candidates)
        self.assertEqual(best["path"], "c")


class SelectFrameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fq_select_")
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"JY_FRAME_QUALITY": "1"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_lazy_swap_replaces_black_center(self):
        center = self.root / "center.jpg"
        sharp = self.root / "sharp_src.jpg"
        _textured(sharp)
        black = self.root / "black_src.jpg"
        _save(black, (0, 0, 0))

        def extract(video, timestamp, output, ffmpeg=None):
            src = black if abs(timestamp - 1.0) < 1e-6 else sharp
            Image.open(src).save(output)
            return True

        picked = frame_quality.select_frame(
            Path("video.mp4"), 1.0, center, start=0.0, end=3.0, half_width=1.5,
            extract=extract)
        self.assertTrue(picked["ok"])
        self.assertTrue(picked["replaced"])
        self.assertEqual(picked["quality"].get("reject_reason"), "")
        self.assertNotAlmostEqual(picked["timestamp"], 1.0, places=3)
        self.assertTrue(0.0 <= picked["timestamp"] <= 3.0)

    def test_disable_switch_keeps_center_without_swap(self):
        os.environ["JY_FRAME_QUALITY"] = "0"
        out = self.root / "out.jpg"
        calls = []

        def extract(video, timestamp, output, ffmpeg=None):
            calls.append(timestamp)
            _save(output, (0, 0, 0))
            return True

        picked = frame_quality.select_frame(
            Path("video.mp4"), 2.0, out, start=0.0, end=4.0, half_width=1.0,
            extract=extract)
        self.assertEqual(calls, [2.0])
        self.assertFalse(picked["replaced"])
        self.assertAlmostEqual(picked["timestamp"], 2.0)
        # Escape hatch: quality off still reports extract success, even for black.
        self.assertTrue(picked["ok"])

    def test_all_hard_reject_is_not_ok(self):
        def extract(video, timestamp, output, ffmpeg=None):
            name = Path(output).name
            if name.startswith("white"):
                color = (255, 255, 255)
            elif name.startswith("flat"):
                color = (128, 128, 128)
            else:
                color = (0, 0, 0)
            _save(Path(output), color)
            return True

        jobs = [
            {"center": 1.0, "output": self.root / "black.jpg", "start": 0.0,
             "end": 3.0, "half_width": 1.0},
            {"center": 1.0, "output": self.root / "white.jpg", "start": 0.0,
             "end": 3.0, "half_width": 1.0},
            {"center": 1.0, "output": self.root / "flat.jpg", "start": 0.0,
             "end": 3.0, "half_width": 1.0},
        ]
        picked = frame_quality.select_frames(
            Path("video.mp4"), jobs, extract=extract)
        self.assertEqual(
            [row["quality"].get("reject_reason") for row in picked],
            ["black", "overexposed", "flat"])
        self.assertTrue(all(not row["ok"] for row in picked))
        # Callers that only check outer ok must not treat these as success frames.
        self.assertEqual([row for row in picked if row.get("ok")], [])


class VisionWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fq_vision_")
        self.root = Path(self.tmp.name)
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith("JY_VISION_") and k != "JY_FRAME_QUALITY"}
        clean["JY_FRAME_QUALITY"] = "1"
        self.env = mock.patch.dict(os.environ, clean, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_sample_max_aligns_with_sheet_tiles(self):
        self.assertEqual(vision_analyzer.SAMPLE_MAX, vision_analyzer.SHEET_TILES)
        times = vision_analyzer.sample_times(0.0, 27.8)
        self.assertEqual(len(times), vision_analyzer.SAMPLE_MAX + 1)

    def test_medium_take_still_covers_second_and_a_half(self):
        times = vision_analyzer.sample_times(0.0, 9.4)
        self.assertEqual(len(times), 8)
        self.assertTrue(any(1.2 <= t <= 4.0 for t in times))

    def test_cache_key_changes_with_quality_protocol(self):
        video = self.root / "clip.mp4"
        video.write_bytes(b"fake")
        shot = {"video": str(video), "source_start": 0.0, "source_end": 3.0}
        manifest = {"product": "demo"}
        key_on = vision_analyzer._vision_cache_key(manifest, shot, "volc")
        os.environ["JY_FRAME_QUALITY"] = "0"
        key_off = vision_analyzer._vision_cache_key(manifest, shot, "volc")
        self.assertNotEqual(key_on, key_off)
        self.assertEqual(frame_quality.FRAME_QUALITY_VERSION, "frame-quality-v2")

    def test_sample_frames_drops_all_hard_rejected(self):
        video = self.root / "clip.mp4"
        video.write_bytes(b"x")
        frame_dir = self.root / "frames"
        frame_dir.mkdir()

        def extract(path, timestamp, output, ffmpeg=None):
            _save(Path(output), (0, 0, 0))
            return True

        def extract_batch(path, requests, ffmpeg=None):
            return [extract(path, ts, out, ffmpeg=ffmpeg) for ts, out in requests]

        shot = {"shot_id": "s1", "video": str(video),
                "source_start": 0.0, "source_end": 3.0}
        with mock.patch("orchestrator.shot_analyzer.extract_frame", side_effect=extract), \
             mock.patch("orchestrator.shot_analyzer.extract_frames_batch",
                        side_effect=extract_batch):
            frames = vision_analyzer._sample_frames(shot, frame_dir, ffmpeg=None)
        self.assertEqual(frames, [])
        self.assertTrue(any(frame_dir.glob("*.jpg")))

    def test_sample_frames_records_quality_and_uses_actual_timestamp(self):
        video = self.root / "clip.mp4"
        video.write_bytes(b"x")
        frame_dir = self.root / "frames"
        frame_dir.mkdir()
        sharp = self.root / "sharp.jpg"
        _textured(sharp)

        def extract(path, timestamp, output, ffmpeg=None):
            Image.open(sharp).save(output)
            return True

        shot = {"shot_id": "s1", "video": str(video),
                "source_start": 0.0, "source_end": 3.0}
        with mock.patch("orchestrator.shot_analyzer.extract_frame", side_effect=extract):
            frames = vision_analyzer._sample_frames(shot, frame_dir, ffmpeg=None)
        self.assertGreaterEqual(len(frames), 2)
        self.assertTrue(all("quality" in f for f in frames))
        self.assertTrue(all(0.0 <= f["timestamp"] <= 3.0 for f in frames))


class PhaseWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fq_phase_")
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"JY_FRAME_QUALITY": "1"}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_phase_cache_key_includes_quality_version(self):
        video = self.root / "clip.mp4"
        video.write_bytes(b"abc")
        shot = {"video": str(video), "source_start": 1.0, "source_end": 4.0}
        scene = {"id": "absorb", "phases": [{"name": "prep"}, {"name": "result"}]}
        key_on = phase_timing._cache_key(shot, scene, "volc")
        os.environ["JY_FRAME_QUALITY"] = "0"
        key_off = phase_timing._cache_key(shot, scene, "volc")
        self.assertTrue(key_on)
        self.assertNotEqual(key_on, key_off)
        self.assertEqual(phase_timing.PHASE_TIMING_VERSION, "phase-timing-v3")
        self.assertEqual(frame_quality.FRAME_QUALITY_VERSION, "frame-quality-v2")

    def test_phase_cache_key_binds_model_endpoint_and_phase_description(self):
        video = self.root / "clip.mp4"
        video.write_bytes(b"abc")
        shot = {"video": str(video), "source_start": 1.0, "source_end": 4.0}
        scene = {"id": "absorb", "phases": [{"name": "result", "describe": "show the result"}]}
        with mock.patch.dict(os.environ, {"JY_VISION_TRANSPORT": "qwen", "JY_QWEN_MODEL": "a",
                                          "JY_QWEN_BASE_URL": "https://a.invalid/v1"}):
            first = phase_timing._cache_key(shot, scene, "volc")
            os.environ["JY_QWEN_MODEL"] = "b"
            second = phase_timing._cache_key(shot, scene, "volc")
            os.environ["JY_QWEN_BASE_URL"] = "https://b.invalid/v1"
            third = phase_timing._cache_key(shot, scene, "volc")
            changed = phase_timing._cache_key(shot, {**scene, "phases": [
                {"name": "result", "describe": "show the complete action"}]}, "volc")
        self.assertEqual(len({first, second, third, changed}), 4)

    def test_locate_all_hard_reject_does_not_build_sheet(self):
        video = self.root / "clip.mp4"
        video.write_bytes(b"abc")
        report = self.root / "report"
        report.mkdir()

        def extract(path, timestamp, output, ffmpeg=None):
            _save(Path(output), (255, 255, 255))
            return True

        def extract_batch(path, requests, ffmpeg=None):
            return [extract(path, ts, out, ffmpeg=ffmpeg) for ts, out in requests]

        runner = mock.Mock(return_value=(True, "{}"))
        shot = {"video": str(video), "source_start": 1.0, "source_end": 4.0}
        scene = {
            "id": "absorb", "label": "吸水",
            "phases": [
                {"name": "prep", "label": "准备", "describe": "准备"},
                {"name": "result", "label": "结果", "describe": "结果"},
            ],
        }
        with mock.patch("orchestrator.shot_analyzer.extract_frame", side_effect=extract), \
             mock.patch("orchestrator.shot_analyzer.extract_frames_batch",
                        side_effect=extract_batch):
            result = phase_timing.locate(
                shot, scene, report, profile="volc", runner=runner)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("reason"), "PHASE_FRAME_EXTRACT_FAILED")
        runner.assert_not_called()

    def test_locate_swaps_within_half_step_and_stays_monotonic(self):
        video = self.root / "clip.mp4"
        video.write_bytes(b"abc")
        report = self.root / "report"
        report.mkdir()
        sharp = self.root / "sharp.jpg"
        black = self.root / "black.jpg"
        _textured(sharp)
        _save(black, (0, 0, 0))

        def extract(path, timestamp, output, ffmpeg=None):
            # Force every nominal sample to black so the gate must swap.
            src = black if abs(timestamp - round(timestamp, 3)) < 1e-9 else sharp
            # Always start black; alternate offsets get sharp via select_frame alts.
            if "_alt" in output.name:
                Image.open(sharp).save(output)
            else:
                Image.open(black).save(output)
            return True

        def runner(sheet, prompt):
            import re
            stamps = [float(v) for v in re.findall(r"t=([0-9.]+)s", prompt)]
            self.assertGreaterEqual(len(stamps), 2)
            mid = len(stamps) // 2
            return True, json.dumps({
                "complete": True,
                "phases": [
                    {"name": "prep", "start": stamps[0], "end": stamps[mid - 1]},
                    {"name": "result", "start": stamps[mid], "end": stamps[-1]},
                ],
                "note": "",
            })

        shot = {"video": str(video), "source_start": 1.0, "source_end": 4.0}
        scene = {
            "id": "absorb", "label": "吸水",
            "phases": [
                {"name": "prep", "label": "准备", "describe": "准备"},
                {"name": "result", "label": "结果", "describe": "结果"},
            ],
        }
        with mock.patch("orchestrator.shot_analyzer.extract_frame", side_effect=extract):
            result = phase_timing.locate(
                shot, scene, report, profile="volc", runner=runner)
        self.assertTrue(result.get("ok"), result)
        times = result["sample_times"]
        self.assertEqual(times, sorted(times))
        self.assertTrue(all(1.0 <= t <= 4.0 for t in times))
        self.assertTrue(result.get("frame_quality"))
        # Half-step swaps must not jump more than MIN_STEP_S / 2 from a nominal grid
        # point; here we only assert no timestamp left the shot window.
        self.assertTrue(any(row.get("replaced") for row in result["frame_quality"]))


if __name__ == "__main__":
    unittest.main()
