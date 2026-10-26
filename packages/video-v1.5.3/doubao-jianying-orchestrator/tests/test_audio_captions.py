import sys
import tempfile
import unittest
import wave
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import audio_captions
from orchestrator.audio_captions import build_captions
from orchestrator.engine import Clip


def make_wav(path: Path, frames=16000, value=1200):
    import struct
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(struct.pack("<h", value) * frames)


class AudioCaptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.audio = self.root / "voice.wav"
        make_wav(self.audio, frames=32000)
        # Cache tests must be independent of downloaded models and optional ASR
        # dependencies on CI. Recognition itself is explicitly injected below.
        self.mock_models = self.root / "mock-models"
        for name in ("small", "medium"):
            model = self.mock_models / name
            model.mkdir(parents=True)
            (model / "model.bin").write_bytes(name.encode())
        resolver = patch.object(audio_captions, "_resolve_cached_model",
                                side_effect=self._local_test_model)
        resolver.start()
        self.addCleanup(resolver.stop)

    def _local_test_model(self, name):
        path = Path(name)
        return path if path.exists() else self.mock_models / name

    def tearDown(self):
        self.tmp.cleanup()

    def transcriber(self, words, text, model="mock-small"):
        def run(path, *, model_name, language=None, hotwords=None):
            device, compute_type = audio_captions._preferred_backend()
            return {"text": text, "words": words, "model": model,
                    "device": device, "compute_type": compute_type}
        return run

    def test_normalized_timeline_uses_clip_start_duration_and_audio_slice(self):
        clips = [
            {"text": "心相印", "parent_index": 0, "audio_clip_index": 0,
             "tts_path": str(self.audio), "audio_mode": "tts", "start_us": 0,
             "duration": 1.0, "audio_clip_start_us": 0},
            {"text": "心相印", "parent_index": 0, "audio_clip_index": 0,
             "tts_path": str(self.audio), "audio_mode": "tts", "start_us": 1_000_000,
             "duration": 1.0, "audio_clip_start_us": 0, "multi_shot": True},
        ]
        mocked = self.transcriber([{"text": "心", "start": 0.1, "end": 0.3},
                                   {"text": "相", "start": 0.3, "end": 0.5},
                                   {"text": "印", "start": 0.5, "end": 0.8}], "心相印")
        result = build_captions({"script": "心相印"}, clips, self.root / "report", "ffmpeg", transcribe=mocked)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["cues"], [{"text": "心相印", "start_us": 100_000, "end_us": 800_000}])
        # One cue covers the narrated sentence; it is not duplicated for each shot.
        self.assertEqual(len(result["cues"]), 1)

    def test_mismatch_fails_closed_instead_of_filling_from_original(self):
        clips = [{"text": "心相印湿厕纸", "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        mocked = self.transcriber([{"text": "你好", "start": 0.1, "end": 0.5}], "你好")
        result = build_captions({"script": "心相印湿厕纸"}, clips, self.root / "report", "ffmpeg", transcribe=mocked)
        self.assertFalse(result["ok"])
        self.assertEqual(result["cues"], [])
        self.assertIn("mismatch", result["issues"][0])
        report = __import__("json").loads((self.root / "report" / "audio_captions.json").read_text(encoding="utf-8"))
        self.assertEqual(report["asr_text"], "你好")
        self.assertEqual(report["asr_word_timestamps"], [{"text": "你好", "start_us": 100_000, "end_us": 500_000}])

    def test_asr_hotwords_come_only_from_reference_protected_terms_and_declared_brands(self):
        clips = [{"text": "这是心相印", "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        seen = {}
        def mocked(path, *, model_name, language=None, hotwords=None):
            seen["hotwords"] = hotwords
            return {"text": "这是心相印", "words": [{"text": "这是", "start": 0.1, "end": 0.3},
                                                   {"text": "心相印", "start": 0.4, "end": 0.9}],
                    "model": "mock", "device": audio_captions._preferred_backend()[0],
                    "compute_type": audio_captions._preferred_backend()[1]}
        result = build_captions({"script": "这是心相印", "brand_names": ["大王品牌"]}, clips,
                                self.root / "report", "ffmpeg", transcribe=mocked)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(seen["hotwords"], ["大王品牌", "心相印"])
        self.assertNotIn("湿厕纸", seen["hotwords"], "unmentioned KEEP_WORDS must not be hinted")

    def test_low_similarity_real_asr_error_case_fails_review_and_hints_product(self):
        reference = "心相印的绒立方厨房纸。完全可以当一块抹布来用"
        recognized = "心相印的荣利方厨方纸完全可以当一块妈布来用"
        clips = [{"text": reference, "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        words = [{"text": recognized, "start": 0.1, "end": 1.6}]
        seen = {}
        def mocked(path, *, model_name, language=None, hotwords=None):
            seen["hotwords"] = hotwords
            return {"text": recognized, "words": words,
                    "device": audio_captions._preferred_backend()[0],
                    "compute_type": audio_captions._preferred_backend()[1]}
        result = build_captions({"script": reference, "product": "绒立方厨房纸"}, clips,
                                self.root / "report", "ffmpeg", transcribe=mocked)
        self.assertFalse(result["ok"])
        self.assertEqual(result["cues"], [])
        self.assertIn("minimum=0.95", result["issues"][0])
        self.assertIn("绒立方厨房纸", seen["hotwords"])
        report = __import__("json").loads((self.root / "report" / "audio_captions.json").read_text(encoding="utf-8"))
        self.assertEqual(report["asr_text"], recognized)
        self.assertEqual(report["asr_word_timestamps"][0]["text"], recognized)

    def test_chinese_and_arabic_numerals_with_same_value_are_equivalent(self):
        for reference, recognized in (("含量一百四十", "含量140"), ("五包", "5包")):
            with self.subTest(reference=reference):
                clips = [{"text": reference, "parent_index": 0, "tts_path": str(self.audio),
                          "audio_mode": "tts", "start_us": 0, "duration": 2}]
                result = build_captions({"script": reference}, clips, self.root / "report", "ffmpeg",
                                        transcribe=self.transcriber([{"text": recognized, "start": 0.1,
                                                                      "end": 0.8}], recognized))
                self.assertTrue(result["ok"], result["issues"])

    def test_invalid_asr_timeout_fails_closed(self):
        clips = [{"text": "你好", "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        for timeout in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(timeout=timeout):
                result = build_captions({"script": "你好", "asr_timeout_s": timeout}, clips,
                                        self.root / "report", "ffmpeg", transcribe=self.transcriber([], ""))
                self.assertFalse(result["ok"])
                self.assertIn("finite positive", result["issues"][0])

    def test_real_backend_timeout_is_reported_without_success_or_stale_cues(self):
        clips = [{"text": "超时测试", "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        original_run = audio_captions.subprocess.run
        def bounded_run(command, **kwargs):
            if "orchestrator.audio_captions" in command:
                raise __import__("subprocess").TimeoutExpired(command, kwargs["timeout"])
            return original_run(command, **kwargs)
        with patch.object(audio_captions.subprocess, "run", side_effect=bounded_run):
            result = build_captions({"script": "超时测试", "asr_timeout_s": 0.01}, clips,
                                    self.root / "report", "ffmpeg")
        self.assertFalse(result["ok"])
        self.assertEqual(result["cues"], [])
        self.assertIn("ASR_TIMEOUT", result["issues"][0])
        report = __import__("json").loads((self.root / "report" / "audio_captions.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "timeout")
        self.assertFalse((self.root / "report" / "audio_captions.cache.json").exists())

    def test_protected_brand_tokens_are_not_split(self):
        clips = [{"text": "心相印来了", "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        mocked = self.transcriber([{"text": "心相", "start": 0.1, "end": 0.4},
                                   {"text": "印", "start": 0.4, "end": 0.6},
                                   {"text": "来", "start": 0.7, "end": 0.9},
                                   {"text": "了", "start": 0.9, "end": 1.0}], "心相印来了")
        result = build_captions({"script": "心相印来了"}, clips, self.root / "report", "ffmpeg", transcribe=mocked)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual("".join(c["text"] for c in result["cues"]), "心相印来了")

    def test_missing_brand_or_changed_digit_requires_review_without_filling_from_script(self):
        for reference, recognized in (("心相印来了", "心相来了"), ("含量99%", "含量98%")):
            with self.subTest(reference=reference, recognized=recognized):
                clips = [{"text": reference, "parent_index": 0, "tts_path": str(self.audio),
                          "audio_mode": "tts", "start_us": 0, "duration": 2}]
                words = [{"text": recognized, "start": 0.1, "end": 1.0}]
                result = build_captions({"script": reference}, clips, self.root / "report", "ffmpeg",
                                        transcribe=self.transcriber(words, recognized))
                self.assertFalse(result["ok"])
                self.assertEqual(result["cues"], [])

    def test_asr_transcript_must_match_timestamped_word_text(self):
        clips = [{"text": "你好", "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        result = build_captions({"script": "你好"}, clips, self.root / "report", "ffmpeg",
                                transcribe=self.transcriber([{"text": "再见", "start": 0.1, "end": 0.6}], "你好"))
        self.assertFalse(result["ok"])
        self.assertIn("word timestamp text mismatch", result["issues"][0])

    def test_two_sentences_across_real_clip_shape_keep_word_timing_and_one_phrase(self):
        # Mirrors engine.Clip: first and second shots share one narrated parent and
        # the latter slices from its true offset, while captions span the whole line.
        clips = [
            Clip(video="", source_start=0, duration=1.0, text="你好。世界！",
                 parent_index=0, raw_index=0, audio_clip_index=0,
                 tts_path=str(self.audio), audio_mode="tts", start_us=0,
                 audio_clip_start_us=0, multi_shot=True),
            Clip(video="", source_start=0, duration=1.0, text="你好。世界！",
                 parent_index=0, raw_index=0, audio_clip_index=0,
                 tts_path=str(self.audio), audio_mode="tts", start_us=1_000_000,
                 audio_clip_start_us=0, multi_shot=True),
        ]
        words = [{"text": "你好", "start": 0.1, "end": 0.4},
                 {"text": "。", "start": 0.4, "end": 0.45},
                 {"text": "世界", "start": 1.1, "end": 1.5},
                 {"text": "！", "start": 1.5, "end": 1.55}]
        result = build_captions({"script": "你好。世界！"}, clips,
                                self.root / "report", "ffmpeg",
                                transcribe=self.transcriber(words, "你好。世界！"))
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual([c["text"] for c in result["cues"]], ["你好。", "世界！"])
        self.assertEqual([c["start_us"] for c in result["cues"]], [100_000, 1_100_000])
        self.assertEqual([c["end_us"] for c in result["cues"]], [450_000, 1_550_000])

    def test_native_source_audio_cannot_satisfy_narration_subtitles(self):
        clips = [{"text": "你好", "parent_index": 0, "audio": str(self.audio),
                  "audio_mode": "native", "start_us": 0, "duration": 2}]
        result = build_captions({"script": "你好"}, clips, self.root / "report", "ffmpeg",
                                transcribe=self.transcriber([{"text": "你好", "start": 0.1, "end": 0.6}], "你好"))
        self.assertFalse(result["ok"])
        self.assertIn("forbidden audio mode", result["issues"][0])

    def test_invalid_timestamps_and_empty_asr_fail_closed(self):
        clips = [{"text": "你好", "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        for words in ([], [{"text": "你好", "start": 1.9, "end": 2.2}],
                      [{"text": "你", "start": 0.5, "end": 0.9},
                       {"text": "好", "start": 0.2, "end": 0.4}],
                      [{"text": "你", "start": 0.2, "end": 0.8},
                       {"text": "好", "start": 0.7, "end": 0.9}]):
            with self.subTest(words=words):
                mock = self.transcriber(words, "你好")
                result = build_captions({"script": "你好"}, clips, self.root / "report", "ffmpeg", transcribe=mock)
                self.assertFalse(result["ok"])

    def test_unsplittable_long_word_fails_closed(self):
        text = "超" * 35
        clips = [{"text": text, "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        result = build_captions({"script": text}, clips, self.root / "report", "ffmpeg",
                                transcribe=self.transcriber([{"text": text, "start": 0.1, "end": 1.5}], text))
        self.assertFalse(result["ok"])
        self.assertIn("over 30", result["issues"][0])

    def test_cache_identity_changes_with_audio_and_model(self):
        clips = [{"text": "你好", "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        calls = []
        base = self.transcriber([{"text": "你好", "start": 0.1, "end": 0.6}], "你好")
        def mock(path, *, model_name, language=None, hotwords=None):
            calls.append(model_name)
            return base(path, model_name=model_name, language=language, hotwords=hotwords)
        first = build_captions({"script": "你好", "asr_model": "small"}, clips, self.root / "report", "ffmpeg", transcribe=mock)
        same = build_captions({"script": "你好", "asr_model": "small"}, clips, self.root / "report", "ffmpeg", transcribe=mock)
        changed_model = build_captions({"script": "你好", "asr_model": "medium"}, clips, self.root / "report", "ffmpeg", transcribe=mock)
        self.assertTrue(first["ok"] and same["ok"] and changed_model["ok"])
        self.assertTrue(same["identity"]["audio_sha256"] == first["identity"]["audio_sha256"])
        cache = __import__("json").loads((self.root / "report" / "audio_captions.json").read_text(encoding="utf-8"))
        self.assertFalse(cache["cache_hit"], "different model request must invalidate the previous cache")
        self.assertEqual(calls, ["small", "medium"], "identical final audio/model/settings may reuse cached cues")
        alternate = self.root / "voice-normalized.wav"
        make_wav(alternate, frames=32000, value=3000)
        changed_audio = build_captions({"script": "你好", "asr_model": "medium"}, clips,
                                       self.root / "report", "ffmpeg",
                                       audio_overrides={str(self.audio): str(alternate)}, transcribe=mock)
        self.assertTrue(changed_audio["ok"])
        self.assertEqual(calls, ["small", "medium", "medium"], "changed final audio content must invalidate cache")

    def test_local_model_weight_change_invalidates_cache(self):
        clips = [{"text": "你好", "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        model = self.root / "local-model"
        model.mkdir()
        weights = model / "weights.bin"
        weights.write_bytes(b"first")
        calls = []
        base = self.transcriber([{"text": "你好", "start": 0.1, "end": 0.6}], "你好")
        def mock(path, *, model_name, language=None, hotwords=None):
            calls.append(model_name)
            return base(path, model_name=model_name, language=language, hotwords=hotwords)
        one = build_captions({"script": "你好", "asr_model": str(model)}, clips,
                             self.root / "report", "ffmpeg", transcribe=mock)
        two = build_captions({"script": "你好", "asr_model": str(model)}, clips,
                             self.root / "report", "ffmpeg", transcribe=mock)
        weights.write_bytes(b"second")
        three = build_captions({"script": "你好", "asr_model": str(model)}, clips,
                               self.root / "report", "ffmpeg", transcribe=mock)
        self.assertTrue(one["ok"] and two["ok"] and three["ok"])
        self.assertEqual(calls, [str(model), str(model)], "local model weight changes must invalidate cache")

    def test_cached_hub_snapshot_revision_invalidates_alias_cache(self):
        clips = [{"text": "你好", "parent_index": 0, "tts_path": str(self.audio),
                  "audio_mode": "tts", "start_us": 0, "duration": 2}]
        snapshots = self.root / "models--fake--small" / "snapshots"
        rev1, rev2 = snapshots / "revision-one", snapshots / "revision-two"
        for rev, contents in ((rev1, b"first"), (rev2, b"other")):
            rev.mkdir(parents=True)
            (rev / "model.bin").write_bytes(contents)
            (rev / "config.json").write_text("{}", encoding="utf-8")
        active = {"path": rev1}
        calls = []
        base = self.transcriber([{"text": "你好", "start": 0.1, "end": 0.6}], "你好")
        def mock(path, *, model_name, language=None, hotwords=None):
            calls.append(model_name)
            return base(path, model_name=model_name, language=language, hotwords=hotwords)
        with patch.object(audio_captions, "_resolve_cached_model", side_effect=lambda _name: active["path"]):
            first = build_captions({"script": "你好", "asr_model": "small"}, clips,
                                   self.root / "report", "ffmpeg", transcribe=mock)
            first_fingerprint = first["identity"]["model_fingerprint"]
            active["path"] = rev2
            second = build_captions({"script": "你好", "asr_model": "small"}, clips,
                                    self.root / "report", "ffmpeg", transcribe=mock)
        self.assertTrue(first["ok"] and second["ok"])
        self.assertNotEqual(first_fingerprint, second["identity"]["model_fingerprint"])
        self.assertEqual(calls, ["small", "small"], "a new cached snapshot revision must force recognition")

    def test_cuda_failure_during_lazy_word_iteration_retries_cpu_and_records_fallback(self):
        calls = []

        class FakeModel:
            def __init__(self, device):
                self.device = device

            def transcribe(self, *_args, **_kwargs):
                if self.device == "cuda":
                    def broken_segments():
                        yield SimpleNamespace(text="你好", words=[])
                        raise RuntimeError("cuDNN CUDA kernel failure during decode")
                    return broken_segments(), SimpleNamespace(language="zh")
                word = SimpleNamespace(word="你好", start=0.1, end=0.6)
                return [SimpleNamespace(text="你好", words=[word])], SimpleNamespace(language="zh")

        def factory(model_name, device, compute_type):
            calls.append((device, compute_type))
            return FakeModel(device)

        cuda_module = SimpleNamespace(get_cuda_device_count=lambda: 1)
        audio_captions._load_whisper_model.cache_clear()
        try:
            with patch.dict(sys.modules, {"ctranslate2": cuda_module}), \
                 patch.object(audio_captions, "_create_whisper_model", side_effect=factory):
                result = audio_captions.transcribe_faster_whisper(
                    self.audio, model_name="small", hotwords=["心相印"])
        finally:
            audio_captions._load_whisper_model.cache_clear()
        self.assertEqual(calls, [("cuda", "float16"), ("cpu", "int8")])
        self.assertEqual(result["device"], "cpu")
        self.assertEqual(result["compute_type"], "int8")
        self.assertIn("cuDNN CUDA kernel failure", result["fallback"])
        self.assertEqual(result["words"], [{"text": "你好", "start": 0.1, "end": 0.6}])


if __name__ == "__main__":
    unittest.main()
