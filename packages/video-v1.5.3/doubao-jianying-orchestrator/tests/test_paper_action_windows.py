"""Frame-domain blockers for fixed-duration, continuous paper action previews."""
import copy
from fractions import Fraction
import json
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.selection import paper_action_windows as windows

US = 1_000_000


def phase(start, end, kind, can_speed=False, **extra):
    return dict(start_us=start, end_us=end, kind=kind, can_speed=can_speed, **extra)


class PaperActionWindowsTests(unittest.TestCase):
    def setUp(self):
        self.pts = [round(Fraction(i * US, 60)) for i in range(180)]
        self.phases = [phase(0, 200_000, "key_action"), phase(200_000, 1_000_000, "process", True),
                       phase(1_000_000, 1_200_000, "key_action"), phase(1_200_000, 1_400_000, "result")]
        self.policy = dict(source_end_us=3_000_000, source_fps=60, min_result_frames=3,
                           source="synthetic.mp4", target_start_us=14_724_609, target_duration_us=1_116_959)

    def plan(self, phases=None, **changes):
        args = dict(self.policy, window_start_us=0, window_end_us=1_400_000)
        args.update(changes)
        return windows.plan_action_window(self.pts, self.phases if phases is None else phases, **args)

    def validate(self, plan, phases=None, **changes):
        args = {k: v for k, v in self.policy.items() if k != "source"}
        args.update(changes)
        return windows.validate_action_plan(plan, self.pts, self.phases if phases is None else phases, **args)

    def longer(self, process_end):
        return [phase(0, 200_000, "key_action"), phase(200_000, process_end, "process", True),
                phase(process_end, process_end + 200_000, "key_action"),
                phase(process_end + 200_000, process_end + 400_000, "result")]

    def test_fixed_s10_sentence_is_exact_microseconds_and_explicitly_33_frames(self):
        result = self.plan()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tier"], "PREFERRED")
        self.assertEqual(sum(p["segment_us"] for p in result["parts"]), 1_116_959)
        self.assertEqual(sum(p["output_frames"] for p in result["parts"]), 33)
        target = result["target"]
        self.assertEqual(target["target_start_us"], 14_724_609)
        self.assertEqual((target["output_start_frame"], target["output_end_frame"]), (442, 475))
        self.assertEqual(target["presentation_duration_us"], {"numerator": 1_100_000, "denominator": 1})
        self.assertEqual(target["duration_quantization_error_us"], -16_959)
        self.assertTrue(target["microsecond_exact"])
        self.assertFalse(target["frame_duration_exact"])
        self.assertTrue(self.validate(result)["ok"])
        json.dumps(result, allow_nan=False)

    def test_strict_frame_duration_mode_rejects_incompatible_microseconds(self):
        result = self.plan(require_exact_frame_duration=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "TARGET_FRAME_DURATION_INCOMPATIBLE")
        exact = self.plan(target_start_us=0, target_duration_us=1_100_000,
                          require_exact_frame_duration=True)
        self.assertTrue(exact["ok"], exact)
        self.assertTrue(exact["target"]["frame_duration_exact"])

    def test_normal_sixty_to_thirty_sampling_covers_protected_time_at_one_speed(self):
        result = self.plan()
        protected = [p for p in result["parts"] if p["locked"]]
        self.assertEqual([p["speed"] for p in protected], [1, 1])
        self.assertTrue(all(p["source_time_map_speed"] == 1 for p in protected))
        samples = result["proof"]["samples"]
        self.assertEqual(len(samples), 33)
        self.assertEqual([s["source_frame_index"] for s in samples[:6]], [0, 2, 4, 6, 8, 10])
        final = samples[-1]["source_coverage_end_us"]
        self.assertEqual(Fraction(final["numerator"], final["denominator"]), 1_400_000)
        self.assertFalse(result["proof"]["tail_freeze_required"])
        self.assertEqual(result["proof"]["result_output_frames"], 6)

    def test_discrete_speed_is_checked_separately_from_nominal_speed(self):
        result = self.plan()
        process = next(p for p in result["parts"] if not p["locked"])
        self.assertLess(process["speed"], process["frame_domain_speed"])
        self.assertAlmostEqual(process["frame_domain_speed"], 1.6)
        self.assertEqual(process["output_frames"], 15)

    def test_exact_frame_map_avoids_over_cap_boundary_jump_from_nominal_sampling(self):
        result = self.plan()
        samples = result["proof"]["samples"]
        steps = [b["source_frame_index"] - a["source_frame_index"] for a, b in zip(samples, samples[1:])]
        self.assertTrue(all(1 <= step <= 4 for step in steps))
        # The former nominal setpts sampling ended this process at frame 55,
        # then jumped to protected frame 60: an implicit 2.5x boundary step.
        process_samples = [s for s in samples if not s["locked"]]
        self.assertEqual(process_samples[-1]["source_frame_index"], 56)
        self.assertEqual(result["proof"]["maximum_source_frame_step"], 4)
        self.assertTrue(result["proof"]["requires_explicit_frame_map"])
        self.assertEqual(result["rendering_contract"], "explicit-source-frame-map")

    def test_hard_cap_mapping_never_has_seven_frame_jump_including_part_boundaries(self):
        phases = self.longer(1_700_000)
        result = self.plan(phases, window_end_us=2_100_000)
        samples = result["proof"]["samples"]
        for a, b in zip(samples, samples[1:]):
            delta = b["source_frame_index"] - a["source_frame_index"]
            self.assertGreater(delta, 0)
            self.assertLessEqual(delta, 6)
            if a["locked"] and a["part_index"] == b["part_index"]:
                self.assertEqual(delta, 2)
        self.assertEqual(result["proof"]["maximum_source_frame_step"], 6)

    def test_noninteger_cfr_sampling_ratio_is_explicitly_unsupported(self):
        self.assertEqual(self.plan(output_fps=29.97)["reason"],
                         "NONINTEGER_SOURCE_OUTPUT_RATIO_UNSUPPORTED")

    def test_three_speed_is_exception_only_when_preferred_has_no_frame_fit(self):
        phases = self.longer(1_700_000)
        result = self.plan(phases, window_end_us=2_100_000)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["tier"], "EXCEPTION")
        self.assertEqual(result["maximum_frame_domain_speed"], 3)
        self.assertTrue(all(p["speed"] == 1 for p in result["parts"] if p["locked"]))
        self.assertTrue(self.validate(result, phases)["ok"])
        refused = self.plan(phases, window_end_us=2_100_000, hard_cap=2)
        self.assertFalse(refused["ok"])

    def test_nominal_below_three_does_not_excuse_discrete_speed_above_three(self):
        phases = self.longer(1_716_667)
        # Continuous arithmetic is <3x, but 15 available output frames require
        # 91 source frames / 60fps / .5s = 3.0333x. This must be blocked.
        self.assertLess(1_516_667 / (1_116_959 - 600_000), 3)
        result = self.plan(phases, window_end_us=2_116_667)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "FRAME_DOMAIN_DURATION_CONFLICT")

    def test_source_too_short_is_not_slowed_or_frozen(self):
        phases = [phase(0, 400_000, "key_action"), phase(400_000, 1_000_000, "result")]
        result = self.plan(phases, window_end_us=1_000_000)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "SOURCE_TOO_SHORT")

    def test_result_interval_with_no_output_sample_cannot_pass(self):
        phases = [phase(0, 200_000, "key_action"), phase(200_000, 1_000_000, "process", True),
                  phase(1_000_000, 1_399_000, "key_action"), phase(1_399_000, 1_400_000, "result")]
        result = self.plan(phases, min_result_frames=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "INSUFFICIENT_RESULT_OUTPUT_FRAMES")

    def test_result_minimum_is_explicit_caller_policy(self):
        self.assertTrue(self.plan(min_result_frames=6)["ok"])
        self.assertEqual(self.plan(min_result_frames=7)["reason"], "INSUFFICIENT_RESULT_OUTPUT_FRAMES")
        for invalid in (0, -1, True, 1.5, None):
            with self.subTest(value=invalid):
                self.assertFalse(self.plan(min_result_frames=invalid)["ok"])

    def test_source_frame_gap_is_not_treated_as_continuous_action(self):
        pts = self.pts[:30] + self.pts[31:]
        result = windows.plan_action_window(pts, self.phases, **self.policy,
                                             window_start_us=0, window_end_us=1_400_000)
        self.assertEqual(result["reason"], "SOURCE_FRAME_GAP_OR_NON_CFR")

    def test_phase_gap_overlap_or_missing_result_is_rejected(self):
        for changes in ({"start_us": 200_001}, {"start_us": 199_999}):
            phases = copy.deepcopy(self.phases)
            phases[1].update(changes)
            self.assertEqual(self.plan(phases)["reason"], "INVALID_OR_NONCONTIGUOUS_PHASES")
        self.assertEqual(self.plan(self.phases[:-1], window_end_us=1_200_000)["reason"],
                         "COMPLETE_ACTION_AND_RESULT_REQUIRED")

    def test_key_action_and_result_cannot_be_labelled_compressible(self):
        for index in (0, 3):
            phases = copy.deepcopy(self.phases)
            phases[index]["can_speed"] = True
            self.assertEqual(self.plan(phases)["reason"], "PROTECTED_PHASE_CANNOT_SPEED")

    def test_half_frame_protection_uses_ceil_not_nearest_or_floor(self):
        phases = [phase(0, 250_000, "key_action"), phase(250_000, 1_050_000, "process", True),
                  phase(1_050_000, 1_250_000, "key_action"), phase(1_250_000, 1_450_000, "result")]
        # At this absolute target position .25s gets only seven output frames,
        # while its complete protected motion needs ceil(7.5) = eight.
        result = self.plan(phases, window_end_us=1_450_000)
        self.assertEqual(result["reason"], "FRAME_DOMAIN_DURATION_CONFLICT")

    def test_nearest_even_absolute_frame_ties_are_exact(self):
        for frame in range(1, 12):
            lo, hi = windows._cell(frame, Fraction(5))
            for t in (lo, hi):
                self.assertEqual(windows._round_frame(t, Fraction(5)), frame)
            self.assertNotEqual(windows._round_frame(lo - 1, Fraction(5)), frame)
            self.assertNotEqual(windows._round_frame(hi + 1, Fraction(5)), frame)
        self.assertEqual(windows._round_frame(100_000, Fraction(5)), 0)
        self.assertEqual(windows._round_frame(300_000, Fraction(5)), 2)

    def test_rounded_source_pts_do_not_invent_an_extra_protected_frame(self):
        # A ten-source-frame protected interval is exactly five output frames,
        # even when its integer-microsecond span rounds up to 166667us.
        phases = [phase(0, 166_667, "key_action"), phase(166_667, 1_000_000, "process", True),
                  phase(1_000_000, 1_200_000, "key_action"), phase(1_200_000, 1_400_000, "result")]
        result = self.plan(phases)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["parts"][0]["output_frames"], 5)
        self.assertEqual(result["parts"][0]["source_frame_duration_us"],
                         {"numerator": 500_000, "denominator": 3})

    def test_uncertain_action_boundary_is_protected_outward_to_source_frames(self):
        phases = [phase(0, 200_000, "setup", True),
                  phase(200_000, 400_000, "key_action", start_uncertainty_us=10_000,
                        end_uncertainty_us=10_000),
                  phase(400_000, 1_000_000, "process", True),
                  phase(1_000_000, 1_200_000, "key_action"), phase(1_200_000, 1_400_000, "result")]
        result = self.plan(phases, window_start_us=200_000)
        self.assertEqual(result["reason"], "WINDOW_CUTS_PROTECTED_ACTION")
        enumerated = windows.enumerate_action_windows(self.pts, phases, **self.policy)
        self.assertTrue(enumerated["ok"], enumerated)
        for candidate in enumerated["plans"]:
            self.assertLessEqual(candidate["source_window_us"]["start"], 183_333)
            locked = [p for p in candidate["parts"] if p["locked"]]
            self.assertTrue(any(p["source_start_us"] <= 183_333 and p["source_end_us"] >= 416_667 for p in locked))

    def test_uncertain_result_does_not_count_ambiguous_appearance_as_result(self):
        phases = copy.deepcopy(self.phases)
        phases[-1].update(start_uncertainty_us=100_000, end_uncertainty_us=100_000)
        self.assertEqual(self.plan(phases)["reason"], "RESULT_BOUNDARY_UNCERTAIN")

    def test_enumerator_only_trims_reviewed_optional_head_and_tail(self):
        phases = [phase(0, 100_000, "setup", True), phase(100_000, 300_000, "key_action"),
                  phase(300_000, 1_100_000, "process", True),
                  phase(1_100_000, 1_300_000, "key_action"), phase(1_300_000, 1_500_000, "result"),
                  phase(1_500_000, 1_600_000, "display", True)]
        result = windows.enumerate_action_windows(self.pts, phases, **self.policy)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["complete"])
        self.assertEqual(result["windows_scanned"], 49)
        self.assertEqual(result["best"]["tier"], "PREFERRED")
        for candidate in result["plans"]:
            self.assertLessEqual(candidate["source_window_us"]["start"], 100_000)
            self.assertGreaterEqual(candidate["source_window_us"]["end"], 1_500_000)
            self.assertTrue(self.validate(candidate, phases)["ok"])

    def test_enumeration_limit_is_incomplete_not_a_material_gap(self):
        phases = [phase(0, 200_000, "setup", True)] + [dict(p, start_us=p["start_us"] + 200_000,
                   end_us=p["end_us"] + 200_000) for p in self.phases]
        result = windows.enumerate_action_windows(self.pts, phases, **self.policy, max_windows=1)
        self.assertEqual(result["reason"], "ENUMERATION_LIMIT")
        self.assertFalse(result["complete"])

    def test_validator_does_not_trust_stored_proof_or_success_flags(self):
        result = self.plan()
        result["proof"] = {"samples": [], "tail_freeze_required": True}
        checked = self.validate(result)
        self.assertFalse(checked["ok"])
        self.assertEqual(checked["reason"], "SOURCE_FRAME_MAP_MISSING")

    def test_validator_blocks_tampered_repeat_seven_frame_jump_and_protected_step(self):
        result = self.plan()
        result["proof"]["samples"][1]["source_frame_index"] = 0
        self.assertEqual(self.validate(result)["reason"], "SOURCE_FRAME_REPEAT_OR_REVERSE")
        phases = self.longer(1_700_000)
        result = self.plan(phases, window_end_us=2_100_000)
        process = [s for s in result["proof"]["samples"] if not s["locked"]]
        process[1]["source_frame_index"] = process[0]["source_frame_index"] + 7
        self.assertEqual(self.validate(result, phases)["reason"], "INSTANTANEOUS_SPEED_CAP_EXCEEDED")
        result = self.plan()
        result["proof"]["samples"][1]["source_frame_index"] = 3
        self.assertEqual(self.validate(result)["reason"], "PROTECTED_SOURCE_FRAME_STEP_CHANGED")

    def test_validator_blocks_source_gap_and_lost_last_source_frame(self):
        for index, key, increment in ((1, "source_start_us", 16_667), (-1, "source_end_us", -16_667)):
            result = self.plan()
            result["parts"][index][key] += increment
            self.assertEqual(self.validate(result)["reason"], "SOURCE_PART_GAP_OR_PROTECTION_CHANGED")

    def test_validator_blocks_accelerated_protection_and_output_frame_tampering(self):
        result = self.plan()
        result["parts"][0]["speed"] = 2
        self.assertEqual(self.validate(result)["reason"], "SPEED_DOES_NOT_MATCH_SOURCE_TIME_MAP")
        result = self.plan()
        result["parts"][-1]["output_frames"] -= 1
        self.assertEqual(self.validate(result)["reason"], "OUTPUT_FRAME_ACCOUNTING_MISMATCH")

    def test_validator_blocks_source_switch_and_false_preferred_tier(self):
        result = self.plan()
        result["parts"][1]["source"] = "different.mp4"
        self.assertEqual(self.validate(result)["reason"], "SOURCE_CHANGED_BETWEEN_PARTS")
        phases = self.longer(1_700_000)
        result = self.plan(phases, window_end_us=2_100_000)
        result.update(tier="PREFERRED", cap=2)
        self.assertEqual(self.validate(result, phases)["reason"], "SPEED_CAP_EXCEEDED_OR_SLOWDOWN")

    def test_empty_or_inverted_window_bounds_cannot_silently_expand_enumeration(self):
        for bounds in ({}, {"start": 1_000_000, "end": 0}):
            result = windows.enumerate_action_windows(self.pts, self.phases, **self.policy,
                                                       window_bounds_us=bounds)
            self.assertEqual(result["reason"], "INVALID_WINDOW_BOUNDS")

    def test_invalid_boolean_nan_or_unbounded_inputs_are_structured_failures(self):
        for changes in ({"source_fps": True}, {"output_fps": float("nan")},
                        {"hard_cap": 3.01}, {"preferred_cap": 0.5}, {"target_duration_us": True},
                        {"window_start_us": False}, {"require_exact_frame_duration": 1}):
            with self.subTest(changes=changes):
                self.assertFalse(self.plan(**changes)["ok"])
        pts = list(self.pts)
        pts[1] = True
        self.assertEqual(windows.enumerate_action_windows(pts, self.phases, **self.policy)["reason"],
                         "INVALID_SOURCE_PTS")

    def test_seeded_frame_properties_keep_contiguity_caps_and_exact_sentence(self):
        randomizer = random.Random(34)
        feasible = 0
        for _ in range(50):
            process_frames = randomizer.randrange(30, 91, 2)
            process_end = round(Fraction((12 + process_frames) * US, 60))
            phases = self.longer(process_end)
            result = self.plan(phases, window_end_us=process_end + 400_000)
            if not result["ok"]:
                continue
            feasible += 1
            self.assertTrue(self.validate(result, phases)["ok"])
            self.assertEqual(sum(p["segment_us"] for p in result["parts"]), 1_116_959)
            self.assertEqual(sum(p["output_frames"] for p in result["parts"]), 33)
            for a, b in zip(result["parts"], result["parts"][1:]):
                self.assertEqual(a["source_end_us"], b["source_start_us"])
                self.assertEqual(a["target_end_us"], b["target_start_us"])
                self.assertEqual(a["output_end_frame"], b["output_start_frame"])
            self.assertLessEqual(result["maximum_speed"], 3)
            self.assertLessEqual(result["maximum_frame_domain_speed"], 3)
        self.assertGreater(feasible, 30)


if __name__ == "__main__":
    unittest.main()
