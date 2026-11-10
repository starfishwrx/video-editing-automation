import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.application import recovery_audio
from orchestrator.audio import narration_preparation as preparation


class RecoveryPreparationTests(unittest.TestCase):
    def test_legacy_audio_updates_timing_before_preflight_without_changing_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "old.ogg"
            source.write_bytes(b"unchanged")
            manifest = {"category": "zhipin", "segments": [{"text": "text"}]}
            timing = {"rows": [{"tts_path": str(source), "audio_duration_us": 1000000}]}
            original = copy.deepcopy(timing)
            record = {"path": str(root / "final.wav"), "duration_us": 800000}
            with patch("orchestrator.rulepack.for_manifest", return_value={
                     "category": "zhipin", "voice": {"acceptance_max_gap_frames": 5}}), \
                 patch("orchestrator.platform_env.find_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
                 patch.object(preparation, "frozen", return_value=None), \
                 patch.object(preparation, "prepare", return_value=record) as prepare, \
                 patch.object(preparation, "preflight") as gate:
                result = recovery_audio.prepare_for_selection(manifest, timing, root)
            self.assertEqual(result["rows"][0]["audio_duration_us"], 800000)
            self.assertEqual(gate.call_args.kwargs["max_frames"], 5)
            self.assertEqual(gate.call_args.args[0], result)
            self.assertTrue(prepare.call_args.kwargs["edge_trim"])
            self.assertEqual(timing, original)
            self.assertEqual(source.read_bytes(), b"unchanged")
            self.assertEqual(json.loads(Path(manifest["narration_timing_file"]).read_text()), result)

    def test_failed_preflight_does_not_publish_new_timing(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("orchestrator.rulepack.for_manifest", return_value={
                 "category": "zhipin", "voice": {"acceptance_max_gap_frames": 5}}), \
             patch("orchestrator.platform_env.find_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
             patch.object(preparation, "frozen", return_value={"path": "final.wav", "duration_us": 9}), \
             patch.object(preparation, "prepare") as prepare, \
             patch.object(preparation, "preflight", side_effect=preparation.NarrationError("bad")):
            manifest = {"segments": [{"text": "text"}]}
            with self.assertRaises(preparation.NarrationError):
                recovery_audio.prepare_for_selection(manifest, {"rows": [{"tts_path": "final.wav"}]},
                                                    Path(temporary))
            prepare.assert_not_called()
            self.assertNotIn("narration_timing_file", manifest)
            self.assertFalse((Path(temporary) / "recovery_narration_timing.json").exists())

    def test_operator_file_audio_is_only_checked(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("orchestrator.rulepack.for_manifest", return_value={
                 "category": "zhipin", "voice": {"acceptance_max_gap_frames": 5}}), \
             patch("orchestrator.platform_env.find_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
             patch.object(preparation, "prepare") as prepare, \
             patch.object(preparation, "preflight") as gate, \
             patch("orchestrator.audio.narration_probe.probe_audio", return_value={"duration": 0.002}):
            manifest = {"segments": [{"text": "text", "audio": "operator.wav", "audio_mode": "file"}]}
            timing = {"rows": [{"tts_path": "operator.wav", "audio_duration_us": 1234}]}
            result = recovery_audio.prepare_for_selection(manifest, timing, Path(temporary))
            self.assertEqual(result["rows"][0]["tts_path"], str(Path("operator.wav").resolve()))
            self.assertEqual(result["rows"][0]["audio_duration_us"], 2000)
            prepare.assert_not_called()
            gate.assert_called_once()

    def test_changed_operator_audio_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch("orchestrator.rulepack.for_manifest", return_value={
                 "category": "zhipin", "voice": {"acceptance_max_gap_frames": 5}}), \
             patch("orchestrator.platform_env.find_ffmpeg", return_value=("ffmpeg", "ffprobe")):
            manifest = {"segments": [{"text": "same", "audio": "B.wav", "audio_mode": "file"}]}
            with self.assertRaisesRegex(ValueError, "RECOVERY_SENTENCE_AUDIO_CHANGED"):
                recovery_audio.prepare_for_selection(manifest, {"rows": [{"tts_path": "A.wav"}]},
                                                    Path(temporary))
