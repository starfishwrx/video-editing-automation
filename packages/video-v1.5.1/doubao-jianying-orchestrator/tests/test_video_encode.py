"""视频编码器选择：GPU 优先，不可用时必须回到 libx264。"""
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import video_encode
from orchestrator.engine import OrchestrationEngine


class EncoderSelectionTests(unittest.TestCase):
    def tearDown(self):
        video_encode.clear_encoder_cache()

    def test_parse_ignores_legend_and_keeps_h264_names(self):
        text = "\n".join([
            "Encoders:",
            " V..... = Video",
            " ------",
            " V....D libx264              libx264 H.264",
            " V....D h264_nvenc           NVIDIA NVENC H.264 encoder",
            " V..... h264_qsv             H.264 Intel Quick Sync",
        ])
        self.assertEqual(
            video_encode.parse_encoder_names(text),
            {"libx264", "h264_nvenc", "h264_qsv"},
        )

    def test_cpu_fallback_when_gpu_encoders_missing(self):
        with mock.patch.object(
            video_encode, "probe_video_encoders",
            return_value={"libx264", "h264_mf", "mpeg4"},
        ):
            plan = video_encode.encoder_plan("ffmpeg")
        self.assertEqual([item.encoder for item in plan], ["libx264", "h264_mf", "mpeg4"])
        self.assertTrue(all(item.ffmpeg == "ffmpeg" for item in plan))
        args = plan[0].args
        self.assertEqual(args[args.index("-c:v") + 1], "libx264")
        self.assertIn("-crf", args)
        self.assertNotIn("-cq", args)
        self.assertNotIn("h264_nvenc", args)
        self.assertNotIn("ultrafast", args)

    def test_prefers_nvenc_vbr_cq_not_cpu_crf(self):
        with mock.patch.object(
            video_encode, "probe_video_encoders",
            return_value={"h264_nvenc", "h264_amf", "h264_qsv", "libx264", "mpeg4"},
        ):
            plan = video_encode.encoder_plan("ffmpeg")
        self.assertEqual(plan[0].encoder, "h264_nvenc")
        self.assertEqual(plan[1].encoder, "libx264")
        self.assertEqual(plan[1].ffmpeg, "ffmpeg")
        args = plan[0].args
        self.assertIn("yuv420p", args)
        self.assertIn("p4", args)
        self.assertEqual(args[args.index("-rc") + 1], "vbr")
        self.assertIn("-cq", args)
        self.assertIn("-b:v", args)
        self.assertNotIn("-crf", args)
        self.assertNotIn("ultrafast", args)

    def test_amf_then_qsv_when_nvenc_absent(self):
        with mock.patch.object(
            video_encode, "probe_video_encoders",
            return_value={"h264_qsv", "libx264"},
        ):
            qsv_plan = video_encode.encoder_plan("ffmpeg")
        self.assertEqual(qsv_plan[0].encoder, "h264_qsv")
        self.assertIn("medium", qsv_plan[0].args)
        with mock.patch.object(
            video_encode, "probe_video_encoders",
            return_value={"h264_amf", "h264_qsv", "libx264"},
        ):
            amf_plan = video_encode.encoder_plan("ffmpeg")
        self.assertEqual(amf_plan[0].encoder, "h264_amf")

    def test_missing_libx264_uses_another_ffmpeg(self):
        with mock.patch.object(
            video_encode, "probe_video_encoders",
            return_value={"h264_nvenc", "h264_mf", "mpeg4"},
        ), mock.patch.object(
            video_encode, "find_libx264_ffmpeg",
            return_value=r"C:\ffmpeg\ffmpeg.exe",
        ):
            plan = video_encode.encoder_plan(r"D:\JianyingPro\ffmpeg.exe")
        self.assertEqual(
            [(item.ffmpeg, item.encoder) for item in plan],
            [
                (r"D:\JianyingPro\ffmpeg.exe", "h264_nvenc"),
                (r"C:\ffmpeg\ffmpeg.exe", "libx264"),
                (r"D:\JianyingPro\ffmpeg.exe", "h264_mf"),
                (r"D:\JianyingPro\ffmpeg.exe", "mpeg4"),
            ],
        )
        self.assertIn("-crf", plan[1].args)
        self.assertNotIn("-cq", plan[1].args)

    def test_runtime_gpu_failure_falls_back_to_libx264(self):
        def fake_run(cmd, **kwargs):
            encoder = cmd[cmd.index("-c:v") + 1]
            dest = Path(cmd[-1])
            if encoder == "libx264":
                dest.write_bytes(b"mp4-ok")
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=1, stdout="", stderr="Cannot load nvcuda")

        with tempfile.TemporaryDirectory() as raw:
            out = Path(raw) / "out.mp4"
            with mock.patch.object(
                video_encode, "probe_video_encoders",
                return_value={"h264_nvenc", "libx264"},
            ), mock.patch("orchestrator.video_encode.subprocess.run", side_effect=fake_run):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    name, _proc = video_encode.run_h264_encode(
                        "ffmpeg", ["-y", "-i", "in.mp4"], out, timeout=5,
                    )
            self.assertEqual(name, "libx264")
            self.assertEqual(out.read_bytes(), b"mp4-ok")
            text = buf.getvalue()
            self.assertIn("libx264", text)
            self.assertIn("CPU 回退", text)
            self.assertIn("h264_nvenc", text)

    def test_placeholder_uses_shared_encoder(self):
        events = []

        def fake_run(ffmpeg, leading, output, trailing=None, timeout=None):
            Path(output).write_bytes(b"mp4")
            return "h264_nvenc", mock.Mock()

        with tempfile.TemporaryDirectory() as raw:
            stub = SimpleNamespace(
                report_dir=Path(raw),
                ffmpeg=Path("ffmpeg"),
                m={"fps": 30},
                _visual_missing_placeholder_s=0.0,
                _fps=lambda: 30.0,
                _event=lambda name, **fields: events.append((name, fields)),
            )
            with mock.patch("orchestrator.engine.run_h264_encode", side_effect=fake_run):
                path = OrchestrationEngine._ensure_visual_missing_placeholder(stub, 1.0)
            self.assertEqual(path.name, "visual_missing_placeholder.mp4")
            self.assertGreater(path.stat().st_size, 0)
            self.assertEqual(events[0][0], "visual_missing_placeholder_encoder")
            self.assertEqual(events[0][1]["encoder"], "h264_nvenc")


