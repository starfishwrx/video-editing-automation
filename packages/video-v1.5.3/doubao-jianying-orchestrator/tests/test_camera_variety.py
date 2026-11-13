"""Offline checks for cross-file camera variety on placed source windows."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.selection import ai_editor, camera_variety as camera


class CameraVarietyTests(unittest.TestCase):
    def test_same_take_and_new_fact(self):
        def segment(video, start, end):
            return dict(video=video, source_start=start, source_end=end)

        segments = [
            segment("indoors", 0, 1),
            segment("indoors", 1, 3),
            segment("outdoors", 0, 1),
            segment("outdoors", 1, 2),
            segment("other", 0, 1),
        ]
        plan = {
            "rows": [{"index": i} for i in range(1, 6)],
            "_frozen_segments": {5: segments[4]},
            "sentences": [{"index": i, "kind": "selling_point" if i == 4 else "hook"} for i in range(1, 6)],
        }
        layout = camera.groups(segments, plan, {})
        self.assertEqual(
            [(g["indices"], g["editable"]) for g in layout], [([1, 2], True), ([3, 4], False), ([5], False)]
        )

    def test_experiment_bridge_is_protected_without_a_group_receipt(self):
        segments = [dict(video=str(i), source_start=0, source_end=1) for i in range(3)]
        plan = {
            "rows": [{"index": i} for i in (1, 2, 3)],
            "sentences": [
                {"index": i, "kind": "hook", "experiment_role": role}
                for i, role in enumerate(("absorption", "bridge", "rewet"), 1)
            ],
        }
        self.assertFalse(any(g["editable"] for g in camera.groups(segments, plan, {})))

    def test_bounded_screen_and_invalid_response(self):
        groups = [dict(video=str(i), start=0, end=1000, editable=True) for i in range(50)]
        audit = {}
        with patch.object(camera, "sheet_for", return_value=Path("pixels.jpg")) as sheet:
            pairs = camera.find_repeats(groups, [], Path("."), None, lambda *_: {"rows": []}, audit=audit)
        self.assertEqual(pairs, [])
        self.assertLessEqual(sheet.call_count, 12)
        self.assertEqual(audit["incomplete_batches"], sheet.call_count)
        self.assertLess(audit["screened_pairs"], audit["eligible_pairs"])

    def test_saved_continuous_group_still_checks_actual_seam(self):
        plan = json.loads(
            json.dumps(
                {
                    "rows": [{"index": 1}, {"index": 2}],
                    "camera_continuous_groups": [{"indices": [1, 2], "key": "joint pixels"}],
                }
            )
        )
        self.assertEqual(list(ai_editor._linked_hooks(plan)), [(1, 2)])
        segments = [
            {"ai_selection": {"source_identity": "original", "window_us": {"start": 0, "end": 1}}},
            {"ai_selection": {"source_identity": "replacement", "window_us": {"start": 1, "end": 2}}},
        ]
        with patch.object(ai_editor, "_assemble_segments", return_value=(segments, [])):
            with self.assertRaisesRegex(ValueError, "AI_EDITOR_NO_SELECTION:1"):
                ai_editor._assemble({}, {}, {**plan, "sentences": []}, narration_timing=None)

            segments[0]["ai_selection"]["source_identity"] = "replacement"
            plan["camera_continuous_groups"][0].update(
                source_identity="original", windows=[{"start": 0, "end": 1}, {"start": 1, "end": 2}]
            )
            with self.assertRaisesRegex(ValueError, "AI_EDITOR_CAMERA_JOIN_CHANGED:1"):
                ai_editor._assemble({}, {}, {**plan, "sentences": []}, narration_timing=None)

    def test_independent_camera_and_action_priority(self):
        groups = [dict(video=str(i), start=0, end=1000, editable=i == 0) for i in range(3)]
        rows = [{"b": 0, "camera": False, "action": True}, {"b": 1, "camera": True, "action": True}]
        with patch.object(camera, "sheet_for", return_value=Path("pixels.jpg")):
            pairs = camera.find_repeats(groups, [], Path("."), None, lambda *_: {"rows": rows})
        self.assertEqual(pairs, [(0, 2, "camera"), (0, 1, "action"), (0, 2, "action")])
