"""Real media at non-millisecond lengths, rotation and source identity boundaries."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.audio import audio_loudness
from orchestrator.application import headless_production, recovery_audio
from orchestrator.media import source_normalization as normalization
from orchestrator.media.probe import probe_media
from orchestrator.quality.ai_editor_qc import source_matches_receipt

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")


@unittest.skipUnless(FFMPEG and FFPROBE, "FFmpeg and FFprobe required")
class HeadlessMediaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def command(self, *args):
        return subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *map(str, args)],
                              capture_output=True, text=True, check=True, timeout=60)

    def voice(self):
        path = self.root / "voice.wav"
        self.command("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                     "-af", "atrim=end_sample=85246", "-c:a", "pcm_s16le", path)
        return path

    def rotated_video(self):
        plain, rotated = self.root / "plain.mp4", self.root / "rotated.mp4"
        self.command("-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=1",
                     "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=48000:duration=1",
                     "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", plain)
        self.command("-display_rotation", "90", "-i", plain, "-c", "copy", rotated)
        return rotated

    def frame_hashes(self, path):
        output = self.command("-i", path, "-map", "0:v:0", "-an", "-f", "framemd5", "-").stdout
        return [line.split(",")[-1].strip() for line in output.splitlines() if not line.startswith("#")]

    def test_real_48k_sample_count_settings_cache_and_old_format_rejection(self):
        source = self.voice()
        before = normalization.identity(source)
        output = audio_loudness.normalized_path(source, self.root / "processed")
        output.parent.mkdir()
        # A cache path containing a previous 44.1kHz artifact must not survive.
        self.command("-i", source, "-ar", "44100", output)
        first = audio_loudness.normalize_narration(source, output, ffmpeg=FFMPEG, ffprobe=FFPROBE)
        facts = probe_media(output, ffprobe=FFPROBE)
        with wave.open(str(output), "rb") as stream:
            self.assertEqual((stream.getframerate(), stream.getnframes(), stream.getnchannels()), (48000, 85246, 2))
        self.assertEqual(facts["duration_us"], 1_775_958)
        self.assertFalse(first.get("reused", False))
        stamp = output.stat().st_mtime_ns
        second = audio_loudness.normalize_narration(source, output, ffmpeg=FFMPEG, ffprobe=FFPROBE)
        self.assertTrue(second["reused"])
        self.assertEqual(output.stat().st_mtime_ns, stamp)
        damaged = output.read_bytes()
        output.write_bytes(damaged[:len(damaged) // 2])  # Intact WAV header, incomplete PCM payload.
        repaired = audio_loudness.normalize_narration(source, output, ffmpeg=FFMPEG, ffprobe=FFPROBE)
        self.assertFalse(repaired.get("reused", False))
        with wave.open(str(output), "rb") as stream:
            self.assertEqual(len(stream.readframes(85246)), 85246 * 2 * 2)
        self.assertEqual(normalization.identity(source), before)
        self.assertNotEqual(output, audio_loudness.normalized_path(source, output.parent, target_lufs=-18))
        self.assertNotEqual(output, audio_loudness.normalized_path(source, output.parent, tp_limit_db=-2))

    def test_sample_domain_check_accepts_same_sample_but_rejects_missing_speech(self):
        facts = {"duration_us": 1_775_958, "streams": [{"codec_type": "audio", "codec_name": "pcm_s16le", "sample_rate": "48000"}]}
        self.assertTrue(headless_production._same_audio_samples(1_775_960, facts))
        self.assertFalse(headless_production._same_audio_samples(1_755_958, facts))
        self.assertFalse(headless_production._same_audio_samples(1_775_980, facts))
        self.assertFalse(headless_production._same_audio_samples(1_775_960, {"duration_us": 1_775_958}))

    def test_rotation_copy_preserves_pixels_clock_audio_and_reuses_cache(self):
        source = self.rotated_video()
        original = normalization.identity(source)
        with patch.dict(os.environ, {"JY_GPU_DISABLE": "1"}):
            record = normalization.normalize_video(source, self.root / "cache", ffmpeg=FFMPEG, ffprobe=FFPROBE)
        prepared = Path(record["prepared"]["path"])
        self.assertTrue(record["changed"])
        self.assertEqual(normalization.rotation(probe_media(prepared, ffprobe=FFPROBE)), 0)
        self.assertEqual((record["display_width"], record["display_height"]), (64, 96))
        self.assertEqual(record["frame_clock"]["count"], 10)
        self.assertTrue(probe_media(prepared, ffprobe=FFPROBE)["audio"])
        self.assertEqual(self.frame_hashes(source), self.frame_hashes(prepared))
        def audio_hash(path):
            return self.command("-i", path, "-map", "0:a:0", "-c:a", "pcm_s16le", "-f", "md5", "-").stdout
        self.assertEqual(audio_hash(source), audio_hash(prepared))
        self.assertEqual(normalization.identity(source), original)
        with patch.object(normalization, "run_h264_encode", side_effect=AssertionError("cache repeated encoding")):
            cached = normalization.normalize_video(source, self.root / "cache", ffmpeg=FFMPEG, ffprobe=FFPROBE)
        self.assertTrue(cached["reused"])
        staged = self.root / "draft-owned.mp4"
        shutil.copyfile(prepared, staged)
        self.assertTrue(source_matches_receipt(str(staged), original, [record]))
        self.assertFalse(source_matches_receipt(str(staged), original))
        changed_mapping = copy.deepcopy(record)
        changed_mapping["time_mapping"]["offset_us"] = 10
        self.assertFalse(source_matches_receipt(str(staged), original, [changed_mapping]))
        staged.write_bytes(staged.read_bytes() + b"changed")
        self.assertFalse(source_matches_receipt(str(staged), original, [record]))
        with source.open("ab") as stream:
            stream.write(b"source replaced")
        self.assertFalse(source_matches_receipt(str(prepared), original, [record]))

    def test_retained_tts_keeps_normalization_provenance_while_operator_file_does_not(self):
        voice = self.voice()
        operator = self.root / "operator.wav"
        shutil.copyfile(voice, operator)
        segments = [{"text": "第一句"}, {"text": "第二句", "audio_mode": "file", "audio": str(operator)}]
        from orchestrator.selection import timing_contract
        rows = [{"key": timing_contract.sentence_key(s, i), "text": s["text"], "tts_path": s.get("audio", str(voice)),
                 "audio_duration_us": 1_775_958, "timing_certainty": "audio"} for i, s in enumerate(segments, 1)]
        timing = self.root / "timing.json"
        timing.write_text(json.dumps({"rows": rows}), encoding="utf-8")
        bound = recovery_audio.bind({"segments": segments, "narration_timing_file": str(timing)})
        self.assertEqual(bound["retained_tts_audio"], [str(voice)])
        self.assertEqual([s["audio_mode"] for s in bound["segments"]], ["file", "file"])
        # Pre-upgrade draft_write persisted file mode without any TTS marker.
        legacy = copy.deepcopy(bound)
        legacy.pop("retained_tts_audio")
        matched = {"ok": True, "input_manifest": {"segments": segments},
                   "manifest": legacy, "segments": legacy["segments"]}
        upgraded = recovery_audio.bind_and_record(legacy, matched, self.root)
        self.assertEqual(upgraded["retained_tts_audio"], [str(voice)])
        saved = json.loads((self.root / "shot_match_report.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["manifest"]["retained_tts_audio"], [str(voice)])

    def test_rotation_preserves_10bit_pixels_and_color_metadata(self):
        plain, source = self.root / "ten-bit.mp4", self.root / "ten-bit-rotated.mp4"
        self.command("-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=1,format=yuv420p10le",
                     "-c:v", "libx264", "-crf", "0", "-pix_fmt", "yuv420p10le",
                     "-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc", plain)
        self.command("-display_rotation", "-90", "-i", plain, "-c", "copy", source)
        with patch.dict(os.environ, {"JY_GPU_DISABLE": "1"}):
            record = normalization.normalize_video(source, self.root / "cache", ffmpeg=FFMPEG, ffprobe=FFPROBE)
        prepared = Path(record["prepared"]["path"])
        self.assertEqual(record["pixel_format"], "yuv420p10le")
        self.assertEqual(record["color_metadata"]["color_space"], "bt2020nc")
        self.assertEqual(self.frame_hashes(source), self.frame_hashes(prepared))

    def test_rotated_vfr_retains_demux_time_base_and_all_frame_timestamps(self):
        plain, source = self.root / "vfr.mp4", self.root / "vfr-rotated.mp4"
        self.command("-f", "lavfi", "-i", "testsrc2=size=96x64:rate=30:duration=1",
                     "-vf", r"settb=1/600,setpts=N*20+mod(N\,2)", "-fps_mode", "passthrough",
                     "-enc_time_base:v", "1/600", "-video_track_timescale", "600",
                     "-c:v", "libx264", plain)
        self.command("-display_rotation", "-90", "-i", plain, "-c", "copy", source)
        clock = normalization._clock(source, FFPROBE)
        self.assertGreater(len({b - a for a, b in zip(clock, clock[1:])}), 1)
        with patch.dict(os.environ, {"JY_GPU_DISABLE": "1"}):
            record = normalization.normalize_video(source, self.root / "cache", ffmpeg=FFMPEG, ffprobe=FFPROBE)
        prepared = Path(record["prepared"]["path"])
        self.assertEqual(clock, normalization._clock(prepared, FFPROBE))
        self.assertEqual(self.frame_hashes(source), self.frame_hashes(prepared))


if __name__ == "__main__":
    unittest.main()
