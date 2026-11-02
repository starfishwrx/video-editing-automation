"""Partial speed fits preserve the full shot, original-speed evidence and sentence."""
import copy
from fractions import Fraction
import json
import math
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.selection.paper_retiming import plan_retiming

US = 1_000_000


class PaperRetimingTests(unittest.TestCase):
    def assert_valid(self, result):
        self.assertTrue(result["ok"], result)
        source_cursor = result["source_start_us"]
        output_cursor = 0
        for part in result["parts"]:
            self.assertEqual(part["source_start_us"], source_cursor)
            self.assertEqual(part["target_start_us"], output_cursor)
            self.assertGreater(part["segment_us"], 0)
            material_us = part["source_end_us"] - part["source_start_us"]
            self.assertGreater(material_us, 0)
            self.assertGreaterEqual(part["speed"], 1)
            cap = result["preferred_cap"] if result["tier"] != "EXCEPTION" else result["hard_cap"]
            self.assertLessEqual(Fraction(material_us, part["segment_us"]), Fraction(str(cap)))
            self.assertEqual(round(part["segment_us"] * part["speed"]), material_us)
            if part["locked"]:
                self.assertEqual(part["speed"], 1)
                self.assertEqual(part["segment_us"], material_us)
            source_cursor = part["source_end_us"]
            output_cursor += part["segment_us"]
        self.assertEqual(source_cursor, result["source_end_us"])
        self.assertEqual(output_cursor, result["target_duration_us"])
        self.assertEqual(result["timeline_us"], result["target_duration_us"])
        json.dumps(result, allow_nan=False)

    def test_six_seconds_fit_by_accelerating_only_process_at_two(self):
        result = plan_retiming(0, 6 * US, [(0, US), (5_500_000, 6 * US)], 3_750_000)
        self.assert_valid(result)
        self.assertEqual(result["tier"], "PREFERRED")
        self.assertEqual([p["speed"] for p in result["parts"]], [1, 2, 1])
        self.assertEqual(result["protected_us"], 1_500_000)

    def test_three_is_exception_only_when_two_cannot_fit(self):
        result = plan_retiming(0, 6 * US, [(0, US), (5_500_000, 6 * US)], 3 * US)
        self.assert_valid(result)
        self.assertEqual(result["tier"], "EXCEPTION")
        self.assertEqual([p["speed"] for p in result["parts"]], [1, 3, 1])
        self.assertEqual(result["exception_reason"], "NO_PREFERRED_SPEED_FIT")

    def test_too_short_stays_conflict_without_exceeding_cap_or_cutting_evidence(self):
        result = plan_retiming(0, 6 * US, [(0, US), (5_500_000, 6 * US)], 2_900_000)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "DURATION_FIT_CONFLICT")
        self.assertEqual(result["detail"]["minimum_target_us"], 3 * US)
        self.assertEqual(result["parts"], [])

    def test_exact_original_duration_has_no_speed_changes(self):
        result = plan_retiming(4 * US, 10 * US, [(5 * US, 6 * US)], 6 * US)
        self.assert_valid(result)
        self.assertEqual(result["tier"], "ORIGINAL")
        self.assertTrue(all(p["speed"] == 1 for p in result["parts"]))
        self.assertEqual(result["exception_reason"], "")

    def test_source_shorter_than_sentence_does_not_synthesize_slowdown_or_hold(self):
        result = plan_retiming(0, US, [], US + 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "SOURCE_TOO_SHORT")
        self.assertEqual(result["parts"], [])

    def test_overlapping_duplicate_and_touching_protection_is_merged_without_mutation(self):
        intervals = [{"start": 4 * US, "end": 5 * US}, [US, 3 * US],
                     (2 * US, 4 * US), (US, 3 * US)]
        before = copy.deepcopy(intervals)
        result = plan_retiming(0, 6 * US, intervals, 5 * US)
        self.assert_valid(result)
        self.assertEqual(intervals, before)
        self.assertEqual(result["protected_intervals"], [{"start": US, "end": 5 * US}])
        self.assertEqual(result["protected_us"], 4 * US)
        self.assertEqual([p["speed"] for p in result["parts"]], [2, 1, 2])

    def test_protected_all_source_is_original_only(self):
        original = plan_retiming(10, 110, [(10, 110)], 100)
        self.assert_valid(original)
        self.assertEqual(original["tier"], "ORIGINAL")
        shorter = plan_retiming(10, 110, [(10, 110)], 99)
        self.assertFalse(shorter["ok"])
        self.assertEqual(shorter["detail"]["cause"], "NO_COMPRESSIBLE_TIME")

    def test_protection_cannot_consume_or_exceed_entire_target(self):
        for target in (US, US - 1):
            with self.subTest(target=target):
                result = plan_retiming(0, 3 * US, [(US, 2 * US)], target)
                self.assertFalse(result["ok"])
                self.assertEqual(result["reason"], "DURATION_FIT_CONFLICT")
                self.assertEqual(result["detail"]["cause"], "PROTECTED_TIME_EXHAUSTS_TARGET")

    def test_empty_protection_is_one_continuous_uniform_part(self):
        result = plan_retiming(2 * US, 6 * US, [], 3 * US)
        self.assert_valid(result)
        self.assertEqual(len(result["parts"]), 1)
        self.assertEqual(result["parts"][0]["speed"], 4 / 3)

    def test_task_specific_two_cap_never_uses_three_fallback(self):
        result = plan_retiming(0, 6 * US, [(0, US), (5_500_000, 6 * US)],
                              3 * US, hard_cap=2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"]["minimum_target_us"], 3_750_000)

    def test_integer_apportionment_keeps_exact_duration_and_every_cap(self):
        result = plan_retiming(101, 108, [(102, 103), (105, 106)], 6)
        self.assert_valid(result)
        self.assertEqual([p["segment_us"] for p in result["parts"] if not p["locked"]], [1, 2, 1])
        self.assertEqual(result["timeline_us"], 6)
        # Three one-microsecond source phases cannot each acquire half a
        # microsecond. Their real individual speeds, not only the average, gate.
        impossible = plan_retiming(0, 5, [(1, 2), (3, 4)], 4)
        self.assertFalse(impossible["ok"])

    def test_noninteger_frame_rate_does_not_drift_microsecond_sentence(self):
        result = plan_retiming(123_456, 9_123_457,
                              [(123_456, 623_456), (8_123_457, 9_123_457)],
                              5_202_117, fps=29.97)
        self.assert_valid(result)
        self.assertEqual(result["frame_us"], round(US / 29.97))
        self.assertEqual(result["timeline_us"], 5_202_117)

    def test_seeded_feasible_shots_preserve_every_source_microsecond(self):
        rng = random.Random(34)
        for case in range(150):
            lengths = [rng.randint(2, 70_000) for _ in range(5)]
            start = rng.randint(0, 1_000_000)
            boundaries = [start]
            for length in lengths:
                boundaries.append(boundaries[-1] + length)
            protected = [(boundaries[1], boundaries[2]), (boundaries[3], boundaries[4])]
            protected_us = lengths[1] + lengths[3]
            minimum = protected_us + sum((length + 2) // 3 for length in lengths[::2])
            target = rng.randint(minimum, sum(lengths))
            with self.subTest(case=case):
                result = plan_retiming(start, boundaries[-1], protected, target)
                self.assert_valid(result)

    def test_invalid_source_window_is_rejected(self):
        for start, end in ((True, 10), (0, True), (-1, 10), (10, 10),
                           (11, 10), (0.0, 10), (0, math.nan), (0, math.inf)):
            with self.subTest(start=start, end=end):
                result = plan_retiming(start, end, [], 1)
                self.assertEqual(result["reason"], "INVALID_SOURCE_WINDOW")

    def test_invalid_target_or_fps_is_rejected(self):
        for value in (True, 0, -1, 1.0, math.nan, math.inf, "1"):
            with self.subTest(target=value):
                self.assertEqual(plan_retiming(0, 10, [], value)["reason"], "INVALID_TARGET_DURATION")
        for value in (True, 0, -1, math.nan, math.inf, "30", 1e-320, 10**400):
            with self.subTest(fps=value):
                self.assertEqual(plan_retiming(0, 10, [], 10, fps=value)["reason"], "INVALID_FPS")

    def test_invalid_protection_never_clips_or_coerces_boundaries(self):
        for intervals in (None, "none", [None], [(0,)], [(0, 1, 2)], [(3, 3)],
                          [(-1, 3)], [(0, 11)], [(True, 3)], [(0, math.nan)],
                          [{"start": 0.0, "end": 3}], [{"start": 0}], ["01"]):
            with self.subTest(intervals=intervals):
                self.assertEqual(plan_retiming(0, 10, intervals, 10)["reason"],
                                 "INVALID_PROTECTED_INTERVALS")

    def test_speed_limits_are_finite_ordered_and_cannot_relax_paper_policy(self):
        for preferred, hard in ((True, 3), (2, True), (0.9, 3), (2.1, 3),
                                (2, 1.5), (2, 3.1), (2, math.inf), (math.nan, 3)):
            with self.subTest(preferred=preferred, hard=hard):
                self.assertEqual(plan_retiming(0, 10, [], 10, preferred_cap=preferred,
                                              hard_cap=hard)["reason"], "INVALID_SPEED_LIMIT")


if __name__ == "__main__":
    unittest.main()
