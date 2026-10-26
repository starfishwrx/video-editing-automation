"""Script subtitles use measured final narration boundaries, with no recognizer."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.audio_captions import build_captions


class AudioCaptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / 'voice.wav'
        self.wav(self.audio, 2)

    def wav(self, path, seconds):
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b'\x00\x01' * round(16000 * seconds))

    def clip(self, **changes):
        return {'text': 'Original brand 140', 'audio_mode': 'tts',
                'tts_path': str(self.audio), 'parent_index': 0,
                'start_us': 0, 'duration': 2, **changes}

    def build(self, clips, **kwargs):
        return build_captions({}, clips, self.root / 'report', **kwargs)

    def test_script_text_uses_final_tts_duration(self):
        result = self.build([self.clip()])
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['cues'], [{'text': 'Original brand 140',
                                         'start_us': 0, 'end_us': 2_000_000}])
        self.assertEqual(result['identity']['source'], 'tts-sentence-timing-v1')

    def test_one_sentence_spanning_two_shots_has_one_continuous_cue(self):
        result = self.build([self.clip(duration=1, multi_shot=True, audio_clip_start_us=0),
                             self.clip(start_us=1_000_000, duration=1,
                                       multi_shot=True, audio_clip_start_us=0)])
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['cues']), 1)
        self.assertEqual(result['cues'][0]['end_us'], 2_000_000)

    def test_two_distinct_sentences_do_not_merge_even_when_text_is_repeated(self):
        result = self.build([self.clip(), self.clip(parent_index=1, start_us=2_000_000)])
        self.assertTrue(result['ok'], result)
        self.assertEqual([cue['start_us'] for cue in result['cues']], [0, 2_000_000])

    def test_postprocessed_audio_controls_timing_instead_of_original_duration(self):
        processed = self.root / 'voice-tempo.wav'
        self.wav(processed, 1)
        result = self.build([self.clip(duration=1)], audio_overrides={str(self.audio): str(processed)})
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['cues'][0]['end_us'], 1_000_000)
        self.assertEqual(result['identity']['narration'][0]['path'], str(processed.resolve()))

    def test_missing_or_overlong_audio_cannot_generate_valid_captions(self):
        for clip in (self.clip(tts_path=str(self.root / 'missing.wav')), self.clip(duration=1),
                     self.clip(audio_mode='mute')):
            with self.subTest(clip=clip):
                result = self.build([clip])
                self.assertFalse(result['ok'])
                self.assertEqual(result['cues'], [])

    def test_changed_text_and_final_audio_are_used_on_every_run(self):
        self.assertTrue(self.build([self.clip()])['ok'])
        self.wav(self.audio, 1)
        result = self.build([self.clip(text='Updated brand 150', duration=1)])
        self.assertEqual(result['cues'], [{'text': 'Updated brand 150', 'start_us': 0, 'end_us': 1_000_000}])
        report = json.loads((self.root / 'report/audio_captions.json').read_text())
        self.assertEqual(report['status'], 'timed_from_tts')

    def test_noncontiguous_multishot_narration_is_not_reported_as_one_sentence(self):
        result = self.build([self.clip(duration=1, multi_shot=True, audio_clip_start_us=0),
                             self.clip(start_us=1_200_000, duration=1,
                                       multi_shot=True, audio_clip_start_us=0)])
        self.assertFalse(result['ok'])


if __name__ == '__main__':
    unittest.main()
