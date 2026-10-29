"""Synthetic main-consumer tests; no model calls or real media inspection."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
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
    def run_main(self, responses, *, claims=1, batch_size=4, write_sheet=None, passes=1,
                 claim_text="揉搓也不掉絮"):
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
                {"text": claim_text, "temporary_shot_id": str(index)} for index in range(claims)]}), encoding="utf-8")
            def output_for_batch(vision, sheet, prompt, output, **kwargs):
                if callable(responses):
                    return responses(vision, sheet, prompt, output, **kwargs)
                batch_no = int(output.stem.rsplit("_", 1)[1])
                return True, json.dumps(responses[batch_no - 1])
            argv = ["qc", "--draft", str(draft), "--report-dir", str(report), "--output", str(root / "qc.json")]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, {"JY_POST_QC_BATCH_CLAIMS": str(batch_size),
                        "JY_SLOT_DIR": str(root / "slots"), "JY_VISION_BATCH_WORKERS": "20"}), \
                    mock.patch.object(qc, "_ffmpeg", return_value="synthetic-ffmpeg"), \
                    mock.patch.object(qc, "_extract", return_value=(True, "")) as extract, \
                    mock.patch.object(vision_analyzer, "_write_sheet", side_effect=write_sheet or (lambda *a, **k: True)), \
                    mock.patch.object(qc, "_call_qc_model", side_effect=output_for_batch) as model, \
                    contextlib.redirect_stdout(io.StringIO()):
                for _ in range(passes):
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

    def test_partial_success_resumes_only_missing_qc_batch(self):
        attempts = {}
        def model(vision, sheet, prompt, output, **kwargs):
            number = int(output.stem.rsplit("_", 1)[1])
            attempts[number] = attempts.get(number, 0) + 1
            if number == 2 and attempts[number] == 1:
                return False, "QC_STAGE_DEADLINE_EXCEEDED"
            return True, json.dumps(response())
        result, code, _, calls = self.run_main(model, claims=2, batch_size=1, passes=2)
        self.assertEqual(attempts, {1: 1, 2: 2})
        self.assertEqual((result["status"], code), ("PASS", 0))
        self.assertEqual(len(calls), 3)

    def test_request_validator_accepts_parsed_qc_and_rejects_incomplete_contract(self):
        vision = mock.Mock()
        def call(*args, **kwargs):
            kwargs["validator"](response())
            with self.assertRaises(ValueError):
                kwargs["validator"]({"overall": "PASS"})
            return True, json.dumps(response())
        vision._run_vision_with_retry.side_effect = call
        ok, _ = qc._call_qc_model(vision, Path("sheet"), "prompt", Path("out"),
                                  timeout=360, workdir=Path("."), expected_count=1)
        self.assertTrue(ok)
        self.assertEqual(vision._run_vision_with_retry.call_count, 1)

    def test_resumed_frames_reuse_only_the_same_source_and_time(self):
        with tempfile.TemporaryDirectory() as raw:
            source, frame = Path(raw) / "source", Path(raw) / "frame.jpg"
            source.write_bytes(b"synthetic-source")
            def extract(*args):
                frame.write_bytes(b"synthetic-frame")
                return True, ""
            with mock.patch.object(qc, "_extract", side_effect=extract) as decode:
                self.assertTrue(qc._resume_frame("ffmpeg", source, 2, frame)[0])
                self.assertTrue(qc._resume_frame("ffmpeg", source, 2, frame)[0])
                self.assertEqual(decode.call_count, 1)
                source.write_bytes(b"changed-source")
                self.assertTrue(qc._resume_frame("ffmpeg", source, 2, frame)[0])
                self.assertTrue(qc._resume_frame("ffmpeg", source, 3, frame)[0])
                self.assertEqual(decode.call_count, 3)

    def test_positive_small_confidence_is_not_an_invented_quality_threshold(self):
        result, _, _, _ = self.run_main([response([judgment(confidence=0.01)])])
        self.assertEqual(result["status"], "PASS")

    def test_qc_cannot_certify_face_cleaning_from_its_own_hand_observation(self):
        row = judgment(observed="手拿棉柔巾擦拭另一只手的手背和手腕", reason="虽然是擦手但可代表擦脸")
        result, _, _, _ = self.run_main([response([row])], claim_text="平时用来洗脸擦脸")
        self.assertNotEqual(result["status"], "PASS")
        self.assertEqual(result["model_review"][0]["match_level"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(result["model_review"][0]["model_match_level"], "DIRECT")
        self.assertTrue(any("POST_DRAFT_VISUAL_INSUFFICIENT:S01" in x for x in result["warnings"]))
        result, _, _, _ = self.run_main([response([row])], claim_text="洗脸巾擦手也好用")
        self.assertEqual(result["status"], "PASS")
        result, _, _, _ = self.run_main([response([row])], claim_text="不用洗脸，只擦手就好")
        self.assertEqual(result["status"], "PASS")
        result, _, _, _ = self.run_main([response([row])], claim_text="不用洗脸，但也能擦脸")
        self.assertNotEqual(result["status"], "PASS")

    def test_qc_cannot_ignore_an_explicitly_unverified_odor_claim(self):
        row = judgment(observed="双手拉扯湿润的纸巾", reason="虽然没有异味无法直接通过视觉呈现，但不容易破得到了验证")
        result, _, _, _ = self.run_main([response([row])], claim_text="没有异味也不容易破")
        self.assertNotEqual(result["status"], "PASS")
        self.assertEqual(result["model_review"][0]["match_level"], "INSUFFICIENT_EVIDENCE")
        row = judgment(observed="包装清晰标注无异味", reason="无异味标识可见")
        result, _, _, _ = self.run_main([response([row])], claim_text="没有异味")
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
        self.assert_blocked({"segments": [], "overall": "FAIL"}, status="FAIL")

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

    def test_parallel_qc_starts_after_preparation_and_merges_by_claim_index(self):
        prepared = set()
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        state = {"active": 0, "peak": 0}

        def sheet(items, output, **kwargs):
            with lock:
                prepared.add(output.name)
            return True

        def model(vision, sheet, prompt, output, **kwargs):
            self.assertEqual(len(prepared), 4)  # Overall evidence plus all three batch sheets.
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            barrier.wait(timeout=10)
            if output.stem.endswith("01"):
                time.sleep(0.03)
            with lock:
                state["active"] -= 1
            return True, json.dumps(response())

        result, code, _, _ = self.run_main(model, claims=3, batch_size=1, write_sheet=sheet)
        self.assertEqual(state["peak"], 3)
        self.assertEqual((result["status"], code), ("PASS", 0))
        self.assertEqual([row["index"] for row in result["model_review"]], [1, 2, 3])
        self.assertEqual(result["response_contract"]["validated_batches"], [1, 2, 3])

    def test_failed_qc_sheet_is_never_sent_and_blocks_release(self):
        def sheet(items, output, **kwargs):
            return not output.stem.endswith("_02")

        result, code, _, calls = self.run_main([response(), response(), response()],
            claims=3, batch_size=1, write_sheet=sheet)
        self.assertEqual(len(calls), 2)
        self.assertEqual((result["status"], code), ("UNCERTIFIED", 2))
        self.assertEqual(result["release_eligibility"], "BLOCKED")
        self.assertIn("CONTACT_SHEET_FAILED:B02", result["warnings"])

    def test_valid_degraded_and_insufficient_keep_their_original_limits(self):
        body = response([judgment(match_level="ACCEPTABLE_DEGRADED")], overall="PASS_WITH_DEGRADED")
        result, code, _, _ = self.run_main([body])
        self.assertEqual((result["status"], code, contract.report_status(result)),
                         ("PASS_WITH_DEGRADED", 0, "PASS_WITH_DEGRADED"))
        self.assert_blocked(response([judgment(match_level="INSUFFICIENT_EVIDENCE", visual_ok=False,
                                             confidence=0)], overall="UNCERTIFIED"))


if __name__ == "__main__":
    unittest.main()
