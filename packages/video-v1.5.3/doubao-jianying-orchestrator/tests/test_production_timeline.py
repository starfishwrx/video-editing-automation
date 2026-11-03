from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.application.production import prepare_timeline
from orchestrator.audio.final_assets import prepare_narration
from orchestrator.domain.clips import Clip
from orchestrator.domain.timeline import EditTimeline, TimelineError
from orchestrator.quality.production_qc import check_production
from test_timeline_render import FFMPEG, FFPROBE, make_media

PACKAGE = Path(__file__).resolve().parents[2]


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/ffprobe required")
class ProductionTimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.video, long_voice, _ = make_media(self.root)
        self.voice = self.root / "short.wav"
        with wave.open(str(long_voice), "rb") as source, wave.open(str(self.voice), "wb") as target:
            target.setparams(source.getparams())
            target.writeframes(source.readframes(28800))
        self.manifest = {"task_id": "fixed-demo", "width": 160, "height": 240, "fps": 30,
                         "segments": [{"video": str(self.video), "audio": str(self.voice), "text": "same text"}],
                         "subtitle_style": {"font_name": "Arial", "font_size_px": 16}}

    def prepare(self, manifest=None, **kwargs):
        return prepare_timeline(manifest or self.manifest, self.root, self.root / "new" / "report",
                                ffmpeg=FFMPEG, ffprobe=FFPROBE, **kwargs)

    def test_actual_audio_overrides_estimated_duration_and_repeated_text_is_distinct(self):
        writer_before = sys.modules.get("jy_wrapper")
        m = dict(self.manifest)
        m["segments"] = [{**self.manifest["segments"][0], "duration": 100, "timing_certainty": "estimated"}] * 2
        with patch("orchestrator.runtime.platform_env.find_drafts_root", side_effect=AssertionError("draft lookup")), \
             patch("orchestrator.runtime.platform_env.find_jianying_editor_root", side_effect=AssertionError("writer lookup")):
            t = self.prepare(m)
        self.assertEqual(t.duration_us, 1_200_000)
        self.assertEqual([s.sentence_id for s in t.sentences], ["sentence-0001", "sentence-0002"])
        self.assertEqual(len(t.subtitles), 2)
        self.assertEqual(len(t.audios), 2)
        self.assertIs(sys.modules.get("jy_wrapper"), writer_before)
        self.assertTrue((self.root / "new/report/timeline.json").is_file())

    def test_clip_adapter_preserves_multishot_one_narration_and_raw_sentence_index(self):
        clips = [Clip(str(self.video), 0, .3, text="same text", audio=str(self.voice), audio_mode="file",
                      parent_index=0, raw_index=6, start_us=0),
                 Clip(str(self.video), .3, .3, text="same text", audio=str(self.voice), audio_mode="file",
                      parent_index=0, raw_index=6, start_us=300_000)]
        t = self.prepare(clips=clips)
        self.assertEqual(len(t.audios), 1)
        self.assertEqual(len(t.subtitles), 1)
        self.assertEqual(t.sentences[0].sentence_id, "sentence-0007")
        self.assertEqual([v.sentence_ids for v in t.videos], [("sentence-0007",)] * 2)

    def test_changed_final_audio_blocks_old_fixed_span_lengths(self):
        m = dict(self.manifest)
        m["segments"] = [{**m["segments"][0], "span_parts": [
            {"video": str(self.video), "segment_s": .4}, {"video": str(self.video), "segment_s": .4}]}]
        with self.assertRaisesRegex(TimelineError, "AUDIO_REPLAN_REQUIRED"):
            self.prepare(m)

    def test_window_solution_speed_alias_is_preserved_for_each_span(self):
        m = dict(self.manifest)
        m["segments"] = [{**m["segments"][0], "video_speed": 1.0, "source_start": 2.0,
                          "source_end": 2.5, "duration_us": 600_000, "span_parts": [
            {"video": str(self.video), "segment_s": .3, "source_start": 0, "source_end": .6, "speed": 2},
            {"video": str(self.video), "segment_s": .3, "source_start": .6, "source_end": 1.2, "speed": 2}]}]
        t = self.prepare(m)
        self.assertEqual([v.speed_num for v in t.videos], [2, 2])
        self.assertEqual([v.source_start_us for v in t.videos], [0, 600_000])

    def test_segment_level_unsupported_effects_cannot_disappear(self):
        for feature in ("brightness", "filters", "adjustments", "crop", "video_effects"):
            m = {**self.manifest, "segments": [{**self.manifest["segments"][0], feature: .2}]}
            with self.subTest(feature=feature), self.assertRaisesRegex(TimelineError, "FEATURE_UNSUPPORTED"):
                self.prepare(m)

    def test_existing_report_is_not_a_concurrent_write_target(self):
        self.prepare()
        with self.assertRaisesRegex(TimelineError, "REPORT_EXISTS"):
            self.prepare()

    def test_clip_does_not_silently_change_fixed_target_or_original_solution(self):
        clip = Clip(str(self.video), 0, .5, text="same text", audio=str(self.voice), audio_mode="file")
        with self.assertRaisesRegex(TimelineError, "AUDIO_REPLAN_REQUIRED"):
            self.prepare(clips=[clip])

    def test_tts_is_not_repeated_when_cached_audio_is_missing(self):
        m = {**self.manifest, "segments": [{"video": str(self.video), "text": "same text"}]}
        with self.assertRaisesRegex(TimelineError, "PREPARED_NARRATION_REQUIRED"):
            self.prepare(m)

    def test_operator_file_is_unchanged(self):
        before = self.voice.read_bytes()
        t = self.prepare()
        self.assertEqual(before, self.voice.read_bytes())
        self.assertEqual(t.assets[0].path, str(self.voice.resolve()))

    def test_legacy_aspect_and_physical_subtitle_position_are_preserved(self):
        m = {k: v for k, v in self.manifest.items() if k not in {"width", "height"}}
        m.update(aspect_ratio="16:9", subtitle_position_px={"x": 96, "y": -270})
        t = self.prepare(m)
        self.assertEqual((t.canvas.width, t.canvas.height), (1920, 1080))
        self.assertEqual((t.subtitles[0].style.x, t.subtitles[0].style.y), (.1, -.5))

    def test_conflicting_subtitle_coordinates_are_not_silently_overridden(self):
        m = {**self.manifest, "subtitle_position_px": {"x": 8, "y": -60}, "subtitle_style": {"x": .8}}
        with self.assertRaisesRegex(TimelineError, "SUBTITLE_POSITION_CONFLICT"):
            self.prepare(m)

    def test_processed_audio_keeps_original_asset_relationship(self):
        final = self.root / "processed.wav"
        final.write_bytes(self.voice.read_bytes())
        with patch("orchestrator.application.production.prepare_narration", return_value={
                "source": str(self.voice), "path": str(final), "source_duration_us": 600000,
                "duration_us": 600000, "processing": {"mode": "test_derived_asset"}}):
            t = self.prepare()
        narration = t.asset_map()[t.audios[0].asset_id]
        self.assertIsNotNone(narration.origin_asset_id)
        self.assertEqual(t.asset_map()[narration.origin_asset_id].path, str(self.voice.resolve()))

    def test_loudness_failure_cannot_leave_nonexistent_audio_in_timeline(self):
        with patch("orchestrator.audio.final_assets.normalize_narration", return_value={"skipped": "encode_failed"}):
            with self.assertRaisesRegex(ValueError, "NORMALIZATION_FAILED"):
                prepare_narration(self.voice, self.root / "audio", mode="tts", ffmpeg=FFMPEG, ffprobe=FFPROBE)

    def test_unknown_effect_and_arbitrary_frame_map_fail_explicitly(self):
        for extra in ({"effects": ["glow"]}, {"source_frame_map": {"frames": [0, 3]}}):
            with self.subTest(extra=extra), self.assertRaisesRegex(TimelineError, "FEATURE_UNSUPPORTED"):
                self.prepare({**self.manifest, **extra})

    def test_cli_makes_playable_preview_without_draft_and_checks_subtitle_text(self):
        m = dict(self.manifest)
        m["bgm"] = {"path": str(self.voice), "gain": .1}
        manifest = self.root / "selected.json"
        manifest.write_text(json.dumps(m), encoding="utf-8")
        report = self.root / "cli"
        proc = subprocess.run([sys.executable, "-X", "utf8", "-m", "tools.production", "preview", "--input", str(manifest),
                               "--report-dir", str(report), "--ffmpeg", FFMPEG, "--ffprobe", FFPROBE],
                              cwd=PACKAGE, capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((report / "render/preview.mp4").is_file())
        self.assertFalse(list(report.rglob("draft_*.json")))
        t = EditTimeline.load(report / "timeline.json")
        qc = json.loads((report / "render/production_qc.json").read_text(encoding="utf-8"))
        self.assertEqual(qc["structure_media"]["status"], "PASS")
        self.assertFalse(qc["formal_delivery_allowed"])
        receipt = json.loads((report / "render/render_receipt.json").read_text(encoding="utf-8"))
        changed = replace(t, subtitles=(replace(t.subtitles[0], text="wrong subtitle"),))
        # Deliberately bind the structurally wrong text to a matching revision receipt;
        # the subtitle/narration check must still reject it.
        from orchestrator.media.timeline_render import timeline_identity
        changed.save(report / "render/timeline.json")
        receipt["timeline_identity"] = timeline_identity(changed)
        (report / "render/render_receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
        qc = check_production(changed, report / "render", ffmpeg=FFMPEG, ffprobe=FFPROBE)
        self.assertEqual(qc["status"], "BLOCKED")
        self.assertIn("SUBTITLE_NARRATION_TEXT_MISMATCH", str(qc))


if __name__ == "__main__":
    unittest.main()
