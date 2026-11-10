"""Focused tests for the 2026-11-09 fix rerun (boundary, sampling, pairs, feedback scope, clamp)."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.selection import ai_editor, experiment_contract, experiment_groups
from orchestrator.vision import experiment_event_review as events


# Synthetic contiguous B observation (frames-only) and the ids it was shown.
B_ROW = {"verdict": "observed", "coverage": "complete_verification", "effective_window_frame_ids": [94, 159],
         "contact_first_frame": 94, "release_first_frame": 127, "result_first_frame": 157,
         "events": [{"kind": "press", "first_frame": 94, "last_frame": 126, "observed": "x"},
                    {"kind": "lift", "first_frame": 127, "last_frame": 156, "observed": "x"},
                    {"kind": "result", "first_frame": 157, "last_frame": 159, "observed": "x"}],
         "limitations": ""}
B_SHOWN = [0, 1, 31, 32, 33, 62, 63, 64, 94, 95, 96, 125, 126, 127, 157, 158, 159, 188, 189, 190, 220, 221, 222,
           251, 252, 253, 283, 284, 285, 314, 315, 316, 346, 347, 348, 377, 378, 379, 409, 410, 411, 440, 441,
           442, 472, 473]


class BoundaryTests(unittest.TestCase):
    def test_interior_last_frame_follows_contiguity(self):
        # Old check rejected lift.last_frame=156 (result starts at shown 157).
        self.assertEqual(events.unshown_boundaries(B_ROW, B_SHOWN), [])
        row, settled = events.settle_boundaries(B_ROW, B_SHOWN, lambda r: True,
                                                lambda m: self.fail("no reask needed"))
        self.assertIsNone(settled)
        self.assertEqual(row, B_ROW)

    def test_one_frame_miss_snaps_and_keeps_markers(self):
        row = copy.deepcopy(B_ROW)
        row["events"][1]["first_frame"] = 128; row["events"][0]["last_frame"] = 127; row["release_first_frame"] = 128
        self.assertEqual(events.unshown_boundaries(row, B_SHOWN), [128])
        snapped, moves = events.snap_boundaries(row, B_SHOWN)
        self.assertEqual(snapped["events"][1]["first_frame"], 127)
        self.assertEqual(snapped["events"][0]["last_frame"], 126)
        self.assertEqual(snapped["release_first_frame"], 127)
        self.assertEqual(moves[0]["from"], 128)

    def test_far_miss_reasks_once_then_rejects(self):
        row = copy.deepcopy(B_ROW)
        row["events"][1]["first_frame"] = 140; row["events"][0]["last_frame"] = 139; row["release_first_frame"] = 140
        calls = []
        good, settled = events.settle_boundaries(row, B_SHOWN, lambda r: True,
                                                 lambda m: calls.append(m) or copy.deepcopy(B_ROW))
        self.assertEqual(calls, [[140]]); self.assertEqual(settled["action"], "reasked"); self.assertEqual(good, B_ROW)
        with self.assertRaisesRegex(ValueError, "BOUNDARY_NOT_SHOWN"):
            events.settle_boundaries(row, B_SHOWN, lambda r: True, lambda m: copy.deepcopy(row))

    def test_non_contiguous_chain_is_not_snapped(self):
        row = copy.deepcopy(B_ROW); row["events"][0]["last_frame"] = 120
        self.assertEqual(events.unshown_boundaries(row, B_SHOWN), ["contiguity"])
        self.assertIsNone(events.snap_boundaries(row, B_SHOWN)[0])


def samples(n, fps=10):
    return {"frames": [{"pts_us": round(i * 1e6 / fps)} for i in range(n)]}


class PairSamplingTests(unittest.TestCase):
    def test_short_window_unchanged(self):
        s = samples(300)
        self.assertEqual(experiment_contract.pair_sample_ids(s, 10, 250),
                         sorted({round(10 + i * 240 / 7) for i in range(8)}))

    def test_long_window_denser_and_bounded(self):
        s = samples(700)  # 68 s clip
        ids = experiment_contract.pair_sample_ids(s, 0, 680)
        self.assertEqual(len(ids), 14)
        self.assertLessEqual(max(b - a for a, b in zip(ids, ids[1:])), 53)

    def test_focus_frames_inside_located_pour(self):
        s = samples(700)
        focus = experiment_contract.focus_frame_ids(s, 0, 680, [(20_000_000, 23_000_000)])
        self.assertTrue(1 <= len(focus) <= 6)
        self.assertTrue(all(200 <= i <= 230 for i in focus))
        ids = experiment_contract.pair_sample_ids(s, 0, 680, focus)
        self.assertTrue(set(focus) <= set(ids)); self.assertLessEqual(len(ids), 20)


class PairPoolTests(unittest.TestCase):
    def test_limit_raised_modestly(self):
        self.assertEqual(experiment_groups.PAIR_LIMIT, 6)

    def test_adjacent_files(self):
        hint = experiment_contract.adjacency_hint(r"\\nas\a\IMG_0218.MOV", r"\\nas\a\IMG_0219.MOV")
        self.assertTrue(hint["adjacent"])
        self.assertFalse(experiment_contract.adjacency_hint(r"\\nas\a\IMG_0218.MOV", r"\\nas\b\IMG_0219.MOV")["adjacent"])


class FeedbackScopeTests(unittest.TestCase):
    sentence = {"index": 4, "text": "t", "qc_feedback": {"claim": "t", "observed": "old", "reason": "r",
                "prior_shot_ids": ["s1"], "same_shot_recut": {"shot_id": "s1", "start": 3.765, "end": 5.4}}}

    def test_same_shot_keeps_recut(self):
        self.assertIs(ai_editor._feedback_for_shot(self.sentence, "s1"), self.sentence)

    def test_other_shot_drops_recut_window(self):
        scoped = ai_editor._feedback_for_shot(self.sentence, "s2")
        self.assertNotIn("same_shot_recut", scoped["qc_feedback"])
        self.assertIn("applies_to", scoped["qc_feedback"])
        self.assertIn("same_shot_recut", self.sentence["qc_feedback"])  # original untouched


class ContinuityRefillTests(unittest.TestCase):
    def test_partial_group_replacement_replans_whole_group(self):
        group = {"schema": "reviewed-sentence-continuity-v1", "input": {"members": [{"index": 2}, {"index": 3}]}}
        manifest = {"segments": [{"text": "一句"}, {"text": "二句"}, {"text": "三句"}]}
        matched = {"segments": [{"index": 1, "text": "一句"}, {"index": 2, "text": "二句", "continuity_group": group},
                                {"index": 3, "text": "三句", "continuity_group": group}]}
        seen = {}
        def fake_groups(manifest, analysis, by_index, wanted, directory, narration_timing):
            seen["wanted"] = list(wanted)
            return set(), {i: "stub" for i in wanted}
        with tempfile.TemporaryDirectory() as tmp, patch.object(experiment_groups, "refill_groups", fake_groups):
            result = ai_editor.refill_indices(manifest, {"shots": []}, matched, [3], Path(tmp))
        self.assertEqual(seen["wanted"], [2, 3])
        self.assertEqual(result["refill_indices"], [2, 3])
        self.assertFalse(result["ok"])


class FocusFrameTests(unittest.TestCase):
    def test_long_shot_extracts_target_window_not_only_even_samples(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        from orchestrator.vision import material_cache
        from orchestrator.runtime import platform_env, runtime_policy
        from orchestrator.selection import shot_analyzer
        seen = []
        def extract(source, requests, **kwargs):
            seen.extend(t for t, _ in requests)
            for _, target in requests:
                target.write_bytes(b"decoded frame")
            return [True] * len(requests)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp4"
            source.write_bytes(b"fixture")
            shot = {"shot_id": "s", "video": str(source), "source_start": 0, "source_end": 100,
                    "_review_focus_window": [90, 91]}
            with patch.object(material_cache, "cached_frames", side_effect=lambda s, d, prepare, **kw: prepare(root)) as cache, \
                 patch.object(material_cache, "cached_sheet", return_value=root / "sheet.jpg"), \
                 patch.object(platform_env, "find_ffmpeg", return_value=("ffmpeg", "ffprobe")), \
                 patch.object(runtime_policy, "media_slot", return_value=nullcontext(SimpleNamespace(acquired=True))), \
                 patch.object(shot_analyzer, "extract_frames_batch", side_effect=extract):
                media = ai_editor._review_media(shot, root)
        self.assertGreaterEqual(len([v for v in seen if 90 <= v < 91]), 3)
        self.assertEqual(media["times"], seen)
        self.assertIn("focus", cache.call_args.kwargs["protocol"])


class SourceFrameRateTests(unittest.TestCase):
    def test_unknown_or_failed_probe_has_no_tolerance_and_is_not_cached(self):
        from types import SimpleNamespace
        import subprocess
        ai_editor._FRAME_US_CACHE.clear()
        with patch("orchestrator.runtime.platform_env.find_ffmpeg", return_value=("ffmpeg", "ffprobe")):
            with patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ffprobe")):
                self.assertEqual(ai_editor._source_frame_us({"video": "unknown"}), 0)
            with patch("subprocess.run", return_value=SimpleNamespace(stdout="0/0,0/0")):
                self.assertEqual(ai_editor._source_frame_us({"video": "unknown"}), 0)
            self.assertNotIn("unknown", ai_editor._FRAME_US_CACHE)
            with patch("subprocess.run", return_value=SimpleNamespace(stdout="120/1,120/1")):
                self.assertEqual(ai_editor._source_frame_us({"video": "unknown"}), 8333)
        ai_editor._FRAME_US_CACHE.clear()


class PlacementClampTests(unittest.TestCase):
    def run_review(self, end_us, frame_us=33_367):
        result = {"verdict": "suitable", "same_product": True, "observation": "o", "reason": "r", "confidence": .9,
                  "evidence_intervals_us": [{"start": 2_038_000, "end": end_us}]}
        media = {"start": 2.038, "end": 6.095708, "before": {"path": "v"}, "times": [2.1], "sheet": Path("x.jpg")}
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(ai_editor, "_review_media", return_value=media), \
                patch.object(ai_editor, "_call", return_value=copy.deepcopy(result)), \
                patch.object(ai_editor, "_source_frame_us", return_value=frame_us), \
                patch.object(ai_editor.contract, "policy_prompt", return_value=""):
            out = ai_editor._review_placement_window({}, {"index": 4, "text": "t"}, {"shot_id": "s", "video": "v"},
                                                     Path(tmp), parent={"evidence_intervals_us": []})
            clamp_files = list((Path(tmp) / "reviews").glob("*.clamp.json"))
        return out, clamp_files

    def test_18ms_overshoot_clamped(self):
        out, files = self.run_review(6_114_000)  # edge rounding: +18.292 ms
        self.assertEqual(out["evidence_intervals_us"], [{"start": 2_038_000, "end": 6_095_708}])
        self.assertEqual(out["verdict"], "matched"); self.assertEqual(len(files), 1)

    def test_unknown_frame_rate_keeps_overshoot_blocking(self):
        with self.assertRaisesRegex(ValueError, "PLACEMENT_EVIDENCE_INVALID"):
            self.run_review(6_105_708, frame_us=0)

    def test_high_frame_rate_overshoot_rejected(self):
        for frame_us in (8_333, 4_167):
            with self.subTest(frame_us=frame_us), self.assertRaisesRegex(ValueError, "PLACEMENT_EVIDENCE_INVALID"):
                self.run_review(6_105_708, frame_us=frame_us)

    def test_more_than_one_frame_rejected(self):
        with self.assertRaisesRegex(ValueError, "PLACEMENT_EVIDENCE_INVALID"):
            self.run_review(6_095_708 + 40_000)


if __name__ == "__main__":
    unittest.main()
