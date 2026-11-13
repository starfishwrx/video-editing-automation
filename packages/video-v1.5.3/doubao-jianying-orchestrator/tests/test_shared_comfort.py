"""Exercise actual solved cuts, colocated candidates and protected selections."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.selection import ai_editor, usage_ledger
import test_selection_recovery as fixtures


class SharedComfortTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SelectionRecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.manifest = {**fixture.manifest, "usage_ledger_path": str(fixture.root / "ledger.json"),
                         "record_id": "current", "draft_name": "current"}
        self.plan = fixture.plan()
        self.analysis = fixture.analysis
        self.video = fixture.shots[0]["video"]
        # Same physical file, another observed scene at a disjoint time.
        fixture.shots.append({**fixture.shots[0], "shot_id": "alternative", "source_start": 5})
        self.plan["sentences"][0]["candidate_ids"].append("alternative")
        self.plan["reviews"]["1"].append({**fixture.reviews["1"][0], "shot_id": "alternative",
            "evidence_intervals_us": [{"start": 7_000_000, "end": 7_500_000}]})

    def record(self, intervals, task="other"):
        cuts = [{"video": self.video, "scene_source_start": 0, "scene_source_end": 10,
                 "source_start": a, "source_end": b} for a, b in intervals]
        usage_ledger.record({**self.manifest, "record_id": task, "draft_name": "earlier"},
                            [], placed_cuts=cuts)

    def vary(self):
        menu = ai_editor._build_edit_inventory(self.manifest, self.analysis, self.plan, None)
        ai_editor._vary_shared_comfort(self.manifest, self.analysis, self.plan, None, menu)
        return self.plan["rows"][0]["parts"][0]["shot_id"]

    def test_separate_actual_cuts_in_one_scene_are_retained(self):
        self.record([(2, 3), (8, 9)])
        used = usage_ledger.other_task_windows(self.manifest)
        self.assertEqual([(r["start_us"], r["end_us"]) for r in used],
                         [(2_000_000, 3_000_000), (8_000_000, 9_000_000)])
        self.assertEqual(self.vary(), "alternative")

    def test_unplaced_scene_remainder_and_same_task_do_not_trigger_repair(self):
        self.record([(4, 5)])
        self.assertEqual(self.vary(), "shot1")
        self.record([(2, 3)], task="current")
        self.assertEqual(self.vary(), "shot1")

    def test_timing_evidence_or_insufficient_alternative_preserves_selection(self):
        self.record([(2, 3)])
        self.plan["reviews"]["1"][0]["time_sensitive"] = True
        self.assertEqual(self.vary(), "shot1")
        self.plan["reviews"]["1"][0]["time_sensitive"] = False
        self.plan["reviews"]["1"][1]["confidence"] = .5
        self.assertEqual(self.vary(), "shot1")

    def test_verified_action_stays_when_same_source_window_was_used(self):
        from test_presentation_windows import phase
        self.record([(0, 2)])
        self.plan["rows"][0]["parts"][0].pop("source_start_us")
        review = self.plan["reviews"]["1"][0]
        review.update(presentation_observation=phase(),
                      evidence_intervals_us=[{"start": 700_000, "end": 1_100_000}])
        self.assertEqual(self.vary(), "shot1")

    def test_writer_cuts_keep_each_physical_window_and_original_scene(self):
        from orchestrator.application.engine import OrchestrationEngine
        from orchestrator.domain.clips import Clip
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine._raw_segments = [{"span_parts": [{"video": self.video, "scene_source_start": 0},
                                                {"video": self.video, "scene_source_start": 0}]}]
        engine._clips = [Clip(video=self.video, text="", raw_index=0, multi_shot=True, span_index=i,
                              source_start=start, duration=0.5, video_speed=1.5)
                         for i, start in enumerate([2, 8])]
        usage_ledger.record({**self.manifest, "draft_name": "earlier", "record_id": "other"},
                            [], placed_cuts=engine.placed_cuts())
        used = usage_ledger.other_task_windows(self.manifest)
        self.assertEqual([(r["start_us"], r["end_us"]) for r in used],
                         [(2_000_000, 2_750_000), (8_000_000, 8_750_000)])

    def test_failed_alternate_is_discarded(self):
        self.record([(2, 3)])
        self.plan["reviews"]["1"][1]["source_identity"] = {"path": "stale"}
        self.assertEqual(self.vary(), "shot1")
