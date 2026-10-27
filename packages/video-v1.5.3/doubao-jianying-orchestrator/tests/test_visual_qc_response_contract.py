"""Synthetic main-consumer tests; no model calls or real media inspection."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import draft_visual_qc as qc, vision_analyzer
from orchestrator import visual_qc_contract as contract


def judgment(index=1, **changes):
    return {"index": index, "match_level": "DIRECT", "visual_ok": True,
            "observed": "合成测试：产品被揉搓后展开，表面未见碎屑",
            "reason": "合成观察中包含测试句要求的动作与结果", "confidence": 0.8, **changes}


def response(rows=None, **changes):
    return {"response_version": contract.RESPONSE_VERSION, "segments": rows if rows is not None else [judgment()],
            "overall": "PASS", **changes}


class VisualQcResponseContractTests(unittest.TestCase):
    def run_main(self, responses, *, claims=1, batch_size=4):
        with tempfile.TemporaryDirectory(prefix="visual-qc-contract-") as raw:
            root = Path(raw)
            draft, report = root / "draft", root / "report"
            draft.mkdir()
            report.mkdir()
            materials, segments = [], []
            for index in range(claims):
                media = root / f"synthetic-{index}.mp4"
                media.touch()
                materials.append({"id": str(index), "path": str(media)})
                segments.append({"material_id": str(index),
                                 "source_timerange": {"start": 2_000_000, "duration": 4_000_000},
                                 "target_timerange": {"start": index * 4_000_000, "duration": 4_000_000}})
            (draft / "draft_info.json").write_text(json.dumps({"duration": claims * 4_000_000,
                "materials": {"videos": materials}, "tracks": [{"name": "video", "segments": segments}]}), encoding="utf-8")
            (report / "shot_match_report.json").write_text(json.dumps({"segments": [
                {"text": "揉搓也不掉絮", "temporary_shot_id": str(index)} for index in range(claims)]}), encoding="utf-8")
            outputs = [(True, json.dumps(row)) for row in responses]
            argv = ["qc", "--draft", str(draft), "--report-dir", str(report), "--output", str(root / "qc.json")]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, {"JY_POST_QC_BATCH_CLAIMS": str(batch_size)}), \
                    mock.patch.object(qc, "_ffmpeg", return_value="synthetic-ffmpeg"), \
                    mock.patch.object(qc, "_extract", return_value=(True, "")) as extract, \
                    mock.patch.object(vision_analyzer, "_write_sheet", return_value=True), \
                    mock.patch.object(qc, "_call_qc_model", side_effect=outputs) as model, \
                    contextlib.redirect_stdout(io.StringIO()):
                code = qc.main()
            result = json.loads((root / "qc.json").read_text(encoding="utf-8"))
            return result, code, extract.call_count, model.call_args_list

    def assert_blocked(self, body, *, status="UNCERTIFIED"):
        result, code, _, _ = self.run_main([body])
        self.assertEqual(result["status"], status, result)
        self.assertEqual(result["release_eligibility"], "BLOCKED")
        self.assertEqual(code, 2)
        self.assertEqual(contract.report_status(result), status)
        return result

    def test_complete_response_passes_and_samples_placed_source(self):
        result, code, extracted, calls = self.run_main([response()])
        self.assertEqual((result["status"], code, extracted), ("PASS", 0, 2))
        self.assertEqual(contract.report_status(result), "PASS")
        self.assertEqual(result["response_contract"]["status"], "VALIDATED")
        self.assertIn(contract.RESPONSE_VERSION, calls[0].args[2])
        self.assertIn("不得重复", calls[0].args[2])

    def test_positive_small_confidence_is_not_an_invented_quality_threshold(self):
        result, _, _, _ = self.run_main([response([judgment(confidence=0.01)])])
        self.assertEqual(result["status"], "PASS")

    def test_boolean_only_never_becomes_direct(self):
        result = self.assert_blocked({"segments": [{"index": 1, "visual_ok": True}]})
        self.assertEqual(result["model_review"][0]["match_level"], "INSUFFICIENT_EVIDENCE")

    def test_zero_confidence_empty_observation_reproduction(self):
        self.assert_blocked(response([judgment(confidence=0, observed="", reason="")]))

    def test_required_fields_and_response_version_are_enforced(self):
        for key in ("match_level", "visual_ok", "observed", "reason", "confidence"):
            with self.subTest(missing=key):
                row = judgment()
                del row[key]
                self.assert_blocked(response([row]))
        for version in (None, "chatcut-visual-qc-v2", 3):
            with self.subTest(version=version):
                self.assert_blocked(response(response_version=version))

    def test_invalid_confidences_never_pass(self):
        for confidence in (0, -0.1, 1.1, float("nan"), float("inf"), float("-inf"), True, "0.9", None):
            with self.subTest(confidence=confidence):
                self.assert_blocked(response([judgment(confidence=confidence)]))

    def test_invalid_positive_judgment_and_overall_are_uncertified(self):
        self.assert_blocked(response([judgment(visual_ok=False)]))
        self.assert_blocked(response([judgment(match_level="PASS")]))
        self.assert_blocked(response(overall="SUCCESS"))

    def test_old_explicit_mismatch_is_preserved_even_with_bad_fields(self):
        for level in ("MISMATCH", "FAIL"):
            with self.subTest(level=level):
                self.assert_blocked({"segments": [{"index": 1, "match_level": level,
                                    "visual_ok": True}], "overall": "PASS"}, status="FAIL")
        # Empty segments + overall FAIL is not an explicit mismatch; treat as UNCERTIFIED.
        self.assert_blocked({"segments": [], "overall": "FAIL"}, status="UNCERTIFIED")

    def test_duplicate_and_contradictory_rows_are_conservative(self):
        self.assert_blocked(response([judgment(), judgment()]))
        self.assert_blocked(response([judgment(), judgment(observed="另一个相互冲突的观察")]))
        for rows in ([judgment(), judgment(match_level="MISMATCH", visual_ok=False)],
                     [judgment(match_level="MISMATCH", visual_ok=False), judgment()]):
            self.assert_blocked(response(rows), status="FAIL")

    def test_out_of_batch_or_noninteger_indices_are_rejected(self):
        for index in (0, 2, "1", True, None):
            with self.subTest(index=index):
                self.assert_blocked(response([judgment(index=index)]))
        self.assert_blocked(response(["not an object"]))

    def test_multiple_batches_map_local_indices_to_every_global_claim(self):
        result, code, _, _ = self.run_main([response([judgment(1), judgment(2)]),
            response([judgment(1), judgment(2)]), response()], claims=5, batch_size=2)
        self.assertEqual((result["status"], code), ("PASS", 0))
        self.assertEqual([row["index"] for row in result["model_review"]], [1, 2, 3, 4, 5])
        self.assertEqual(result["response_contract"]["validated_batches"], [1, 2, 3])
        self.assertEqual(contract.report_status(result), "PASS")

    def test_incomplete_later_batch_blocks_whole_report(self):
        result, code, _, _ = self.run_main([response([judgment(1), judgment(2)]), response([])], claims=3, batch_size=2)
        self.assertEqual((result["status"], code), ("UNCERTIFIED", 2))
        self.assertEqual(result["response_contract"]["status"], "NOT_VALIDATED")
        self.assertIn("POST_DRAFT_VISION_MISSING:S03", result["warnings"])
        self.assertEqual(contract.report_status(result), "UNCERTIFIED")

    def test_valid_degraded_and_insufficient_keep_their_original_limits(self):
        body = response([judgment(match_level="ACCEPTABLE_DEGRADED")], overall="PASS_WITH_DEGRADED")
        result, code, _, _ = self.run_main([body])
        self.assertEqual((result["status"], code, contract.report_status(result)),
                         ("PASS_WITH_DEGRADED", 0, "PASS_WITH_DEGRADED"))
        # Model overall=UNCERTIFIED with judged segments is ignored (align with
        # FAIL ignore); INSUFFICIENT stays refillable PASS_WITH_DEGRADED.
        ignored, ignored_code, _, _ = self.run_main([response(
            [judgment(match_level="INSUFFICIENT_EVIDENCE", visual_ok=False, confidence=0.5)],
            overall="UNCERTIFIED")])
        self.assertEqual((ignored["status"], ignored_code), ("PASS_WITH_DEGRADED", 0))
        self.assertTrue(any(
            str(w).startswith("POST_DRAFT_OVERALL_UNCERTIFIED_IGNORED:")
            for w in (ignored.get("warnings") or [])))
        # Segment INSUFFICIENT alone is refillable PASS_WITH_DEGRADED (not UNCERTIFIED).
        insuf, insuf_code, _, _ = self.run_main([response(
            [judgment(match_level="INSUFFICIENT_EVIDENCE", visual_ok=False, confidence=0.5)],
            overall="PASS_WITH_DEGRADED")])
        self.assertEqual((insuf["status"], insuf_code), ("PASS_WITH_DEGRADED", 0))
        self.assertEqual(contract.report_status(insuf), "PASS_WITH_DEGRADED")
        self.assertTrue(any(int(r.get("index") or 0) == 1 for r in (insuf.get("degraded_matches") or [])))


if __name__ == "__main__":
    unittest.main()
