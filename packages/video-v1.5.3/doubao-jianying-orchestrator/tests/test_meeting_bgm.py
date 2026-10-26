import tempfile
import sys
import unittest
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import bgm_selector, engine as engine_module, platform_env


class MeetingBgmSelectionTests(unittest.TestCase):
    def setUp(self):
        self.candidates = [{"music_id": str(i), "title": "track-%s" % i} for i in range(3)]

    def choose(self, identity, batch, root, **kw):
        return bgm_selector.select_bgm_for_task(self.candidates,
            task_identity=identity, batch_identity=batch, state_dir=Path(root), **kw)

    def test_retry_reuses_stable_task_receipt_and_identity_change_does_not(self):
        with tempfile.TemporaryDirectory() as root:
            first = self.choose({"task_id": "r1", "script_sha": "a"}, "b1", root)
            retry = self.choose({"task_id": "r1", "script_sha": "a"}, "b1", root)
            changed = self.choose({"task_id": "r1", "script_sha": "b"}, "b1", root)
            self.assertEqual(first["music_id"], retry["music_id"])
            self.assertTrue(retry["reused"])
            self.assertFalse(changed["reused"])

    def test_batch_avoids_repeat_until_candidate_pool_is_exhausted(self):
        with tempfile.TemporaryDirectory() as root:
            picked = [self.choose({"task_id": str(i)}, "batch", root)["music_id"]
                      for i in range(3)]
            self.assertEqual(len(set(picked)), 3)
            fourth = self.choose({"task_id": "fourth"}, "batch", root)
            self.assertIn(fourth["music_id"], picked)

    def test_same_music_id_with_different_scores_counts_as_one_track(self):
        candidates = [
            {"music_id": "same", "score": 10, "title": "same track"},
            {"music_id": "same", "score": 99, "title": "same track"},
            {"music_id": "other", "score": 20, "title": "other track"},
        ]
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(bgm_selector.random.SystemRandom, "choice",
                                   side_effect=lambda rows: rows[0]):
                first = bgm_selector.select_bgm_for_task(candidates,
                    task_identity={"task_id": "a"}, batch_identity="batch",
                    state_dir=Path(root))
                second = bgm_selector.select_bgm_for_task(candidates,
                    task_identity={"task_id": "b"}, batch_identity="batch",
                    state_dir=Path(root))
            self.assertEqual({first["music_id"], second["music_id"]}, {"same", "other"})

    def test_disjoint_candidate_pools_keep_history_when_pool_returns(self):
        with tempfile.TemporaryDirectory() as root:
            pool_ab = [{"music_id": "A"}, {"music_id": "B"}]
            pool_cd = [{"music_id": "C"}, {"music_id": "D"}]
            with mock.patch.object(bgm_selector.random.SystemRandom, "choice",
                                   side_effect=lambda rows: rows[0]):
                first = bgm_selector.select_bgm_for_task(pool_ab,
                    task_identity="one", batch_identity="rolling", state_dir=Path(root))
                other_pool = bgm_selector.select_bgm_for_task(pool_cd,
                    task_identity="two", batch_identity="rolling", state_dir=Path(root))
                return_pool = bgm_selector.select_bgm_for_task(pool_ab,
                    task_identity="three", batch_identity="rolling", state_dir=Path(root))
            self.assertEqual(first["music_id"], "A")
            self.assertEqual(other_pool["music_id"], "C")
            self.assertEqual(return_pool["music_id"], "B")

    def test_threaded_selection_keeps_batch_choices_unique(self):
        candidates = [{"music_id": str(i)} for i in range(20)]
        with tempfile.TemporaryDirectory() as root:
            def choose(i):
                return bgm_selector.select_bgm_for_task(candidates,
                    task_identity={"task_id": str(i)}, batch_identity="threaded",
                    state_dir=Path(root))["music_id"]
            with ThreadPoolExecutor(max_workers=8) as pool:
                selected = list(pool.map(choose, range(12)))
            self.assertEqual(len(set(selected)), 12)

    def test_existing_lock_file_from_crashed_process_is_reused(self):
        with tempfile.TemporaryDirectory() as root:
            state_dir = Path(root)
            lock_file = state_dir / ".selection.lock"
            lock_file.write_text("stale owner pid", encoding="utf-8")
            selected = bgm_selector.select_bgm_for_task(self.candidates,
                task_identity="after-crash", batch_identity="crash-test",
                state_dir=state_dir)
            self.assertIn(selected["music_id"], {"0", "1", "2"})
            self.assertTrue(lock_file.is_file(), "OS lock file is retained, not unlinked")

    def test_explicit_path_and_music_id_bypass_random(self):
        with tempfile.TemporaryDirectory() as root:
            by_path = self.choose({"task_id": "p"}, "b", root,
                                  explicit_path="C:/music.mp3")
            by_id = self.choose({"task_id": "m"}, "b", root,
                                explicit_music_id="1")
            self.assertEqual(by_path["path"], "C:/music.mp3")
            self.assertEqual(by_id["music_id"], "1")
            self.assertEqual(by_id["selection_mode"], "explicit_music_id")

    def test_explicit_official_title_resolves_by_normalized_exact_match(self):
        track = bgm_selector.MusicTrack("song-id", "Bright Morning", 60,
                                         "calm", "https://cdn.example/song")
        with mock.patch.object(bgm_selector, "load_library", return_value=[track]):
            result = bgm_selector.select_bgm(music_id="  bright   morning  ", topk=5)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["music_id"], "song-id")
        self.assertEqual(result[0]["title"], "Bright Morning")

    def test_explicit_id_still_resolves_before_title_lookup(self):
        by_id = bgm_selector.MusicTrack("song-id", "Different title", 60,
                                        "calm", "https://cdn.example/song")
        with mock.patch.object(bgm_selector, "load_library", return_value=[by_id]):
            result = bgm_selector.select_bgm(music_id="song-id")
        self.assertEqual(result[0]["music_id"], "song-id")

    def test_unknown_explicit_title_does_not_fall_back_to_recommendations(self):
        track = bgm_selector.MusicTrack("song-id", "Official title", 60,
                                         "calm", "https://cdn.example/song")
        with mock.patch.object(bgm_selector, "load_library", return_value=[track]), \
             mock.patch.object(bgm_selector, "recommend_bgm", side_effect=AssertionError("must not fallback")):
            self.assertEqual(bgm_selector.select_bgm(music_id="unknown title"), [])

    def test_duplicate_explicit_titles_fail_closed(self):
        tracks = [
            bgm_selector.MusicTrack("song-a", "Shared title", 60, "calm", "url-a"),
            bgm_selector.MusicTrack("song-b", "Shared  title", 60, "calm", "url-b"),
        ]
        with mock.patch.object(bgm_selector, "load_library", return_value=tracks), \
             mock.patch.object(bgm_selector, "recommend_bgm", side_effect=AssertionError("must not fallback")):
            self.assertEqual(bgm_selector.select_bgm(music_id="Shared title"), [])

    def test_engine_passes_resolved_music_id_after_title_lookup(self):
        candidates = [{"music_id": "catalog-id", "title": "Official Song",
                       "url": "https://cdn.example/track"}]
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.m4a"
            source.write_bytes(b"fixture")
            engine = engine_module.OrchestrationEngine.__new__(engine_module.OrchestrationEngine)
            engine.m = {"bgm": {"music_id": "Official Song"}, "task_id": "task-1",
                        "full_script": "demo script"}
            engine.total_us = 1_000_000
            engine.report_dir = Path(root)
            engine.ffmpeg = None
            engine.warnings = []
            with mock.patch.object(engine_module.bgm_selector, "select_bgm",
                                   return_value=candidates), \
                 mock.patch.object(engine_module.bgm_selector, "select_bgm_for_task",
                                   return_value=dict(candidates[0])) as select_task, \
                 mock.patch.object(engine_module.bgm_selector, "download_track",
                                   return_value=source), \
                 mock.patch.object(engine_module.bgm_selector, "prepare_bgm_track"):
                engine.prepare_bgm()
            self.assertEqual(select_task.call_args.kwargs["explicit_music_id"], "catalog-id")
            self.assertEqual(engine._bgm_choice["requested_label"], "Official Song")

    def test_negative_scientific_near_zero_onset_is_leading_but_later_pause_is_not(self):
        initial = ("[silencedetect] silence_start: -2.26757e-05\n"
                   "[silencedetect] silence_end: 3.000000 | silence_duration: 3.000022")
        later = ("[silencedetect] silence_start: 1.250000\n"
                 "[silencedetect] silence_end: 2.000000 | silence_duration: 0.750000")
        self.assertAlmostEqual(bgm_selector._leading_silence_end(initial), 3.0)
        self.assertIsNone(bgm_selector._leading_silence_end(later))

    @mock.patch.object(bgm_selector, "_integrated_lufs", side_effect=[-25.0, -28.0])
    @mock.patch.object(bgm_selector, "_mean_volume", side_effect=[-24.0, -28.0])
    @mock.patch.object(bgm_selector.subprocess, "run")
    def test_prepare_skips_leading_silence_and_keeps_timeline_start_zero(self, run, mean, lufs):
        run.return_value = mock.Mock(stderr="silence_start: 0.000000\nsilence_end: 1.250000\n")
        with tempfile.TemporaryDirectory() as root:
            out = Path(root) / "prepared.m4a"
            bgm_selector.prepare_bgm_track(Path(root) / "source.m4a", out, 5.0)
            render = next(call.args[0] for call in run.call_args_list
                          if "-c:a" in call.args[0])
            af = render[render.index("-af") + 1]
            self.assertIn("atrim=start=1.25:duration=5.0", af)
            self.assertIn("afade=t=in:st=0", af)

    @mock.patch.object(bgm_selector, "_mean_volume", return_value=-90.0)
    @mock.patch.object(bgm_selector.subprocess, "run")
    def test_silent_unavailable_source_is_not_reported_as_success(self, run, mean):
        run.return_value = mock.Mock(stderr="")
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "no audible music"):
                bgm_selector.prepare_bgm_track(Path(root) / "silent.m4a",
                    Path(root) / "out.m4a", 5.0)

    def test_real_three_second_leader_is_trimmed_to_audible_one_second_track(self):
        ffmpeg_path, ffprobe_path = platform_env.find_ffmpeg()
        if not ffmpeg_path or not ffprobe_path:
            self.skipTest("ffmpeg/ffprobe are required for the real media fixture")
        old_path = os.environ.get("PATH")
        platform_env.bootstrap_path(ffmpeg_path.parent)
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old_path)
                        if old_path is not None else os.environ.pop("PATH", None))
        ffmpeg, ffprobe = str(ffmpeg_path), str(ffprobe_path)
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "leader.wav"
            output = Path(root) / "trimmed.m4a"
            subprocess.run([
                ffmpeg, "-y", "-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100:duration=3",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=2",
                "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a]",
                "-map", "[a]", "-c:a", "pcm_s16le", str(source),
            ], check=True, capture_output=True, text=True)
            bgm_selector.prepare_bgm_track(source, output, 1.0, ffmpeg=ffmpeg,
                fade_in=0.1, fade_out=0.1, target_lufs=-28.0)
            onset = bgm_selector._mean_volume(ffmpeg, output, start=0.0, duration=0.25)
            self.assertIsNotNone(onset)
            self.assertGreater(onset, -50.0)
            probe = subprocess.run([
                ffprobe, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(output),
            ], check=True, capture_output=True, text=True)
            self.assertAlmostEqual(float(probe.stdout.strip()), 1.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
