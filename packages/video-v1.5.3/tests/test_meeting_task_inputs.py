import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "doubao-jianying-orchestrator"))
from orchestrator.task_inputs import (done_meeting_fields, manifest_meeting_fields,
                                      normalize_meeting_task)


class MeetingTaskInputTests(unittest.TestCase):
    def test_meeting_defaults_to_single_output_and_keeps_tempo_and_original_voice(self):
        task = normalize_meeting_task({"record_id": "r1", "count": 4,
            "voice": "voice-key", "voice_original": "真人播客女 1.3倍速", "voice_tempo": 1.3})
        self.assertEqual(task["count"], 1)
        fields = manifest_meeting_fields(task)["meeting_inputs"]
        self.assertEqual(fields["voice_original"], "真人播客女 1.3倍速")
        self.assertEqual(fields["voice_tempo"], 1.3)

    def test_legacy_count_is_explicit_and_reference_absence_stays_unknown(self):
        legacy = normalize_meeting_task({"meeting_profile": "legacy", "count": 3})
        self.assertEqual(legacy["count"], 3)
        alias = normalize_meeting_task({"profile": "legacy", "count": 2})
        self.assertEqual(manifest_meeting_fields(alias)["meeting_profile"], "legacy")
        self.assertEqual(alias["count"], 2)
        absent = done_meeting_fields({"record_id": "r2"})
        self.assertEqual(absent["meeting_inputs"]["reference_status"], "unknown")
        self.assertEqual(absent["meeting_reference_evaluation"], "unknown")

    def test_reference_links_are_associated_but_never_pass(self):
        result = done_meeting_fields({"modified_reference_links": "https://example.invalid/ref"})
        self.assertEqual(result["meeting_inputs"]["modified_reference_links"],
                         ["https://example.invalid/ref"])
        self.assertEqual(result["meeting_reference_evaluation"], "unknown")

    def test_original_voice_fills_missing_normalized_voice(self):
        task = normalize_meeting_task({"voice_original": "原始音色填写"})
        self.assertEqual(task["voice"], "原始音色填写")
        self.assertEqual(task["meeting_inputs"]["voice_original"], "原始音色填写")

    def test_invalid_tempo_stays_invalid_without_defaulting(self):
        fields = manifest_meeting_fields({"voice_tempo_status": "invalid",
                                          "voice_tempo_original": "快一点"})
        self.assertIsNone(fields["meeting_inputs"]["voice_tempo"])
        self.assertEqual(fields["meeting_inputs"]["voice_tempo_status"], "invalid")


if __name__ == "__main__":
    unittest.main()
