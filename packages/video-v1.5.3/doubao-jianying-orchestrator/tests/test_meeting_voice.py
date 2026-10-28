import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import cli, engine, rulepack, voice_request, voice_tts
from orchestrator.voice_catalog import VoiceResolutionError


class MeetingVoiceRequestTests(unittest.TestCase):
    def test_combined_name_is_split_before_catalog_match(self):
        req = voice_request.resolve_voice_request("甜美年轻女声1.3倍速")
        self.assertEqual(req.original_voice, "甜美年轻女声1.3倍速")
        self.assertEqual(req.preset.sami, "zh_female_xiaopengyou")
        self.assertEqual(req.tempo, 1.3)
        self.assertEqual(req.tempo_source, "voice_name")

    def test_live_table_bare_decimal_suffix_uses_shared_parser(self):
        req = voice_request.resolve_voice_request("甜美年轻女声1.2")
        self.assertEqual(req.original_voice, "甜美年轻女声1.2")
        self.assertEqual(req.preset.sami, "zh_female_xiaopengyou")
        self.assertEqual(req.tempo, 1.2)
        self.assertEqual(rulepack.voice_name_speed("甜美年轻女声1.2"), 1.2)
        out = rulepack.apply_manifest_defaults({"voice":"甜美年轻女声1.2"})
        preset, blocker = cli._lock_voice_once(out)
        self.assertIsNone(blocker)
        self.assertEqual(preset.sami, req.preset.sami)
        self.assertEqual(out["voice_tempo"], 1.2)
        self.assertEqual(voice_request.resolve_voice_request(
            "甜美年轻女声1.2", voice_tempo=1.0).tempo, 1.0)

    def test_integer_voice_name_is_not_treated_as_bare_tempo(self):
        from orchestrator.voice_catalog import _split_speed
        self.assertEqual(_split_speed("熊二2"), ("熊二2", None))
        self.assertEqual(_split_speed("female_sweet2倍速"), ("female_sweet", 2.0))
        with self.assertRaises(ValueError):
            voice_request.resolve_voice_request("female_sweet9.0")

    def test_table_tempo_suffix_variants_use_same_locked_voice(self):
        for suffix in ("1.6倍", "1.6倍数", "1.6倍速", "1.6"):
            with self.subTest(suffix=suffix):
                req = voice_request.resolve_voice_request("female_sweet" + suffix)
                self.assertEqual(req.preset.sami, "zh_female_xiaopengyou")
                self.assertEqual(req.tempo, 1.6)
                self.assertEqual(rulepack.voice_name_speed("female_sweet" + suffix), 1.6)
        self.assertEqual(voice_request.resolve_voice_request(
            "female_sweet1.6倍", voice_tempo=1.0).tempo, 1.0)

    def test_explicit_tempo_overrides_suffix_without_mutating_voice(self):
        spec = "甜美年轻女声1.3倍速"
        req = voice_request.resolve_voice_request(spec, voice_tempo="1.4", category_tempo=1.1)
        self.assertEqual(req.original_voice, spec)
        self.assertEqual(req.preset.sami, "zh_female_xiaopengyou")
        self.assertEqual(req.tempo, 1.4)
        self.assertEqual(req.tempo_source, "voice_tempo")

    def test_explicit_one_times_name_and_dict_speed(self):
        self.assertEqual(voice_request.resolve_voice_request("甜美年轻女声1.0倍速",
                                                               category_tempo=1.3).tempo, 1.0)
        req = voice_request.resolve_voice_request({"key": "female_sweet", "speed": 1.0},
                                                  category_tempo=1.3)
        self.assertEqual(req.tempo, 1.0)
        self.assertEqual(req.tempo_source, "voice_name")

    def test_category_default_and_rulepack_precedence(self):
        self.assertEqual(voice_request.resolve_voice_request("female_sweet", category_tempo=1.2).tempo, 1.2)
        out = rulepack.apply_manifest_defaults({"product": "卫生巾", "voice": "甜美年轻女声1.3倍速",
                                                "voice_tempo": 1.1})
        self.assertEqual(out["voice"], "甜美年轻女声1.3倍速")
        self.assertEqual(out["voice_tempo"], 1.1)
        explicit_one = rulepack.apply_manifest_defaults({"product": "卫生巾", "voice": "female_sweet",
                                                         "voice_tempo": 1.0})
        self.assertEqual(explicit_one["voice_tempo"], 1.0)

    def test_invalid_tempo_is_not_defaulted(self):
        for value in ("fast", "nan", 0, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                voice_request.resolve_voice_request("female_sweet", voice_tempo=value)

    def test_null_optional_field_keeps_suffix_for_pre_synthesis(self):
        out = rulepack.apply_manifest_defaults({"voice": "female_sweet1.3倍速", "voice_tempo": None})
        self.assertEqual(out["voice_tempo"], 1.3)
        with self.assertRaises(ValueError):
            voice_request.resolve_voice_request({"key": "female_sweet", "speed": "oops"})

    def test_unknown_and_ambiguous_voices_remain_errors(self):
        with self.assertRaises(VoiceResolutionError):
            voice_request.resolve_voice_request("this voice does not exist1.2倍速")
        duplicate_rows = [SimpleNamespace(to_dict=lambda: {"speaker_id": sid})
                          for sid in ("speaker_a", "speaker_b")]
        with patch("orchestrator.voice_library.find_exact_name", return_value=duplicate_rows):
            with self.assertRaisesRegex(VoiceResolutionError, "对应多个 speaker_id"):
                voice_request.resolve_voice_request("重复显示名1.2倍速")

    def test_tempo_validation_keeps_range_and_rejects_bad_values(self):
        self.assertEqual(voice_tts.normalize_tempo(None), 1.0)
        for invalid in (9, 0.1, True):
            with self.assertRaises(ValueError):
                voice_tts.normalize_tempo(invalid)
        with self.assertRaises(ValueError):
            voice_tts.normalize_tempo("not-a-number")

    def test_processed_path_requires_original_cache_for_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            derived = Path(tmp) / "tts_hash.p1_k25_t130.ogg"
            derived.write_bytes(b"derived")
            with self.assertRaisesRegex(RuntimeError, "原始配音缓存不存在"):
                voice_tts.postprocess_voice(derived, tempo=1.4)

    def test_batch_postprocess_failure_propagates(self):
        with patch.object(voice_tts, "postprocess_voice", side_effect=RuntimeError("ffmpeg failed")):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg failed"):
                voice_tts._postprocess_all({"a": "raw.ogg"}, tempo=1.2, edge_trim=True)

    def test_nested_derived_paths_restart_from_original_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "tts_hash.ogg"
            derived = raw.with_name("tts_hash.p1_k25_t130.ogg")
            nested = raw.with_name("tts_hash.p1_k25_t130.p1_k25_t140.ogg")
            for path in (raw, derived, nested):
                path.write_bytes(b"audio fixture")
            with patch.object(voice_tts, "_audio_cache_is_usable", return_value=True):
                result = voice_tts.postprocess_voice(nested, tempo=1.0, edge_trim=False)
            self.assertEqual(result, str(raw))

    def test_generate_batch_uses_name_tempo_after_resolving_voice(self):
        captured = {}

        def close_and_return(coro):
            coro.close()
            return {"line": "raw.ogg"}

        def postprocess(paths, *, tempo, edge_trim):
            captured.update(tempo=tempo, edge_trim=edge_trim)
            return paths

        with patch.object(voice_tts, "_quiet_async_run", side_effect=close_and_return), \
             patch.object(voice_tts, "_postprocess_all", side_effect=postprocess):
            preset, paths = voice_tts.generate_batch(
                [{"key": "line", "text": "demo"}], Path("unused"),
                "甜美年轻女声1.3倍速", edge_trim=False)
        self.assertEqual(preset.sami, "zh_female_xiaopengyou")
        self.assertEqual(captured["tempo"], 1.3)
        self.assertEqual(paths, {"line": "raw.ogg"})

    def test_explicit_manifest_voice_wins_over_inherited_environment(self):
        env_voice = "甜美年轻女声1.6倍速"
        manifest_voice = "阳光活力男声1.2倍速"
        with patch.dict("os.environ", {"JY_OPERATOR_VOICE": env_voice}):
            chosen = cli._apply_operator_voice_input({"voice": manifest_voice})
            normalized = rulepack.apply_manifest_defaults(chosen)
            preset, blocker = cli._lock_voice_once(normalized)
            self.assertIsNone(blocker)
            self.assertEqual(normalized["voice"], manifest_voice)
            self.assertEqual(normalized["voice_tempo"], 1.2)
            self.assertEqual(preset.sami, "zh_male_huoli")

            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(cli.timing_contract, "split_sentences", return_value=[{"text": "spoken"}]), \
                 patch.object(cli.timing_contract, "sentence_key", return_value="k0"), \
                 patch.object(cli.timing_contract, "sentence_text", side_effect=lambda row: row["text"]), \
                 patch.object(cli.timing_contract, "narration_timing", return_value={}), \
                 patch.object(cli, "_audio_probe", return_value=None), \
                 patch.object(engine, "find_jianying_editor_root", return_value=Path(tmp)), \
                 patch.object(engine, "choose_profile", return_value=SimpleNamespace(tts_concurrency=2)), \
                 patch.object(engine, "apply_process_priority"), \
                 patch.object(engine.OrchestrationEngine, "_load_probe_cache"), \
                 patch.object(engine, "find_ffmpeg", return_value=(None, None)), \
                 patch.object(engine, "bootstrap_path"):
                pre_calls = []
                with patch.object(voice_tts, "generate_batch",
                                  side_effect=lambda *a, **kw: (pre_calls.append(kw.copy()) or (preset, {"k0": "raw.ogg"}))):
                    cli._pre_synthesize_narration(normalized, Path(tmp), preset)
                self.assertEqual(pre_calls[0]["tempo"], 1.2)
                self.assertEqual(pre_calls[0]["voice_spec"].sami, preset.sami)

                eng = engine.OrchestrationEngine(normalized, Path(tmp), Path(tmp))
                self.assertEqual(eng.decide_voice()[0].sami, preset.sami)
                eng.profile = SimpleNamespace(tts_concurrency=2)
                calls = []
                with patch.object(voice_tts, "generate_batch",
                                  side_effect=lambda *a, **kw: (calls.append(kw.copy()) or (preset, {"k0": "out.ogg"}))):
                    eng.synthesize([engine.Clip("v.mp4", 0, 1, text="spoken")], preset)
                self.assertEqual(calls[0]["tempo"], 1.2)
                self.assertEqual(calls[0]["voice_spec"].sami, preset.sami)
                self.assertEqual(calls[0]["voice_spec"].sami, pre_calls[0]["voice_spec"].sami)

    def test_environment_fallback_suffix_is_resolved_before_defaults(self):
        env_voice = "甜美年轻女声1.6倍速"
        with patch.dict("os.environ", {"JY_OPERATOR_VOICE": env_voice}):
            normalized = rulepack.apply_manifest_defaults(
                cli._apply_operator_voice_input({"product": "卫生巾"}))
            self.assertEqual(normalized["voice"], env_voice)
            self.assertEqual(normalized["voice_tempo"], 1.6)
            preset, blocker = cli._lock_voice_once(normalized)
            self.assertIsNone(blocker)
            self.assertEqual(preset.sami, "zh_female_xiaopengyou")

            with tempfile.TemporaryDirectory() as tmp, \
                 patch.object(engine, "find_jianying_editor_root", return_value=Path(tmp)), \
                 patch.object(engine, "choose_profile", return_value=SimpleNamespace(tts_concurrency=2)), \
                 patch.object(engine, "apply_process_priority"), \
                 patch.object(engine.OrchestrationEngine, "_load_probe_cache"), \
                 patch.object(engine, "find_ffmpeg", return_value=(None, None)), \
                 patch.object(engine, "bootstrap_path"):
                eng = engine.OrchestrationEngine({"product": "卫生巾"}, Path(tmp), Path(tmp))
                self.assertEqual(eng.m["voice"], env_voice)
                self.assertEqual(eng.m["voice_tempo"], 1.6)
                self.assertEqual(eng.decide_voice()[0].sami, preset.sami)


if __name__ == "__main__":
    unittest.main()
