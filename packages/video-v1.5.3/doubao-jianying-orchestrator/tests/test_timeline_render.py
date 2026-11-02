from dataclasses import replace
from fractions import Fraction
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.application.production import prepare_timeline
from orchestrator.domain.timeline import (Asset, AudioEvent, Canvas, EditTimeline, SourceInterval,
                                         Sentence, SubtitleEvent, TimelineError, VideoEvent)
from orchestrator.media.probe import probe_media
from orchestrator.media.timeline_render import render_timeline, _frame_plan, subtitle_ass
from orchestrator.quality.production_qc import check_production

# Capture real tools before unrelated legacy tests temporarily rewrite PATH.
FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")


def run(cmd, **kw):
    return subprocess.run([str(x) for x in cmd], capture_output=True, check=True, timeout=90, **kw)


def make_media(root, *, offset=0, frame_count=150):
    root = Path(root)
    video, voice = root / ("source-offset.mkv" if offset else "source.mkv"), root / "voice.wav"
    colors = np.array([[(i * 17 + 20) % 240, (i * 29 + 30) % 240, (i * 43 + 40) % 240]
                       for i in range(frame_count)], dtype=np.uint8)
    frames = np.broadcast_to(colors[:, None, None, :], (frame_count, 240, 160, 3)).copy()
    cmd = [FFMPEG, "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "160x240", "-r", "60", "-i", "-"]
    if offset:
        cmd += ["-vf", f"setpts=PTS+{offset}/TB"]
    run(cmd + ["-c:v", "ffv1", video], input=frames.tobytes())
    if not voice.exists():
        sample = (.08 * 32767 * np.sin(np.arange(120000) * 2 * np.pi * 440 / 48000)).astype("<i2")
        with wave.open(str(voice), "wb") as w:
            w.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            w.writeframes(np.column_stack((sample, sample)).tobytes())
    return video, voice, colors


def fixed_timeline(video, voice=None, duration=1_116_959):
    info = probe_media(video, ffprobe=FFPROBE)
    assets = [Asset("v", str(video), "video", info["duration_us"], 160, 240, 60)]
    audios = ()
    if voice:
        assets.append(Asset("a", str(voice), "audio", 2_500_000))
        audios = (AudioEvent("n", "a", "narration", 0, duration),
                  AudioEvent("b", "a", "bgm", 0, duration, gain=.1),
                  AudioEvent("o", "a", "original", 0, duration, gain=.1))
    v = VideoEvent("v1", "v", (), 0, duration, 0, duration,
                   protected_intervals=(SourceInterval("result", 1_100_000, duration, "result", 1),))
    return EditTimeline("fixed", 1, Canvas(160, 240), duration, tuple(assets), (), (v,), audios)


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/ffprobe required")
class TimelineRenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.video, cls.voice, cls.colors = make_media(cls.root)
        cls.t = fixed_timeline(cls.video, cls.voice)
        cls.out = cls.root / "render"
        cls.receipt = render_timeline(cls.t, cls.out, ffmpeg=FFMPEG, ffprobe=FFPROBE)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_real_output_has_expected_source_frames_and_protected_last_frame(self):
        receipt = self.receipt
        mapping = receipt["frame_mapping"][0]["frames"]
        self.assertEqual(len(mapping), 34)
        self.assertEqual(mapping[-1]["source_frame"], 66)
        self.assertEqual(receipt["protected"][0]["actual_output_frames"], 1)
        raw = run([FFMPEG, "-v", "error", "-i", receipt["video"], "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]).stdout
        frames = np.frombuffer(raw, np.uint8).reshape(34, 240, 160, 3)
        for frame, row in zip(frames, mapping):
            self.assertLess(np.abs(frame.astype(float).mean(axis=(0, 1)) - self.colors[row["source_frame"]]).max(), 10)
        self.assertGreaterEqual(receipt["render_duration_us"], self.t.duration_us)
        self.assertLess(receipt["quantization_overhang_us"], 33334)

    def test_long_continuous_action_does_not_exceed_expression_parser_depth(self):
        source = self.root / "long-source"
        source.mkdir()
        video, _, colors = make_media(source, frame_count=420)
        t = fixed_timeline(video, duration=6_216_959)
        t = replace(t, videos=(replace(t.videos[0], protected_intervals=(
            SourceInterval("long-result", 6_100_000, 6_216_959, "result", 3),)),))
        receipt = render_timeline(t, self.root / "long-render", ffmpeg=FFMPEG, ffprobe=FFPROBE)
        self.assertEqual(receipt["video_verification"]["frames"], 187)
        self.assertEqual(receipt["frame_mapping"][0]["frames"][-1]["source_frame"], 372)
        raw = run([FFMPEG, "-v", "error", "-i", receipt["video"], "-vf", "select='eq(n,186)'",
                   "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]).stdout
        frame = np.frombuffer(raw, np.uint8).reshape(240, 160, 3)
        self.assertLess(np.abs(frame.astype(float).mean(axis=(0, 1)) - colors[372]).max(), 10)

    def test_independent_audio_tracks_and_full_sample_lengths(self):
        self.assertEqual(set(self.receipt["audio"]["tracks"]), {"narration", "bgm", "original"})
        for path in self.receipt["audio"]["tracks"].values():
            with wave.open(path, "rb") as w:
                self.assertEqual(w.getnframes(), self.receipt["audio"]["samples"])

    def test_valid_render_is_still_uncertified(self):
        qc = check_production(self.t, self.out, ffmpeg=FFMPEG, ffprobe=FFPROBE)
        self.assertEqual(qc["structure_media"]["status"], "PASS")
        self.assertEqual(qc["status"], "UNCERTIFIED")
        self.assertEqual(qc["content_semantics"]["status"], "NOT_RUN")
        self.assertFalse(qc["formal_delivery_allowed"])

    def test_changed_revision_does_not_reuse_content_or_render(self):
        qc = check_production(replace(self.t, revision=2), self.out, ffmpeg=FFMPEG, ffprobe=FFPROBE)
        self.assertEqual(qc["status"], "BLOCKED")
        self.assertIn("RENDER_REVISION", str(qc))

    def test_content_gap_blocks_delivery_without_mislabeling_valid_media(self):
        t = replace(self.t, unresolved=({"sentence_id": "s1", "reason": "quantity evidence unverified"},))
        out = self.root / "unresolved-content"
        render_timeline(t, out, ffmpeg=FFMPEG, ffprobe=FFPROBE)
        qc = check_production(t, out, ffmpeg=FFMPEG, ffprobe=FFPROBE)
        self.assertEqual(qc["structure_media"]["status"], "PASS")
        self.assertEqual(qc["content_semantics"]["status"], "BLOCKED")
        self.assertEqual(qc["status"], "BLOCKED")
        self.assertFalse(qc["formal_delivery_allowed"])

    def test_invalid_unresolved_type_still_returns_blocked_report(self):
        for index, value in enumerate((None, 42, "fake")):
            out = self.root / f"bad-unresolved-{index}"
            with self.subTest(value=value):
                qc = check_production(replace(self.t, unresolved=value), out, ffmpeg=FFMPEG, ffprobe=FFPROBE)
                self.assertEqual(qc["status"], "BLOCKED")
                self.assertEqual(qc["structure_media"]["status"], "FAIL")
                self.assertTrue((out / "production_qc.json").is_file())

    def test_cannot_overwrite_existing_output(self):
        with self.assertRaisesRegex(TimelineError, "OUTPUT_EXISTS"):
            render_timeline(self.t, self.out, ffmpeg=FFMPEG, ffprobe=FFPROBE)

    def test_window_can_cover_result_but_fail_actual_output_frame_minimum(self):
        v = replace(self.t.videos[0], protected_intervals=(SourceInterval("result", 1_100_000, self.t.duration_us, "result", 2),))
        out = self.root / "missing-result"
        with self.assertRaisesRegex(TimelineError, "PROTECTED_OUTPUT_FRAMES_MISSING"):
            render_timeline(replace(self.t, videos=(v,)), out, ffmpeg=FFMPEG, ffprobe=FFPROBE)
        self.assertFalse((out / "render_receipt.json").exists())
        self.assertTrue((out / "render_failure.json").is_file())

    def test_vfr_is_rejected_by_actual_pts_not_average_fps(self):
        metadata = {"stream": {"avg_frame_rate": "60/1"}, "source_end_us": 2_000_000,
                    "frames": [{"pts_us": n * 16667 + (5000 if n > 15 else 0)} for n in range(120)]}
        with self.assertRaisesRegex(TimelineError, "VFR_UNSUPPORTED"):
            _frame_plan(self.t, self.t.videos[0], metadata)

    def test_declared_audio_duration_cannot_hide_short_source(self):
        a = replace(self.t.assets[1], duration_us=3_500_000)
        with self.assertRaisesRegex(TimelineError, "ACTUAL_ASSET_DURATION_CHANGED"):
            render_timeline(replace(self.t, assets=(self.t.assets[0], a)), self.root / "short-audio",
                            ffmpeg=FFMPEG, ffprobe=FFPROBE)

    def test_nonzero_container_start_is_normalized_without_wrong_source_frame(self):
        video, _, _ = make_media(self.root, offset=3)
        t = fixed_timeline(video)
        receipt = render_timeline(t, self.root / "offset-render", ffmpeg=FFMPEG, ffprobe=FFPROBE)
        self.assertEqual(receipt["frame_mapping"][0]["frames"][-1]["decoded_source_pts_us"], 1_100_000)

    def test_real_vfr_source_is_not_silently_normalized(self):
        video = self.root / "vfr.mkv"
        run([FFMPEG, "-v", "error", "-i", self.video, "-vf", "select='not(eq(mod(n,3),0))'",
             "-fps_mode", "vfr", "-c:v", "ffv1", video])
        t = fixed_timeline(video)
        with self.assertRaisesRegex(TimelineError, "VFR_UNSUPPORTED"):
            render_timeline(t, self.root / "vfr-render", ffmpeg=FFMPEG, ffprobe=FFPROBE)

    def test_native_audio_with_relative_stream_offset_is_rejected(self):
        video = self.root / "delayed-audio.mkv"
        run([FFMPEG, "-v", "error", "-i", self.video, "-itsoffset", "0.5", "-i", self.voice,
             "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", video])
        t = fixed_timeline(video)
        t = replace(t, audios=(AudioEvent("native", "v", "original", 0, t.duration_us),))
        with self.assertRaisesRegex(TimelineError, "ORIGINAL_AUDIO_STREAM_OFFSET_UNSUPPORTED"):
            render_timeline(t, self.root / "delayed-audio-render", ffmpeg=FFMPEG, ffprobe=FFPROBE)

    def test_adjacent_subtitles_share_quantized_boundary(self):
        t = replace(self.t, sentences=(Sentence("s1", "AAA", 0, 600001),
                                       Sentence("s2", "BBB", 600001, self.t.duration_us - 600001)),
                    videos=(replace(self.t.videos[0], sentence_ids=("s1", "s2")),),
                    subtitles=(SubtitleEvent("c1", "s1", "AAA", 0, 600001),
                               SubtitleEvent("c2", "s2", "BBB", 600001, self.t.duration_us - 600001)))
        rows = [r.split(",") for r in subtitle_ass(t, 160, 240).splitlines() if r.startswith("Dialogue:")]
        self.assertEqual(rows[0][2], rows[1][1])

    def test_literal_ass_control_sequences_are_explicitly_unsupported(self):
        for text in (r"A\NB", "A{B}"):
            t = replace(self.t, subtitles=(SubtitleEvent("c", "s", text, 0, 100_000),))
            with self.subTest(text=text), self.assertRaisesRegex(TimelineError, "LITERAL_ESCAPE_UNSUPPORTED"):
                subtitle_ass(t, 160, 240)

    def test_subtitle_below_ass_time_quantum_is_explicitly_unsupported(self):
        t = replace(self.t, subtitles=(SubtitleEvent("c", "s", "tiny", 101, 800),))
        with self.assertRaisesRegex(TimelineError, "BELOW_ASS_TIME_QUANTUM"):
            subtitle_ass(t, 160, 240)

    def test_between_frame_source_start_uses_first_real_frame_inside_window(self):
        frames = [{"pts_us": round(n * 1_000_000 / 60)} for n in range(150)]
        metadata = {"stream": {"avg_frame_rate": "60/1", "time_base": "1/60000"},
                    "source_end_us": 2_500_000, "frames": frames}
        event = replace(self.t.videos[0], source_start_us=210_000)
        mapping = _frame_plan(self.t, event, metadata)
        self.assertEqual(mapping[0]["source_frame"], 13)
        self.assertGreaterEqual(mapping[0]["source_pts_us"], 210_000)
        self.assertEqual(len(mapping), self.t.frame_count)


if __name__ == "__main__":
    unittest.main()