class WebmNormalizerTests(unittest.TestCase):
    def test_webm_transcode_delegates_codec_choice(self):
        scripts = Path(__file__).resolve().parents[2] / "jianying-editor" / "scripts"
        sys.path.insert(0, str(scripts))
        from utils import media_normalizer

        seen = {}

        def fake_run(ffmpeg, leading, output, trailing=None, timeout=None):
            seen["ffmpeg"] = ffmpeg
            seen["leading"] = list(leading)
            seen["trailing"] = list(trailing or [])
            Path(output).write_bytes(b"mp4")
            return "h264_nvenc", None

        with tempfile.TemporaryDirectory() as raw:
            src = Path(raw) / "clip.webm"
            src.write_bytes(b"webm")
            with mock.patch.object(
                media_normalizer, "_encoder_api",
                return_value=(fake_run, r"C:\ffmpeg\ffmpeg.exe"),
            ):
                out = media_normalizer.normalize_webm_for_jianying(str(src))
            self.assertTrue(out and out.endswith(".__jy_norm__.mp4"))
            self.assertNotIn("libx264", seen["leading"])
            self.assertNotIn("-c:v", seen["leading"])
            self.assertEqual(seen["trailing"][:2], ["-c:a", "aac"])
            self.assertEqual(seen["ffmpeg"], r"C:\ffmpeg\ffmpeg.exe")


if __name__ == "__main__":
    unittest.main()
