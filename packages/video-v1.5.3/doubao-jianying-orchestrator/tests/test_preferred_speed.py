import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import shot_analyzer, timing_contract as tc


class PreferredSpeedTests(unittest.TestCase):
    def request(self, **changes):
        values = dict(source_in_us=0, source_out_us=10_000_000,
                      audio_duration_us=2_000_000, tail_pad_us=0,
                      head_guard_us=0, tail_guard_us=0,
                      speed_min=0.8, speed_max=1.5, preferred_speed=1.3)
        values.update(changes)
        return tc.WindowRequest(**values)

    def test_preferred_1_3_is_selected_when_feasible(self):
        got = tc.solve_window(self.request())
        self.assertTrue(got.ok, got.to_dict())
        self.assertAlmostEqual(got.speed, tc.frame_speed(1.3), places=8)

    def test_unavailable_preference_uses_nearest_feasible_speed(self):
        got = tc.solve_window(self.request(speed_max=1.2))
        self.assertTrue(got.ok, got.to_dict())
        self.assertAlmostEqual(got.speed, tc.frame_speed(1.2, 0.8, 1.2), places=8)

    def test_evidence_remains_fully_covered(self):
        got = tc.solve_window(self.request(evidence_start_us=3_000_000,
                                           evidence_end_us=4_500_000))
        self.assertTrue(got.ok, got.to_dict())
        self.assertLessEqual(got.source_start_us, 3_000_000)
        self.assertGreaterEqual(got.source_end_us, 4_500_000)

    def test_shot_preference_flows_to_single_and_span_solvers(self):
        shot = {"source_start": 0, "source_end": 10, "material_duration_us": 10_000_000,
                "role": "product_display", "selection_mode": "ai_editor",
                "speed_range_override": [0.8, 1.5], "preferred_speed": 1.3,
                "frame_time": 5, "duration": 10}
        manifest = {"head_trim_s": 0, "tail_trim_s": 0, "tail_pad_s": 0, "fps": 30}
        single = shot_analyzer.candidate_window(shot, audio_duration_us=2_000_000,
                                               manifest=manifest, cover_evidence=False)
        self.assertTrue(single.ok, single.to_dict())
        self.assertAlmostEqual(single.speed, tc.frame_speed(1.3), places=8)

        segment = shot_analyzer.span_segment_for(shot, manifest=manifest)
        self.assertEqual(segment.preferred_speed, 1.3)
        span = tc.solve_span(tc.SpanRequest(segments=(segment, segment),
                                            audio_duration_us=2_000_000,
                                            tail_pad_us=0))
        self.assertTrue(span.ok, span.to_dict())
        self.assertEqual(len(span.parts), 2)
        self.assertTrue(all(abs(part.speed - tc.frame_speed(1.3)) < 1e-8
                            for part in span.parts))

    def test_default_preference_preserves_original_1x_choice(self):
        req = self.request()
        got = tc.solve_window(tc.dataclasses.replace(req, preferred_speed=1.0))
        self.assertTrue(got.ok, got.to_dict())
        self.assertAlmostEqual(got.speed, tc.frame_speed(1.0), places=8)

    def test_invalid_preference_fails_bounded(self):
        for value in (0, -1, 20.01, float("inf"), "bad"):
            with self.subTest(value=value):
                got = tc.solve_window(self.request(preferred_speed=value))
                self.assertFalse(got.ok)
                self.assertEqual(got.reason, tc.REASON_SPEED_OUT_OF_RANGE)
                self.assertEqual(got.detail["allowed_preference"], [0.05, 20.0])


if __name__ == "__main__":
    unittest.main()
