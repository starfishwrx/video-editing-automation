import tempfile
import unittest
from pathlib import Path

from orchestrator.selection import ai_editor, usage_ledger


def _plan(video_a, video_b, text):
    return (
        {"shots": [
            {"shot_id": "a", "video": video_a, "source_start": 0.0, "source_end": 4.0},
            {"shot_id": "b", "video": video_b, "source_start": 0.0, "source_end": 4.0},
        ]},
        {"sentences": [{"index": 3, "kind": "selling_point", "text": text,
                        "selling_points": [{"text": text}]}],
         "rows": [{"index": 3, "parts": [{"shot_id": "a", "audio_duration_us": 1_500_000,
                                          "source_start_us": 1_000_000}]}],
         "reviews": {"3": [
             {"shot_id": "a", "verdict": "matched",
              "evidence_intervals_us": [{"start": 1_000_000, "end": 2_500_000}]},
             {"shot_id": "b", "verdict": "matched",
              "evidence_intervals_us": [{"start": 500_000, "end": 1_500_000}]},
         ]}},
    )


class SharedComfortTests(unittest.TestCase):
    def test_overlapping_surface_shot_switches_to_another_matched_shot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            video_a, video_b = r"D:\pool\soft\a.mp4", r"D:\pool\soft\b.mp4"
            usage_ledger.record(
                {"usage_ledger_path": path, "record_id": "other", "draft_name": "别的任务"},
                [{"video": video_a, "source_start": 1.0, "source_end": 3.0}])
            analysis, plan = _plan(video_a, video_b, "面层柔软又舒适")
            manifest = {"usage_ledger_path": path, "record_id": "task-b",
                        "draft_name": "本任务", "ledger_task": "task-b"}
            self.assertTrue(ai_editor._repair_shared_comfort_shot(
                manifest, analysis, plan, Path(tmp)))
            part = plan["rows"][0]["parts"][0]
            self.assertEqual(part["shot_id"], "b")
            self.assertNotIn("source_start_us", part)
            self.assertEqual(part["audio_duration_us"], 1_500_000)

    def test_only_matched_shot_stays(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            video_a, video_b = r"D:\pool\soft\a.mp4", r"D:\pool\soft\b.mp4"
            usage_ledger.record(
                {"usage_ledger_path": path, "record_id": "other", "draft_name": "别的任务"},
                [{"video": video_a, "source_start": 1.0, "source_end": 3.0}])
            analysis, plan = _plan(video_a, video_b, "柔软舒适")
            plan["reviews"]["3"] = plan["reviews"]["3"][:1]
            manifest = {"usage_ledger_path": path, "record_id": "this", "draft_name": "本任务"}
            self.assertFalse(ai_editor._repair_shared_comfort_shot(
                manifest, analysis, plan, Path(tmp)))
            self.assertEqual(plan["rows"][0]["parts"][0]["shot_id"], "a")

    def test_same_task_and_absorption_lines_stay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "ledger.json")
            video_a, video_b = r"D:\pool\soft\a.mp4", r"D:\pool\soft\b.mp4"
            usage_ledger.record(
                {"usage_ledger_path": path, "record_id": "same", "draft_name": "同任务另一版"},
                [{"video": video_a, "source_start": 1.0, "source_end": 3.0}])
            analysis, plan = _plan(video_a, video_b, "面层柔软")
            manifest = {"usage_ledger_path": path, "record_id": "same", "draft_name": "本任务"}
            self.assertFalse(ai_editor._repair_shared_comfort_shot(
                manifest, analysis, plan, Path(tmp)))
            absorption = _plan(video_a, video_b, "吸收效果杠杠好")[1]
            usage_ledger.record(
                {"usage_ledger_path": path, "record_id": "other", "draft_name": "别的任务"},
                [{"video": video_a, "source_start": 1.0, "source_end": 3.0}])
            other = {"usage_ledger_path": path, "record_id": "rec-b", "draft_name": "另一条"}
            self.assertFalse(ai_editor._repair_shared_comfort_shot(
                other, analysis, absorption, Path(tmp)))
            self.assertEqual(absorption["rows"][0]["parts"][0]["shot_id"], "a")


if __name__ == "__main__":
    unittest.main()
