"""GPU 运行时：槽位超时、显存不足、JY_GPU_DISABLE 都走 CPU，统计与调用日志可审计。"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import gpu_runtime, video_decode, video_encode
from orchestrator.proc_slots import SlotHandle
from test_video_decode import isolate_gpu_runtime


def _proc(code: int, stdout: str = "", stderr: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr, args=[])


@contextmanager
def _timed_out_slot(kind="decode"):
    yield SlotHandle("gpu", None, 0.5)


class VramTests(unittest.TestCase):
    def setUp(self):
        gpu_runtime.reset_stats()
        self.addCleanup(gpu_runtime.reset_stats)
        env = mock.patch.dict(os.environ, {"JY_GPU_MIN_FREE_MB": "768"})
        env.start()
        self.addCleanup(env.stop)

    def test_low_free_memory_blocks_gpu(self):
        with mock.patch.object(gpu_runtime.subprocess, "run", return_value=_proc(0, "512\n")):
            self.assertEqual(gpu_runtime.query_free_vram_mb(), 512)
            self.assertFalse(gpu_runtime.vram_ok())

    def test_enough_memory_allows_gpu_and_is_cached(self):
        with mock.patch.object(gpu_runtime.subprocess, "run",
                               return_value=_proc(0, "4096\n")) as run:
            self.assertTrue(gpu_runtime.vram_ok())
            self.assertTrue(gpu_runtime.vram_ok())
        self.assertEqual(run.call_count, 1)
        args = run.call_args[0][0]
        self.assertEqual(args[0], "nvidia-smi")
        self.assertEqual(run.call_args[1]["timeout"], 5)

    def test_missing_or_garbled_nvidia_smi_allows_gpu(self):
        with mock.patch.object(gpu_runtime.subprocess, "run", side_effect=FileNotFoundError):
            self.assertTrue(gpu_runtime.vram_ok())
        gpu_runtime.reset_stats()
        with mock.patch.object(gpu_runtime.subprocess, "run",
                               return_value=_proc(0, "[N/A]\n")):
            self.assertIsNone(gpu_runtime.query_free_vram_mb())
            self.assertTrue(gpu_runtime.vram_ok())


class StatsTests(unittest.TestCase):
    def setUp(self):
        isolate_gpu_runtime(self)

    def test_snapshot_counts_and_hit_rates(self):
        gpu_runtime.record_decode("cuda", "gpu_ok", 2.0)
        gpu_runtime.record_decode("cuda", "gpu_ok", 1.0)
        gpu_runtime.record_decode("cpu", "slot_timeout", 4.0, slot_wait=1.5)
        gpu_runtime.record_decode("cpu", "vram_low", 3.0)
        gpu_runtime.record_encode("h264_nvenc", None, 5.0)
        gpu_runtime.record_encode("libx264", "h264_nvenc", 9.0)
        snap = gpu_runtime.snapshot()
        self.assertEqual(snap["decode"]["calls"], 4)
        self.assertEqual(snap["decode"]["outcomes"]["gpu_ok"], 2)
        self.assertEqual(snap["decode"]["outcomes"]["slot_timeout"], 1)
        self.assertEqual(snap["decode"]["outcomes"]["gpu_fail_rerun"], 0)
        self.assertEqual(snap["decode"]["seconds"], {"cuda": 3.0, "cpu": 7.0})
        self.assertEqual(snap["decode"]["gpu_hit_rate"], 0.5)
        self.assertEqual(snap["encode"]["encoders"], {"h264_nvenc": 1, "libx264": 1})
        self.assertEqual(snap["encode"]["fallbacks"], 1)
        self.assertEqual(snap["encode"]["gpu_hit_rate"], 0.5)
        self.assertEqual(snap["slot_wait_s"], 1.5)
        json.dumps(snap)
        gpu_runtime.reset_stats()
        empty = gpu_runtime.snapshot()
        self.assertEqual(empty["decode"]["calls"], 0)
        self.assertIsNone(empty["decode"]["gpu_hit_rate"])

    def test_call_log_writes_one_line_per_call(self):
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "sub" / "gpu_calls.jsonl"
            gpu_runtime.set_call_log(log)
            try:
                gpu_runtime.record_decode("cuda", "gpu_ok", 1.25, slot_wait=0.5)
                gpu_runtime.record_encode("libx264", "h264_nvenc", 2.0, outcome="vram_low")
            finally:
                gpu_runtime.set_call_log(None)
            rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["kind"], "decode")
        self.assertEqual(rows[0]["backend"], "cuda")
        self.assertEqual(rows[0]["outcome"], "gpu_ok")
        self.assertEqual(rows[0]["slot_wait"], 0.5)
        self.assertIn("ts", rows[0])
        self.assertEqual(rows[1]["kind"], "encode")
        self.assertEqual(rows[1]["encoder"], "libx264")
        self.assertEqual(rows[1]["outcome"], "vram_low")

    def test_env_call_log_and_unwritable_log_never_raise(self):
        with tempfile.TemporaryDirectory() as raw:
            log = Path(raw) / "env.jsonl"
            with mock.patch.dict(os.environ, {"JY_GPU_CALL_LOG": str(log)}):
                gpu_runtime.record_decode("cpu", "cpu_only", 0.1)
            self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)
            gpu_runtime.set_call_log(Path(raw))
            try:
                gpu_runtime.record_decode("cpu", "cpu_only", 0.1)
            finally:
                gpu_runtime.set_call_log(None)

    def test_gpu_slot_respects_capacity(self):
        with mock.patch.dict(os.environ, {"JY_GPU_SLOTS": "1", "JY_GPU_SLOT_TIMEOUT": "0.3"}):
            with gpu_runtime.gpu_slot() as first:
                self.assertTrue(first.acquired)
                with gpu_runtime.gpu_slot() as second:
                    self.assertFalse(second.acquired)
            with gpu_runtime.gpu_slot() as again:
                self.assertTrue(again.acquired)


class DecodeRoutingTests(unittest.TestCase):
    def setUp(self):
        isolate_gpu_runtime(self)
        probe = mock.patch.object(video_decode, "probe_hwaccels", return_value={"cuda"})
        probe.start()
        self.addCleanup(probe.stop)
        self.addCleanup(video_decode.clear_decode_cache)
        self.calls = []

    def _fake_run(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        return _proc(0, stderr="ok")

    def _decode(self):
        with mock.patch.object(video_decode.subprocess, "run", side_effect=self._fake_run), \
                redirect_stdout(io.StringIO()) as out:
            video_decode.run_ffmpeg_decode(["ffmpeg", "-i", "a.mov", "-f", "null", "-"])
        return out.getvalue()

    def test_slot_timeout_runs_cpu(self):
        with mock.patch.object(gpu_runtime, "gpu_slot", _timed_out_slot):
            text = self._decode()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("-hwaccel", self.calls[0])
        self.assertIn("槽位", text)
        snap = gpu_runtime.snapshot()
        self.assertEqual(snap["decode"]["outcomes"]["slot_timeout"], 1)
        self.assertEqual(snap["slot_wait_s"], 0.5)

    def test_vram_low_runs_cpu(self):
        with mock.patch.object(gpu_runtime, "vram_ok", return_value=False):
            self._decode()
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("-hwaccel", self.calls[0])
        self.assertEqual(gpu_runtime.snapshot()["decode"]["outcomes"]["vram_low"], 1)

    def test_disable_env_forces_cpu_without_vram_query(self):
        with mock.patch.dict(os.environ, {"JY_GPU_DISABLE": "1"}), \
                mock.patch.object(gpu_runtime, "vram_ok",
                                  side_effect=AssertionError("must not query")):
            self._decode()
        self.assertNotIn("-hwaccel", self.calls[0])
        self.assertEqual(gpu_runtime.snapshot()["decode"]["outcomes"]["cpu_only"], 1)

    def test_gpu_ok_and_rerun_are_recorded(self):
        self._decode()
        self.assertIn("-hwaccel", self.calls[0])

        def failing(cmd, **kwargs):
            self.calls.append(list(cmd))
            return _proc(1 if "-hwaccel" in cmd else 0, stderr="CUDA_ERROR")

        with mock.patch.object(video_decode.subprocess, "run", side_effect=failing), \
                redirect_stdout(io.StringIO()):
            video_decode.run_ffmpeg_decode(["ffmpeg", "-i", "a.mov"])
        snap = gpu_runtime.snapshot()
        self.assertEqual(snap["decode"]["outcomes"]["gpu_ok"], 1)
        self.assertEqual(snap["decode"]["outcomes"]["gpu_fail_rerun"], 1)
        self.assertEqual(snap["decode"]["gpu_hit_rate"], 0.5)

    def test_slot_is_released_before_cpu_rerun(self):
        held = []

        @contextmanager
        def tracking_slot(kind="decode"):
            held.append(True)
            try:
                yield SlotHandle("gpu", 0, 0.0)
            finally:
                held.pop()

        def failing(cmd, **kwargs):
            if "-hwaccel" not in cmd:
                self.assertEqual(held, [], "CPU 重跑时不应占着 GPU 槽位")
            return _proc(1 if "-hwaccel" in cmd else 0)

        with mock.patch.object(gpu_runtime, "gpu_slot", tracking_slot), \
                mock.patch.object(video_decode.subprocess, "run", side_effect=failing), \
                redirect_stdout(io.StringIO()):
            proc = video_decode.run_ffmpeg_decode(["ffmpeg", "-i", "a.mov"])
        self.assertEqual(proc.returncode, 0)

    def test_no_cuda_records_cpu_only(self):
        with mock.patch.object(video_decode, "probe_hwaccels", return_value={"d3d11va"}):
            self._decode()
        self.assertEqual(gpu_runtime.snapshot()["decode"]["outcomes"]["cpu_only"], 1)


class EncodeRoutingTests(unittest.TestCase):
    def setUp(self):
        isolate_gpu_runtime(self)
        probe = mock.patch.object(video_encode, "probe_video_encoders",
                                  return_value={"h264_nvenc", "libx264"})
        probe.start()
        self.addCleanup(probe.stop)
        self.addCleanup(video_encode.clear_encoder_cache)
        self.encoders = []

    def _fake_run(self, cmd, **kwargs):
        encoder = cmd[cmd.index("-c:v") + 1]
        self.encoders.append(encoder)
        Path(cmd[-1]).write_bytes(b"mp4")
        return mock.Mock(returncode=0, stdout="", stderr="")

    def _encode(self):
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch("orchestrator.video_encode.subprocess.run",
                            side_effect=self._fake_run), redirect_stdout(io.StringIO()):
                name, _proc = video_encode.run_h264_encode(
                    "ffmpeg", ["-y", "-i", "in.mp4"], Path(raw) / "out.mp4", timeout=5)
        return name

    def test_gpu_encode_recorded(self):
        self.assertEqual(self._encode(), "h264_nvenc")
        snap = gpu_runtime.snapshot()
        self.assertEqual(snap["encode"]["encoders"], {"h264_nvenc": 1})
        self.assertEqual(snap["encode"]["gpu_hit_rate"], 1.0)

    def test_slot_timeout_skips_to_libx264(self):
        with mock.patch.object(gpu_runtime, "gpu_slot", _timed_out_slot):
            self.assertEqual(self._encode(), "libx264")
        self.assertEqual(self.encoders, ["libx264"])
        snap = gpu_runtime.snapshot()
        self.assertEqual(snap["encode"]["encoders"], {"libx264": 1})
        self.assertEqual(snap["encode"]["outcomes"], {"slot_timeout": 1})
        self.assertEqual(snap["encode"]["fallbacks"], 1)

    def test_vram_low_and_disable_skip_gpu(self):
        with mock.patch.object(gpu_runtime, "vram_ok", return_value=False):
            self.assertEqual(self._encode(), "libx264")
        with mock.patch.dict(os.environ, {"JY_GPU_DISABLE": "1"}):
            self.assertEqual(self._encode(), "libx264")
        self.assertEqual(self.encoders, ["libx264", "libx264"])
        outcomes = gpu_runtime.snapshot()["encode"]["outcomes"]
        self.assertEqual(outcomes, {"vram_low": 1, "gpu_disabled": 1})

    def test_runtime_gpu_failure_records_fallback(self):
        def fake_run(cmd, **kwargs):
            encoder = cmd[cmd.index("-c:v") + 1]
            self.encoders.append(encoder)
            if encoder == "libx264":
                Path(cmd[-1]).write_bytes(b"mp4")
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="Cannot load nvcuda")

        self._fake_run = fake_run
        self.assertEqual(self._encode(), "libx264")
        snap = gpu_runtime.snapshot()
        self.assertEqual(snap["encode"]["outcomes"], {"fallback": 1})
        self.assertEqual(snap["encode"]["gpu_hit_rate"], 0.0)

    def test_skipped_gpu_is_last_resort_when_cpu_fails(self):
        def fake_run(cmd, **kwargs):
            encoder = cmd[cmd.index("-c:v") + 1]
            self.encoders.append(encoder)
            if encoder == "h264_nvenc":
                Path(cmd[-1]).write_bytes(b"mp4")
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="no libx264")

        self._fake_run = fake_run
        with mock.patch.object(gpu_runtime, "gpu_slot", _timed_out_slot):
            self.assertEqual(self._encode(), "h264_nvenc")
        self.assertEqual(self.encoders, ["libx264", "h264_nvenc"])


class DecodeRouteTests(unittest.TestCase):
    """默认全部走 GPU；收窄 CODECS / 关闭单帧时才回 CPU。"""

    def setUp(self):
        isolate_gpu_runtime(self)
        # 默认是 *；本套用例先钉成 hevc，方便测 codec 收窄。
        env = mock.patch.dict(os.environ, {"JY_GPU_DECODE_CODECS": "hevc",
                                           "JY_GPU_SINGLE_FRAME": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(video_decode.clear_decode_cache)

    def _route(self, cmd, codec):
        with mock.patch.object(gpu_runtime, "probe_video_codec", return_value=codec):
            return gpu_runtime.decode_route(cmd)

    def test_hevc_may_use_gpu(self):
        self.assertIsNone(self._route(["ffmpeg", "-i", "a.mov", "-f", "null", "-"], "hevc"))

    def test_h264_goes_to_cpu_when_codecs_narrowed(self):
        self.assertEqual(self._route(["ffmpeg", "-i", "a.mp4", "-f", "null", "-"], "h264"),
                         "codec_cpu")

    def test_unknown_codec_keeps_gpu_attempt(self):
        self.assertIsNone(self._route(["ffmpeg", "-i", "a.mov", "-f", "null", "-"], None))

    def test_single_frame_defaults_to_gpu_unless_disabled(self):
        cmd = ["ffmpeg", "-ss", "1", "-i", "a.mov", "-frames:v", "1", "o.jpg"]
        self.assertEqual(self._route(cmd, "hevc"), "single_frame_cpu")
        with mock.patch.dict(os.environ, {"JY_GPU_SINGLE_FRAME": "1"}):
            self.assertIsNone(self._route(cmd, "hevc"))

    def test_default_allows_all_codecs_and_single_frame(self):
        clean = {k: v for k, v in os.environ.items()
                 if k not in {"JY_GPU_DECODE_CODECS", "JY_GPU_SINGLE_FRAME"}}
        with mock.patch.dict(os.environ, clean, clear=True):
            self.assertIsNone(gpu_runtime.decode_codecs())
            self.assertTrue(gpu_runtime.single_frame_gpu())
            self.assertIsNone(self._route(
                ["ffmpeg", "-i", "a.mp4", "-f", "null", "-"], "h264"))
            self.assertIsNone(self._route(
                ["ffmpeg", "-ss", "1", "-i", "a.mov", "-frames:v", "1", "o.jpg"], "hevc"))

    def test_wildcard_allows_all_codecs(self):
        with mock.patch.dict(os.environ, {"JY_GPU_DECODE_CODECS": "*"}):
            self.assertIsNone(self._route(["ffmpeg", "-i", "a.mp4", "-f", "null", "-"], "h264"))

    def test_routed_decode_runs_once_on_cpu_and_is_counted(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(list(cmd))
            return _proc(0)

        cmd = ["ffmpeg", "-i", "a.mp4", "-f", "null", "-"]
        with mock.patch.object(video_decode, "gpu_decode_args",
                               return_value=["-threads", "1", "-hwaccel", "cuda"]), \
                mock.patch.object(gpu_runtime, "probe_video_codec", return_value="h264"), \
                mock.patch.object(video_decode.subprocess, "run", side_effect=fake_run), \
                redirect_stdout(io.StringIO()):
            video_decode.run_ffmpeg_decode(cmd, capture_output=True)
        self.assertEqual(calls, [cmd])
        self.assertEqual(gpu_runtime.snapshot()["decode"]["outcomes"]["codec_cpu"], 1)


class SlotPoolTests(unittest.TestCase):
    def setUp(self):
        self.env = {k: v for k, v in os.environ.items()
                    if k not in {"JY_GPU_SLOTS", "JY_GPU_DECODE_SLOTS", "JY_GPU_ENCODE_SLOTS"}}

    def test_default_pools(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(gpu_runtime.slot_capacity("decode"), 6)
            self.assertEqual(gpu_runtime.slot_capacity("encode"), 6)

    def test_legacy_variable_sets_decode_pool(self):
        with mock.patch.dict(os.environ, {**self.env, "JY_GPU_SLOTS": "5"}, clear=True):
            self.assertEqual(gpu_runtime.slot_capacity("decode"), 5)
        with mock.patch.dict(os.environ, {**self.env, "JY_GPU_SLOTS": "5",
                                          "JY_GPU_DECODE_SLOTS": "10"}, clear=True):
            self.assertEqual(gpu_runtime.slot_capacity("decode"), 10)

    def test_encode_pool_is_independent(self):
        with mock.patch.dict(os.environ, {**self.env, "JY_GPU_ENCODE_SLOTS": "2"}, clear=True):
            self.assertEqual(gpu_runtime.slot_capacity("encode"), 2)
            self.assertEqual(gpu_runtime.slot_capacity("decode"), 6)


if __name__ == "__main__":
    unittest.main()
