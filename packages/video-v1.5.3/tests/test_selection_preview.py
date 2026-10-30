"""Diagnostic selection overrides must never alter the other timeline tracks."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools import selection_preview as preview


class SelectionPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="selection preview ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        self.video, self.voice = self.media / "video.mp4", self.media / "voice.wav"
        self.video.write_bytes(b"synthetic media identity")
        self.voice.write_bytes(b"synthetic audio identity")
        self.draft = {"duration": 2_000_000, "materials": {
            "videos": [{"id": "video", "path": str(self.video)}],
            "audios": [{"id": "voice", "path": str(self.voice)}],
            "texts": [{"id": "text", "content": json.dumps({"text": "One {test}\nTwo", "styles": [{"size": 8, "bold": True}]})}]},
            "tracks": [{"type": "video", "segments": [{"material_id": "video", "volume": 0,
                "target_timerange": {"start": 0, "duration": 2_000_000},
                "source_timerange": {"start": 0, "duration": 2_000_000}, "speed": 1}]},
                {"name": "Narration", "type": "audio", "segments": [{"material_id": "voice", "volume": 1,
                 "target_timerange": {"start": 0, "duration": 2_000_000},
                 "source_timerange": {"start": 0, "duration": 2_000_000}}]},
                {"type": "text", "segments": [{"material_id": "text", "target_timerange": {"start": 0, "duration": 2_000_000},
                  "clip": {"transform": {"x": 0, "y": -.55}}}]}]}

    def overrides(self):
        return {"overrides": [{"index": 1, "reason": "Preserve result, compress process",
            "parts": [{"source": str(self.video), "source_start_us": 0, "source_end_us": 2_000_000,
                       "segment_us": 1_000_000, "speed": 2},
                      {"source": str(self.video), "source_start_us": 2_000_000, "source_end_us": 3_000_000,
                       "segment_us": 1_000_000, "speed": 1}]}]}

    def test_overrides_change_video_only_and_preserve_exact_sentence_slot(self):
        original = copy.deepcopy(self.draft)
        rows = preview.video_rows(self.draft, self.root, self.overrides())
        self.assertEqual(self.draft, original)
        self.assertEqual([(row["target_start_us"], row["target_end_us"], row["speed"]) for row in rows],
                         [(0, 1_000_000, 2), (1_000_000, 2_000_000, 1)])
        self.assertEqual(preview._audio_rows(self.draft, self.root), preview._audio_rows(original, self.root))
        self.assertEqual(preview.subtitle_text(self.draft, 540, 960), preview.subtitle_text(original, 540, 960))

    def test_duration_change_and_invalid_source_speed_are_rejected(self):
        for key, value in (("segment_us", 999_000), ("speed", 9), ("speed", float("nan")),
                           ("source_start_us", -1), ("source_end_us", 0)):
            overrides = self.overrides()
            overrides["overrides"][0]["parts"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                preview.video_rows(self.draft, self.root, overrides)

    def test_unknown_or_duplicate_override_and_missing_reason_are_rejected(self):
        for modifier in (lambda rows: rows[0].update(index=2),
                         lambda rows: rows.append(copy.deepcopy(rows[0])),
                         lambda rows: rows[0].update(reason="")):
            overrides = self.overrides()
            modifier(overrides["overrides"])
            with self.assertRaises(ValueError):
                preview.video_rows(self.draft, self.root, overrides)

    def test_gaps_and_unmuted_override_audio_cannot_be_silently_changed(self):
        self.draft["tracks"][0]["segments"][0]["target_timerange"]["start"] = 1
        with self.assertRaisesRegex(ValueError, "GAP_OR_OVERLAP"):
            preview.video_rows(self.draft, self.root)
        self.draft["tracks"][0]["segments"][0]["target_timerange"]["start"] = 0
        self.draft["tracks"][0]["segments"][0]["volume"] = .5
        with self.assertRaisesRegex(ValueError, "NATIVE_AUDIO"):
            preview.video_rows(self.draft, self.root, self.overrides())

    def test_subtitle_position_and_escaping_preserve_original_content(self):
        text = preview.subtitle_text(self.draft, 540, 960)
        self.assertIn(r"\pos(270.00,744.00)", text)
        self.assertIn(r"One \{test\}\NTwo", text)
        self.assertIn("0:00:00.00,0:00:02.00", text)

    def test_native_saved_zero_start_can_be_omitted(self):
        for track in self.draft["tracks"]:
            track["segments"][0]["target_timerange"].pop("start")
        self.assertEqual(preview.video_rows(self.draft, self.root)[0]["target_start_us"], 0)
        self.assertEqual(preview._audio_rows(self.draft, self.root)[0]["target_start_us"], 0)
        self.assertIn("0:00:00.00,0:00:02.00", preview.subtitle_text(self.draft, 540, 960))

    def test_fallback_media_and_missing_source(self):
        self.assertEqual(preview.resolve_media("Z:/old/video.mp4", self.root), self.video.resolve())
        with self.assertRaisesRegex(ValueError, "MEDIA_MISSING"):
            preview.resolve_media("missing.mov", self.root)

    def test_atempo_handles_native_reference_high_speeds(self):
        self.assertEqual(preview.atempo(8), "atempo=2,atempo=2,atempo=2")
        self.assertEqual(preview.atempo(.25), "atempo=0.5,atempo=0.5")

    def test_subframe_part_is_rejected_even_when_rounding_would_give_one_frame(self):
        plan = self.overrides()
        plan["overrides"][0]["parts"] = [
            {"source": str(self.video), "source_start_us": 0, "source_end_us": 12_000,
             "segment_us": 12_000, "speed": 1},
            {"source": str(self.video), "source_start_us": 12_000, "source_end_us": 17_000,
             "segment_us": 5_000, "speed": 1},
            {"source": str(self.video), "source_start_us": 17_000, "source_end_us": 2_000_000,
             "segment_us": 1_983_000, "speed": 1}]
        # The 5 ms protected tail crosses a round-to-frame boundary and would
        # otherwise become a full 33.3 ms frame despite its shorter duration.
        self.assertEqual(round(.017 * 30) - round(.012 * 30), 1)
        path = self.root / "timeline.json"
        path.write_text(json.dumps(self.draft), encoding="utf-8")
        output = self.root.parent / (self.root.name + "-subframe")
        with self.assertRaisesRegex(ValueError, "PART_SHORTER_THAN_OUTPUT_FRAME"):
            preview.render(path, output, overrides=plan, sheets=False)
        self.assertFalse(output.exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_continuous_source_retiming_keeps_sentence_frames_and_final_result(self):
        # Source time is continuous across six parts; only the process is 2x.
        # The green result occurs at the very tail and must survive rendering.
        subprocess.run(["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
            "color=c=red:s=90x160:d=3.2:r=30,drawbox=c=lime:t=fill:enable='gte(t,2.94)'",
            "-c:v", "libx264", "-threads", "1", "-y", str(self.video)],
            check=True, capture_output=True, timeout=20)
        self.draft["tracks"] = self.draft["tracks"][:1]
        first = self.draft["tracks"][0]["segments"][0]
        first["target_timerange"]["duration"] = 1_000_000
        first["source_timerange"]["duration"] = 1_551_000
        first["speed"] = 1.551
        second = copy.deepcopy(first)
        second["target_timerange"]["start"] = 1_000_000
        second["source_timerange"]["start"] = 1_551_000
        self.draft["tracks"][0]["segments"].append(second)
        plan = {"overrides": []}
        for index in (1, 2):
            cursor = (index - 1) * 1_551_000
            parts = []
            for duration, speed in ((217_000, 1), (551_000, 2), (232_000, 1)):
                end = cursor + duration * speed
                parts.append({"source": str(self.video), "source_start_us": cursor,
                    "source_end_us": end, "segment_us": duration, "speed": speed})
                cursor = end
            plan["overrides"].append({"index": index, "reason": "Keep protected contact and result",
                                      "parts": parts})
        path = self.root / "timeline.json"
        path.write_text(json.dumps(self.draft), encoding="utf-8")
        output = self.root.parent / (self.root.name + "-continuous")
        self.addCleanup(shutil.rmtree, output, True)
        result = preview.render(path, output, overrides=plan, width=90, height=160, sheets=False)
        for index in (1, 2):
            rows = [r for r in result["video_parts"] if r["index"] == index]
            self.assertEqual([r["frames"] for r in rows], [7, 16, 7])
            self.assertEqual(sum(r["frames"] for r in rows), 30)
        probe = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=nb_read_frames", "-of", "json", result["output"]],
            capture_output=True, check=True, timeout=20)
        self.assertEqual(int(json.loads(probe.stdout)["streams"][0]["nb_read_frames"]), 60)
        pixels = subprocess.run(["ffmpeg", "-v", "error", "-i", result["output"], "-vf",
            "select=eq(n\\,59)", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True, timeout=20).stdout
        self.assertEqual(len(pixels), 90 * 160 * 3)
        self.assertGreater(sum(pixels[1::3]) / (90 * 160), 150)
        self.assertLess(sum(pixels[0::3]) / (90 * 160), 70)
        self.assertLess(sum(pixels[2::3]) / (90 * 160), 70)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_real_render_decodes_and_keeps_source_files_unchanged(self):
        for command in (["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                         "color=c=blue:s=90x160:d=3:r=30", "-c:v", "libx264", "-threads", "1", "-y", str(self.video)],
                        ["ffmpeg", "-hide_banner", "-v", "error", "-f", "lavfi", "-i",
                         "sine=f=440:d=2", "-y", str(self.voice)]):
            subprocess.run(command, check=True, capture_output=True, timeout=20)
        path = self.root / "timeline.json"
        path.write_text(json.dumps(self.draft), encoding="utf-8")
        before = path.read_bytes(), self.voice.read_bytes()
        result = preview.render(path, self.root.parent / (self.root.name + "-output"),
                                overrides=self.overrides(), width=90, height=160, sheets=False)
        self.addCleanup(shutil.rmtree, Path(result["output"]).parent)
        self.assertEqual((path.read_bytes(), self.voice.read_bytes()), before)
        self.assertTrue(result["full_decode"])
        self.assertFalse(result["native_export"])
        self.assertLessEqual(abs(result["actual_duration_s"] - 2), 1 / 30)
        with self.assertRaisesRegex(ValueError, "NEW_AND_OUTSIDE"):
            preview.render(path, self.root / "would-change-draft")


if __name__ == "__main__":
    unittest.main()
