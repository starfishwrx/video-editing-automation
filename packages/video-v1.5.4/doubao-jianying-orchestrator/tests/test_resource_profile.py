import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import resource_profile  # noqa: E402
from orchestrator.resource_profile import choose_profile  # noqa: E402


def _env(**values):
    clean = {k: v for k, v in os.environ.items()
             if k not in {"JY_EXECUTION_PROFILE", "JY_GPU_DISABLE",
                          "JY_TTS_CONCURRENCY", "JY_ANALYSIS_WORKERS"}}
    clean.update(values)
    return mock.patch.dict(os.environ, clean, clear=True)


class ChooseProfileTests(unittest.TestCase):
    def test_env_override_beats_manifest_and_detection(self):
        with _env(JY_EXECUTION_PROFILE="gpu_balanced"), \
                mock.patch.object(resource_profile, "is_jianying_running", return_value=False), \
                mock.patch.object(resource_profile, "gpu_decode_available", return_value=False):
            profile = choose_profile({"execution_profile": "performance"})
        self.assertEqual(profile.name, "gpu_balanced")
        self.assertEqual((profile.analysis_workers, profile.tts_concurrency,
                          profile.staging_workers, profile.process_priority),
                         (2, 3, 1, "below_normal"))

    def test_invalid_env_falls_back_to_manifest(self):
        with _env(JY_EXECUTION_PROFILE="turbo"), \
                mock.patch.object(resource_profile, "is_jianying_running", return_value=True):
            self.assertEqual(choose_profile({"execution_profile": "performance"}).name, "performance")

    def test_manifest_override(self):
        with _env(), mock.patch.object(resource_profile, "is_jianying_running", return_value=False), \
                mock.patch.object(resource_profile, "gpu_decode_available", return_value=True):
            profile = choose_profile({"execution_profile": "background_friendly"})
        self.assertEqual(profile.name, "background_friendly")
        self.assertEqual(profile.analysis_workers, 1)

    def test_jianying_running_with_cuda_selects_gpu_balanced(self):
        with _env(), mock.patch.object(resource_profile, "is_jianying_running", return_value=True), \
                mock.patch.object(resource_profile, "gpu_decode_available", return_value=True):
            profile = choose_profile({})
        self.assertEqual((profile.name, profile.analysis_workers, profile.tts_concurrency,
                          profile.staging_workers), ("gpu_balanced", 4, 3, 1))

    def test_jianying_running_without_cuda_stays_friendly(self):
        with _env(), mock.patch.object(resource_profile, "is_jianying_running", return_value=True), \
                mock.patch.object(resource_profile, "gpu_decode_available", return_value=False):
            profile = choose_profile({})
        self.assertEqual((profile.name, profile.analysis_workers, profile.tts_concurrency),
                         ("background_friendly", 1, 3))

    def test_idle_editor_selects_performance(self):
        with _env(), mock.patch.object(resource_profile, "is_jianying_running", return_value=False), \
                mock.patch.object(resource_profile, "gpu_decode_available", return_value=False):
            profile = choose_profile({})
        self.assertEqual((profile.name, profile.analysis_workers, profile.tts_concurrency,
                          profile.staging_workers, profile.process_priority),
                         ("performance", 2, 3, 2, "normal"))

    def test_idle_editor_with_cuda_widens_analysis(self):
        with _env(), mock.patch.object(resource_profile, "is_jianying_running", return_value=False), \
                mock.patch.object(resource_profile, "gpu_decode_available", return_value=True):
            self.assertEqual(choose_profile({}).analysis_workers, 4)

    def test_env_overrides_tts_and_analysis_workers(self):
        with _env(JY_TTS_CONCURRENCY="5", JY_ANALYSIS_WORKERS="6"), \
                mock.patch.object(resource_profile, "is_jianying_running", return_value=True), \
                mock.patch.object(resource_profile, "gpu_decode_available", return_value=True):
            profile = choose_profile({})
        self.assertEqual((profile.analysis_workers, profile.tts_concurrency), (6, 5))

    def test_friendly_profile_ignores_analysis_widening(self):
        with _env(JY_EXECUTION_PROFILE="background_friendly", JY_ANALYSIS_WORKERS="6"), \
                mock.patch.object(resource_profile, "gpu_decode_available", return_value=True):
            self.assertEqual(choose_profile({}).analysis_workers, 1)


class GpuAvailabilityTests(unittest.TestCase):
    def test_gpu_disable_short_circuits_probe(self):
        with _env(JY_GPU_DISABLE="1"), \
                mock.patch("orchestrator.video_decode.gpu_decode_args") as probe:
            self.assertFalse(resource_profile.gpu_decode_available())
        probe.assert_not_called()

    def test_gpu_disable_makes_running_editor_friendly(self):
        with _env(JY_GPU_DISABLE="1"), \
                mock.patch.object(resource_profile, "is_jianying_running", return_value=True), \
                mock.patch("orchestrator.platform_env.find_ffmpeg",
                           return_value=(Path("ffmpeg.exe"), Path("ffprobe.exe"))), \
                mock.patch("orchestrator.video_decode.gpu_decode_args",
                           return_value=["-hwaccel", "cuda"]):
            self.assertEqual(choose_profile({}).name, "background_friendly")

    def test_cuda_probe_result_is_used(self):
        with _env(), mock.patch("orchestrator.platform_env.find_ffmpeg",
                                return_value=(Path("ffmpeg.exe"), Path("ffprobe.exe"))), \
                mock.patch("orchestrator.video_decode.gpu_decode_args",
                           return_value=["-threads", "1", "-hwaccel", "cuda"]):
            self.assertTrue(resource_profile.gpu_decode_available())
        with _env(), mock.patch("orchestrator.platform_env.find_ffmpeg",
                                return_value=(Path("ffmpeg.exe"), Path("ffprobe.exe"))), \
                mock.patch("orchestrator.video_decode.gpu_decode_args", return_value=[]):
            self.assertFalse(resource_profile.gpu_decode_available())

    def test_probe_exception_means_unavailable(self):
        with _env(), mock.patch("orchestrator.platform_env.find_ffmpeg",
                                side_effect=RuntimeError("boom")):
            self.assertFalse(resource_profile.gpu_decode_available())
        with _env(), mock.patch("orchestrator.platform_env.find_ffmpeg", return_value=(None, None)):
            self.assertFalse(resource_profile.gpu_decode_available())


if __name__ == "__main__":
    unittest.main()
