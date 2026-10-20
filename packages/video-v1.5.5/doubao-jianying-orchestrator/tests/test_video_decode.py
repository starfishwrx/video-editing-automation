"""视频解码：有 cuda 时走 GPU，失败再回退原来的 CPU 命令。"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import gpu_runtime, video_decode


def _proc(code: int, stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout="", stderr=stderr, args=[])


def isolate_gpu_runtime(case: unittest.TestCase) -> None:
    """槽位放进临时目录、显存视为充足、去掉 JY_GPU_DISABLE，避免碰真实 GPU。"""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    env = mock.patch.dict(os.environ, {"JY_SLOT_DIR": tmp.name, "JY_GPU_SLOTS": "3",
                                       "JY_GPU_DECODE_CODECS": "*",
                                       "JY_GPU_SINGLE_FRAME": "1"})
    env.start()
    case.addCleanup(env.stop)
    os.environ.pop("JY_GPU_DISABLE", None)
    os.environ.pop("JY_GPU_DECODE_SLOTS", None)
    os.environ.pop("JY_GPU_ENCODE_SLOTS", None)
    os.environ.pop("JY_GPU_CALL_LOG", None)
    vram = mock.patch.object(gpu_runtime, "vram_ok", return_value=True)
    vram.start()
    case.addCleanup(vram.stop)
    gpu_runtime.reset_stats()
    gpu_runtime.set_call_log(None)
    case.addCleanup(gpu_runtime.reset_stats)


class DecodeSelectionTests(unittest.TestCase):
    def setUp(self):
        isolate_gpu_runtime(self)

    def tearDown(self):
        video_decode.clear_decode_cache()

    def test_parse_hwaccel_names(self):
        text = "\n".join([
            "Hardware acceleration methods:",
            "cuda",
            "dxva2",
            "d3d11va",
        ])
        self.assertEqual(
            video_decode.parse_hwaccel_names(text),
            {"cuda", "dxva2", "d3d11va"},
        )

    def test_cuda_args_are_threads_and_hwaccel(self):
        with mock.patch.object(video_decode, "probe_hwaccels", return_value={"cuda", "d3d11va"}):
            cmd = video_decode.with_gpu_decode(
                ["ffmpeg", "-nostdin", "-ss", "0", "-i", "a.mov", "-f", "null", "-"])
        self.assertEqual(cmd[:5], ["ffmpeg", "-threads", "1", "-hwaccel", "cuda"])
        self.assertIn("-i", cmd)
        self.assertLess(cmd.index("-hwaccel"), cmd.index("-i"))

    def test_missing_cuda_keeps_cpu_command(self):
        with mock.patch.object(video_decode, "probe_hwaccels", return_value={"d3d11va"}):
            original = ["ffmpeg", "-i", "a.mov"]
            self.assertEqual(video_decode.with_gpu_decode(original), original)
            self.assertEqual(video_decode.gpu_decode_args("ffmpeg"), [])

    def test_success_does_not_retry(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return _proc(0, "Selecting decoder 'hevc' because of requested hwaccel method cuda\n")

        with mock.patch.object(video_decode, "probe_hwaccels", return_value={"cuda"}), \
                mock.patch.object(video_decode.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(io.StringIO()) as out:
            video_decode.run_ffmpeg_decode(["ffmpeg", "-i", "a.mov", "-f", "null", "-"])
        self.assertEqual(len(calls), 1)
        self.assertIn("-hwaccel", calls[0])
        self.assertIn("cuda hwaccel", out.getvalue())

    def test_nonzero_retries_without_hwaccel(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            if "-hwaccel" in cmd:
                return _proc(1, "CUDA_ERROR_INVALID_VALUE")
            return _proc(0, "cpu")

        with mock.patch.object(video_decode, "probe_hwaccels", return_value={"cuda"}), \
                mock.patch.object(video_decode.subprocess, "run", side_effect=fake_run):
            proc = video_decode.run_ffmpeg_decode(
                ["ffmpeg", "-ss", "0", "-i", "a.mov"], check=True)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("-hwaccel", calls[1])
        self.assertEqual(calls[1], ["ffmpeg", "-ss", "0", "-i", "a.mov"])

    def test_silent_cpu_fallback_is_not_decoded_twice(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _proc(0, "Failed setup for format cuda: hwaccel initialisation returned error.")

        with mock.patch.object(video_decode, "probe_hwaccels", return_value={"cuda"}), \
                mock.patch.object(video_decode.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(io.StringIO()) as out:
            video_decode.run_ffmpeg_decode(["ffmpeg", "-i", "a.mov"])
        self.assertEqual(len(calls), 1)
        self.assertIn("CPU", out.getvalue())


if __name__ == "__main__":
    unittest.main()
