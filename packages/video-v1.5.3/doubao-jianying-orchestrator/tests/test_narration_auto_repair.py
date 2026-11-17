"""Short narration recovery preserves voice and sentence-binding boundaries."""
import math
import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.application import cli
from orchestrator.audio import narration_preparation, verified_narration_cache, voice_tts
from orchestrator.audio.voice_request import resolve_voice_request
from orchestrator.runtime.platform_env import find_ffmpeg


def make_audio(path, head, speech, tail):
    samples = np.arange(round(speech * 48000)) / 48000
    tone = np.round(6000 * np.sin(2 * math.pi * 440 * samples)).astype("<i2")
    data = np.concatenate([np.zeros(round(head * 48000), dtype="<i2"), tone,
                           np.zeros(round(tail * 48000), dtype="<i2")])
    with wave.open(str(path), "wb") as output:
        output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        output.writeframes(np.column_stack([data, data]).astype("<i2").tobytes())


class NarrationAutoRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"JY_TTS_VERIFIED_CACHE": str(self.root / "cache")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.ffmpeg, self.ffprobe = map(str, find_ffmpeg())
        self.unsafe = self.root / "unsafe.wav"
        self.valid = self.root / "valid.wav"
        make_audio(self.unsafe, .27, .45, .29)
        make_audio(self.valid, .025, .9, .025)
        self.voice = resolve_voice_request("真人播客女", voice_tempo=1.0).preset
        self.manifest = {"category": "zhipin", "voice_tempo": 1.0,
                         "full_script": "短句文字，后续示例，",
                         "sentence_plan_version": "semantic-v1",
                         "segments": [{"text": "短句文字，"}, {"text": "后续示例，"}]}

    def fake_gather(self, calls):
        async def gather(items, directory, preset, concurrency, retries=3):
            calls.append(([(item["key"], item["text"]) for item in items], retries))
            return {item["key"]: str(self.unsafe if item["text"] == "短句文字，" else self.valid)
                    for item in items}
        return gather

    def test_repeated_source_is_processed_once_and_bound_to_both_sentences(self):
        calls = []

        def process(path, *, tempo, edge_trim):
            calls.append((Path(path), tempo, edge_trim))
            return str(path) + '.processed'

        with patch.object(voice_tts, 'postprocess_voice', side_effect=process):
            result = voice_tts._postprocess_all(
                {'s1': str(self.valid), 's2': self.valid, 's3': str(self.unsafe)},
                tempo=1.2, edge_trim=False)
        self.assertEqual(result['s1'], result['s2'])
        self.assertNotEqual(result['s1'], result['s3'])
        self.assertCountEqual(calls, [(self.valid, 1.2, False), (self.unsafe, 1.2, False)])

    def test_cached_merged_audio_precedes_retry_and_keeps_text(self):
        final = narration_preparation.prepare(self.valid, self.root / "seed", loudness={},
            ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        identity = verified_narration_cache.identity("短句文字，后续示例，", self.voice.sami, 1.0, {}, True)
        verified_narration_cache.remember(verified_narration_cache.cache_root(), identity, final["path"])
        calls = []
        with patch.object(voice_tts, "_gather", side_effect=self.fake_gather(calls)):
            timing = cli._pre_synthesize_narration(self.manifest, self.root, self.voice)
        self.assertEqual([row["text"] for row in timing["rows"]], ["短句文字，后续示例，"])
        self.assertFalse(any(retries == 1 for _, retries in calls))
        self.assertEqual(self.manifest["narration_replans"][0]["source"], ["短句文字，", "后续示例，"])

    def test_retry_then_merge_when_fresh_short_audio_still_unsafe(self):
        calls = []
        with patch.object(voice_tts, "_gather", side_effect=self.fake_gather(calls)):
            timing = cli._pre_synthesize_narration(self.manifest, self.root, self.voice)
        self.assertEqual([row["text"] for row in timing["rows"]], ["短句文字，后续示例，"])
        self.assertEqual(sum(retries == 1 for _, retries in calls), 1)
        self.assertEqual(self.manifest["segments"], [{"text": "短句文字，后续示例，"}])

    def test_shot_binding_prevents_merge(self):
        self.manifest["shot_notes_by_index"] = {"1": "特定画面"}
        original = list(self.manifest["segments"])
        calls = []
        with patch.object(voice_tts, "_gather", side_effect=self.fake_gather(calls)):
            with self.assertRaisesRegex(narration_preparation.NarrationError, "NARRATION_EDGE_REPAIR_UNSAFE"):
                cli._pre_synthesize_narration(self.manifest, self.root, self.voice)
        self.assertEqual(self.manifest["segments"], original)
        self.assertEqual(sum(retries == 1 for _, retries in calls), 1)
        with patch.object(voice_tts, "_gather", side_effect=self.fake_gather(calls)):
            with self.assertRaisesRegex(narration_preparation.NarrationError, "NARRATION_EDGE_REPAIR_UNSAFE"):
                cli._pre_synthesize_narration(self.manifest, self.root, self.voice)
        self.assertEqual(sum(retries == 1 for _, retries in calls), 1)

    def test_explicit_segment_metadata_and_sentence_end_do_not_replan(self):
        self.manifest["segments"][0]["shot_note"] = "固定片段"
        self.assertEqual(cli._short_sentence_merges(self.manifest), {})
        self.manifest["segments"][0] = {"text": "短句文字。"}
        self.manifest["full_script"] = "短句文字。后续示例，"
        self.assertEqual(cli._short_sentence_merges(self.manifest), {})

    def test_cache_rejects_different_voice_tempo_or_modified_asset(self):
        final = narration_preparation.prepare(self.valid, self.root / "seed", loudness={},
            ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
        data = verified_narration_cache.identity("短句文字，", self.voice.sami, 1.0, {}, True)
        root = verified_narration_cache.cache_root()
        verified_narration_cache.remember(root, data, final["path"])
        cached = verified_narration_cache.lookup(root, data)
        self.assertIsNotNone(cached)
        self.assertIsNone(verified_narration_cache.lookup(root,
            verified_narration_cache.identity("短句文字，", "other", 1.0, {}, True)))
        self.assertIsNone(verified_narration_cache.lookup(root,
            verified_narration_cache.identity("短句文字，", self.voice.sami, 1.2, {}, True)))
        Path(cached).write_bytes(b"corrupt")
        self.assertIsNone(verified_narration_cache.lookup(root, data))

    def test_history_import_binds_task_voice_text_timing_and_raw_synthesis(self):
        texts = ["第一句，", "第二句。"]
        segments, rows = [], []
        for index, text in enumerate(texts, 1):
            raw = self.root / voice_tts._cache_name(text, self.voice.sami)
            make_audio(raw, .025, .8 + index * .1, .025)
            Path(str(raw) + ".backend").write_text("sami", encoding="utf-8")
            final = narration_preparation.prepare(raw, self.root / "final", loudness={},
                ffmpeg=self.ffmpeg, ffprobe=self.ffprobe)
            segments.append({"text": text, "audio": final["path"]})
            rows.append({"key": f"s{index}", "index": index, "text": text,
                         "tts_path": final["path"], "audio_duration_us": final["duration_us"],
                         "timing_certainty": "audio"})
        task = {"record_id": "task", "script": "".join(texts), "voice": "真人播客女",
                "manifest": {"segments": segments}}
        planned = {"task_id": "task", "record_id": "task", "full_script": task["script"],
                   "voice": task["voice"], "voice_tempo": 1.0, "segments": [{"text": t} for t in texts],
                   "rulepack": {"voice_acceptance_max_gap_frames": 5}}
        timing = {"voice": task["voice"], "rows": rows}
        narration_preparation.preflight(timing, self.root, fps=30, max_frames=5, ffmpeg=self.ffmpeg)
        paths = [self.root / name for name in ("task.json", "planned.json", "timing.json")]
        for path, value in zip(paths, (task, planned, timing)):
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        preflight = self.root / "narration_preflight_report.json"
        root = self.root / "imported"
        result = verified_narration_cache.import_history(*paths, preflight, root)
        self.assertEqual(len(result["imported"]), 2)
        for text in texts:
            key = verified_narration_cache.identity(text, self.voice.sami, 1.0, {}, True)
            self.assertIsNotNone(verified_narration_cache.lookup(root, key))
        normal_task = dict(task)
        normal_task.pop("manifest")
        paths[0].write_text(json.dumps(normal_task, ensure_ascii=False), encoding="utf-8")
        result = verified_narration_cache.import_history(*paths, preflight, self.root / "normal-task")
        self.assertEqual(len(result["imported"]), 2)
        task["voice"] = "台湾腔甜妹"
        paths[0].write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "TASK_IDENTITY_MISMATCH"):
            verified_narration_cache.import_history(*paths, preflight, self.root / "rejected")
        planned["voice"] = timing["voice"] = task["voice"]
        paths[1].write_text(json.dumps(planned, ensure_ascii=False), encoding="utf-8")
        paths[2].write_text(json.dumps(timing, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "RAW_IDENTITY_MISSING"):
            verified_narration_cache.import_history(*paths, preflight, self.root / "rejected")
        self.assertFalse((self.root / "rejected").exists())


if __name__ == "__main__":
    unittest.main()
