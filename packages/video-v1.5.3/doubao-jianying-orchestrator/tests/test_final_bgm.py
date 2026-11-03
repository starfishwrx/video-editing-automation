from concurrent.futures import ThreadPoolExecutor
from threading import Event
from dataclasses import replace
from contextlib import redirect_stderr
import io
import math
from pathlib import Path
import shutil
import struct
import sys
import tempfile
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.audio import final_bgm
from orchestrator.application.production import prepare_headless_task
from orchestrator.domain.timeline import TimelineError
from test_timeline_export import example
from tools.queueing.contracts import meeting_bgm_spec

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")


class BusinessAudioGateTests(unittest.TestCase):
    def test_worker_empty_bgm_contract_is_auto(self):
        for value in (None, "", {}, "  "):
            self.assertEqual(meeting_bgm_spec(value), {"auto": True})

    def test_wrong_task_does_not_begin_audio_or_create_output(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(final_bgm, "prepare_task_bgm") as music:
            root = Path(raw)
            with self.assertRaisesRegex(TimelineError, "TASK_IDENTITY_MISMATCH"):
                prepare_headless_task(example(), {"record_id": "different"}, render_dir=root,
                    report_dir=root / "out", state_dir=root / "state")
            music.assert_not_called()
            self.assertFalse((root / "out").exists())

    def test_changed_script_does_not_begin_audio(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(final_bgm, "prepare_task_bgm") as music:
            root = Path(raw)
            with self.assertRaisesRegex(TimelineError, "TASK_SCRIPT_MISMATCH"):
                prepare_headless_task(example(), {"task_id": "synthetic-export", "script": "changed"},
                    render_dir=root, report_dir=root / "out", state_dir=root / "state")
            music.assert_not_called()

    def test_missing_bgm_candidate_blocks_instead_of_creating_silent_success(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(final_bgm.bgm_selector, "select_bgm", return_value=[]):
            root = Path(raw)
            with self.assertRaisesRegex(TimelineError, "BGM_REQUIRED_NO_MATCHING_TRACK"):
                final_bgm.prepare_task_bgm({"auto": True}, script="sample", task_id="task", batch_id="batch",
                    state_dir=root / "state", output_dir=root / "out", duration_us=1116959)
            self.assertFalse((root / "out/bgm_receipt.json").exists())

    def test_auto_track_failure_tries_alternatives_but_explicit_does_not(self):
        candidates = [{"music_id": "bad", "duration_s": 100}, {"music_id": "good", "duration_s": 100}]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "good.wav"; source.write_bytes(b"fixture")
            def download(track, output):
                return source if track["music_id"] == "good" else None
            facts = {"audio": True, "duration_us": 1_000_000, "streams": [{"codec_type": "audio"}]}
            with patch.object(final_bgm.bgm_selector, "select_bgm", return_value=candidates), \
                 patch("orchestrator.audio.bgm_selector.random.SystemRandom.choice", side_effect=lambda values: list(values)[0]), \
                 patch.object(final_bgm.bgm_selector, "download_track", side_effect=download) as dl, \
                 patch.object(final_bgm, "probe_media", return_value=facts), \
                 patch.object(final_bgm.bgm_selector, "prepare_bgm_track", return_value=source), \
                 patch.object(final_bgm.bgm_selector, "_integrated_lufs", return_value=-28), \
                 patch.object(final_bgm.subprocess, "run"):
                result = final_bgm.prepare_task_bgm({"auto": True}, script="sample", task_id="task",
                    batch_id="batch", state_dir=root / "state", output_dir=root / "out", duration_us=1_000_000)
                self.assertEqual([c.args[0]["music_id"] for c in dl.call_args_list], ["bad", "good"])
                self.assertEqual(result["selected"]["music_id"], "good")
                self.assertEqual(result["attempted_music_ids"], ["bad", "good"])
                dl.reset_mock()
                with self.assertRaisesRegex(TimelineError, "BGM_REQUIRED_DOWNLOAD_FAILED"):
                    final_bgm.prepare_task_bgm({"music_id": "bad"}, script="sample", task_id="other",
                        batch_id="batch", state_dir=root / "state", output_dir=root / "explicit", duration_us=1_000_000)
                self.assertEqual([c.args[0]["music_id"] for c in dl.call_args_list], ["bad"])

    def test_unusable_auto_track_replaced_but_explicit_track_blocked(self):
        candidates = [{"music_id": "bad", "duration_s": 100}, {"music_id": "good", "duration_s": 100}]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); bad = root / "bad.wav"; good = root / "good.wav"
            bad.write_bytes(b"bad"); good.write_bytes(b"good")
            def render(source, output, **kw):
                if source == bad:
                    raise final_bgm._UnusableTrack("BGM_SOURCE_UNREADABLE")
                return source, {"duration_us": 2_000_000}, output / "bgm.wav", {"duration_us": 1_000_000}, -28, 48000
            with patch.object(final_bgm.bgm_selector, "select_bgm", return_value=candidates), \
                 patch("orchestrator.audio.bgm_selector.random.SystemRandom.choice", side_effect=lambda values: list(values)[0]), \
                 patch.object(final_bgm.bgm_selector, "download_track", side_effect=lambda track, out: bad if track["music_id"] == "bad" else good), \
                 patch.object(final_bgm, "_render_source", side_effect=render):
                result = final_bgm.prepare_task_bgm({"auto": True}, script="sample", task_id="auto", batch_id="batch",
                    state_dir=root / "state", output_dir=root / "out", duration_us=1_000_000)
                self.assertEqual(result["attempted_music_ids"], ["bad", "good"])
                with self.assertRaisesRegex(ValueError, "BGM_SOURCE_UNREADABLE"):
                    final_bgm.prepare_task_bgm({"music_id": "bad"}, script="sample", task_id="explicit", batch_id="batch",
                        state_dir=root / "state", output_dir=root / "explicit", duration_us=1_000_000)

    def test_parallel_attempt_cannot_replace_confirmed_success(self):
        candidates = [{"music_id": "A", "duration_s": 100}, {"music_id": "B", "duration_s": 100}]
        started, finish = Event(), Event()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "music.wav"; source.write_bytes(b"fixture")
            downloads = []
            def download(track, out):
                downloads.append(track["music_id"])
                return source if len(downloads) == 1 else None
            def render(source, output, **kw):
                started.set()
                self.assertTrue(finish.wait(5))
                return source, {"duration_us": 2_000_000}, output / "bgm.wav", {"duration_us": 1_000_000}, -28, 48000
            def attempt(name):
                return final_bgm.prepare_task_bgm({"auto": True}, script="sample", task_id="same", batch_id="batch",
                    state_dir=root / "state", output_dir=root / name, duration_us=1_000_000)
            with patch.object(final_bgm.bgm_selector, "select_bgm", return_value=candidates), \
                 patch("orchestrator.audio.bgm_selector.random.SystemRandom.choice", side_effect=lambda values: list(values)[0]), \
                 patch.object(final_bgm.bgm_selector, "download_track", side_effect=download), \
                 patch.object(final_bgm, "_render_source", side_effect=render), ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(attempt, "one")
                self.assertTrue(started.wait(5))
                second = pool.submit(attempt, "two")
                self.assertFalse(second.done())
                finish.set()
                self.assertEqual(first.result(timeout=10)["selected"]["music_id"], "A")
                with self.assertRaisesRegex(ValueError, "BGM_REQUIRED_DOWNLOAD_FAILED"):
                    second.result(timeout=10)
            self.assertEqual(downloads, ["A", "A"])
            state = next((root / "state").glob("task-*.json"))
            record = __import__('json').loads(state.read_text(encoding='utf-8'))
            self.assertTrue(record["confirmed"])
            self.assertEqual(record["selected"]["music_id"], "A")

    def test_probe_and_decode_failures_are_distinguished_from_tool_failures(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "music.m4a"; source.write_bytes(b"fixture")
            with self.assertRaises(final_bgm.bgm_selector.BgmSourceDecodeError):
                final_bgm.bgm_selector._check_ffmpeg_result(SimpleNamespace(returncode=0, stderr="Invalid PCM packet"))
            with self.assertRaises(final_bgm.bgm_selector.BgmToolError):
                final_bgm.bgm_selector._check_ffmpeg_result(SimpleNamespace(returncode=1, stderr="Error while decoding stream: Cannot allocate memory"))
            with self.assertRaises(final_bgm.bgm_selector.BgmToolError):
                final_bgm.bgm_selector._check_ffmpeg_result(SimpleNamespace(returncode=1, stderr="Invalid data found when processing input\nError opening output file: Read-only file system"))
            final_bgm.bgm_selector._check_ffmpeg_result(SimpleNamespace(returncode=0,
                stderr="Input #0, wav, from 'D:/packet corrupt/permission denied.wav':"))
            final_bgm.bgm_selector._check_ffmpeg_result(SimpleNamespace(returncode=0,
                stderr="    title           : permission denied\n    artist          : packet corrupt"))
            with self.assertRaises(final_bgm.bgm_selector.BgmSourceDecodeError):
                final_bgm.bgm_selector._check_ffmpeg_result(SimpleNamespace(returncode=1,
                    stderr="D:/permission denied/bad.m4a: Invalid data found when processing input"))
            for stderr, expected in (("Invalid data found when processing input", final_bgm._UnusableTrack),
                                     ("Unrecognized option 'show_streams'", final_bgm.bgm_selector.BgmToolError)):
                failure = subprocess.CalledProcessError(1, "ffprobe", stderr=stderr)
                with self.subTest(stderr=stderr), patch.object(final_bgm, "probe_media", side_effect=failure):
                    with self.assertRaises(expected):
                        final_bgm._render_source(source, Path(raw) / "out", duration_us=1_000_000,
                            ffmpeg="ffmpeg", ffprobe="ffprobe", target_lufs=-28, fade_in=.4, fade_out=.2, start=0)
            for stderr, expected in (("Error while decoding stream #0:0: Invalid data found when processing input", final_bgm.bgm_selector.BgmSourceDecodeError),
                                     ("No such filter: loudnorm", final_bgm.bgm_selector.BgmToolError)):
                with self.subTest(stderr=stderr), patch.object(final_bgm.bgm_selector.subprocess, "run",
                        return_value=SimpleNamespace(returncode=1, stderr=stderr)):
                    with self.assertRaises(expected):
                        final_bgm.bgm_selector.prepare_bgm_track(source, Path(raw) / "out.m4a", 1.0)

    def test_ffmpeg_render_source_error_differs_from_missing_filter(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "music.m4a"; source.write_bytes(b"fixture")
            for stderr, expected in (("Error while decoding stream #0:0: Invalid data found when processing input", final_bgm.bgm_selector.BgmSourceDecodeError),
                                     ("No such filter: loudnorm", final_bgm.bgm_selector.BgmToolError)):
                failure = subprocess.CalledProcessError(1, "ffmpeg", stderr=stderr)
                with self.subTest(stderr=stderr), \
                     patch.object(final_bgm.bgm_selector.subprocess, "run", side_effect=[SimpleNamespace(returncode=0, stderr=""), failure]), \
                     patch.object(final_bgm.bgm_selector, "_mean_volume", return_value=-24), \
                     patch.object(final_bgm.bgm_selector, "_integrated_lufs", return_value=-25):
                    with self.assertRaises(expected):
                        final_bgm.bgm_selector.prepare_bgm_track(source, root / "out.m4a", 1.0)

    def test_global_processing_failure_does_not_switch_auto_tracks(self):
        candidates = [{"music_id": "A", "duration_s": 100}, {"music_id": "B", "duration_s": 100}]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw); source = root / "music.m4a"; source.write_bytes(b"fixture")
            with patch.object(final_bgm.bgm_selector, "select_bgm", return_value=candidates), \
                 patch.object(final_bgm.bgm_selector, "download_track", return_value=source) as download, \
                 patch.object(final_bgm, "_render_source", side_effect=final_bgm.bgm_selector.BgmToolError("missing filter")):
                with self.assertRaises(final_bgm.bgm_selector.BgmToolError):
                    final_bgm.prepare_task_bgm({"auto": True}, script="sample", task_id="auto", batch_id="b",
                        state_dir=root / "state", output_dir=root / "out", duration_us=1_000_000)
                self.assertEqual(download.call_count, 1)

    def test_business_cli_cannot_omit_task_and_bgm_state(self):
        from tools.production import main
        message = io.StringIO()
        with redirect_stderr(message), self.assertRaises(SystemExit) as caught:
            main(["headless-plan", "--timeline", "timeline.json", "--render-dir", "render",
                  "--name", "case", "--font", "local.ttf", "--report-dir", "report", "--review-only"])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("--task-json", message.getvalue())
        self.assertIn("--bgm-state-dir", message.getvalue())

    def test_video_duration_cannot_mask_a_short_embedded_audio_stream(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve(); source = root / "video.mp4"; source.write_bytes(b"fixture")
            facts = {"audio": True, "duration_us": 20000000,
                     "streams": [{"codec_type": "video"}, {"codec_type": "audio", "duration": "2"}]}
            with patch.object(final_bgm, "probe_media", return_value=facts), \
                 patch.object(final_bgm.bgm_selector, "prepare_bgm_track") as prepare:
                with self.assertRaisesRegex(TimelineError, "AUDIO_ONLY_REQUIRED"):
                    final_bgm.prepare_task_bgm({"path": str(source)}, script="sample", task_id="task", batch_id="batch",
                        state_dir=root / "state", output_dir=root / "out", duration_us=20000000)
                prepare.assert_not_called()

    def test_batch_aliases_and_existing_edit_decisions_are_preserved(self):
        from orchestrator.quality import production_qc
        from orchestrator.media import timeline_render
        for alias in ("batch_identity", "worker_batch_identity"):
            with self.subTest(alias=alias), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); timeline = example()
                task = {"task_id": timeline.task_id, "script": "重复字幕\n重复字幕",
                        "bgm": {"auto": True}, alias: "same-batch"}
                receipt = {"path": str(root / "music.wav"), "asset_duration_us": timeline.duration_us}
                with patch.object(final_bgm, "prepare_task_bgm", return_value=receipt) as prepare, \
                     patch.object(production_qc, "check_production", return_value={"structure_media": {"status": "PASS"}}), \
                     patch.object(timeline_render, "render_timeline"):
                    revised, _ = prepare_headless_task(timeline, task, render_dir=root / "source-render",
                        report_dir=root / "new", state_dir=root / "state")
                self.assertEqual(prepare.call_args.kwargs["batch_id"], "same-batch")
                self.assertEqual(revised.videos, timeline.videos)
                self.assertEqual(revised.sentences, timeline.sentences)
                self.assertEqual(revised.subtitles, timeline.subtitles)
                self.assertEqual([e for e in revised.audios if e.role == "narration"], list(timeline.audios))
                self.assertEqual(len([e for e in revised.audios if e.role == "bgm"]), 1)
                self.assertEqual(revised.unresolved, timeline.unresolved)


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg/ffprobe required")
class FinalBgmMediaTests(unittest.TestCase):
    def test_real_audio_metadata_cannot_be_mistaken_for_an_error(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "tone.wav"
            subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                            "-metadata", "title=permission denied", "-y", str(source)],
                           capture_output=True, check=True)
            result = subprocess.run([FFMPEG, "-i", str(source), "-f", "null", "-"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn("permission denied", result.stderr)
            final_bgm.bgm_selector._check_ffmpeg_result(result)

    def test_real_corrupt_audio_is_rejected_as_source_failure(self):
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "corrupt.m4a"; source.write_bytes(b"not a media stream" * 200)
            with self.assertRaisesRegex(final_bgm._UnusableTrack, "BGM_SOURCE_UNREADABLE"):
                final_bgm._render_source(source, Path(raw) / "out", duration_us=1_000_000,
                    ffmpeg=FFMPEG, ffprobe=FFPROBE, target_lufs=-28, fade_in=.4, fade_out=.2, start=0)

    def test_short_music_or_trimming_leading_silence_cannot_be_padded_to_success(self):
        for leading_samples, duration in ((0, 20_000_000), (48000, 2_500_000)):
            with self.subTest(leading=leading_samples), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve(); source = root / "short.wav"
                tone = b"".join(struct.pack("<hh", *([round(10000 * math.sin(i * 2 * math.pi * 440 / 48000))] * 2))
                                for i in range(96000))
                with wave.open(str(source), "wb") as stream:
                    stream.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
                    stream.writeframes(b"\0\0\0\0" * leading_samples + tone)
                with self.assertRaisesRegex(ValueError, "BGM_SOURCE_WINDOW_TOO_SHORT"):
                    final_bgm.prepare_task_bgm({"path": str(source)}, script="test", task_id="t", batch_id="b",
                        state_dir=root / "state", output_dir=root / "out", duration_us=duration,
                        ffmpeg=FFMPEG, ffprobe=FFPROBE)
                self.assertFalse((root / "out/bgm_receipt.json").exists())

    def test_auto_selection_receipt_loudness_fades_and_non_millisecond_pcm_endpoint(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            source = root / "music.wav"
            samples = b"".join(struct.pack("<hh", *([round(10000 * math.sin(i * 2 * math.pi * 440 / 48000))] * 2))
                               for i in range(192000))
            with wave.open(str(source), "wb") as stream:
                stream.setparams((2, 2, 48000, 0, "NONE", "not compressed")); stream.writeframes(samples)
            candidate = {"music_id": "fixture-id", "title": "fixture music", "duration_s": 4, "url": "unused"}
            with patch.object(final_bgm.bgm_selector, "select_bgm", return_value=[candidate]) as select, \
                 patch.object(final_bgm.bgm_selector, "download_track", return_value=source):
                result = final_bgm.prepare_task_bgm(meeting_bgm_spec(""), script="妈妈家庭", task_id="fixture",
                    batch_id="batch", state_dir=root / "state", output_dir=root / "out",
                    duration_us=1116959, ffmpeg=FFMPEG, ffprobe=FFPROBE)
                self.assertEqual(select.call_args.kwargs["script"], "妈妈家庭")
            self.assertEqual(result["selected"]["music_id"], "fixture-id")
            self.assertNotIn("url", result["selected"])
            self.assertGreaterEqual(result["asset_duration_us"], 1116959)
            self.assertEqual(result["samples"], (1116959 * 48000 + 999999) // 1000000)
            self.assertLessEqual(abs(result["measured_lufs"] + 28), 1.5)
            with wave.open(result["path"], "rb") as stream:
                self.assertEqual(stream.getnframes(), result["samples"])
                data = struct.unpack("<" + "h" * (stream.getnframes() * 2), stream.readframes(stream.getnframes()))
            rms = lambda values: math.sqrt(sum(v*v for v in values) / len(values))
            self.assertLess(rms(data[:480]), rms(data[24000:30000]) * .2)
            self.assertTrue(result["fades_baked_into_asset"])
            # The real shared selector reuses the choice instead of randomizing a retry.
            chosen = final_bgm.bgm_selector.select_bgm_for_task([candidate], task_identity={
                "task_id": "fixture", "script_sha256": __import__('hashlib').sha256("妈妈家庭".encode()).hexdigest()},
                batch_identity="batch", state_dir=root / "state")
            self.assertTrue(chosen["reused"])


if __name__ == "__main__":
    unittest.main()
