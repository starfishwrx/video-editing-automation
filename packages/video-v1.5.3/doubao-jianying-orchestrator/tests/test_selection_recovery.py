"""Directed selection repair, cache reuse and failure routing, without API calls."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import ai_editor_contract as contract, visual_qc_contract
from orchestrator.application import cli
from orchestrator.selection import ai_editor as editor
from orchestrator.runtime.task_log import result_contract


class SelectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manifest = {"category": "weipin", "selection_mode": "ai_editor", "head_trim_s": 0,
            "tail_trim_s": 0, "tail_pad_s": 0, "segments": [
                {"text": "柔软的表层", "audio_duration_us": 1_000_000},
                {"text": "没几下就吸收完了", "audio_duration_us": 1_000_000}]}
        self.shots, self.sentences, self.reviews, self.rows = [], [], {}, []
        for i, text in enumerate(self.manifest["segments"], 1):
            video = self.root / f"source{i}.mp4"
            video.write_bytes(b"synthetic source")
            sid = f"shot{i}"
            self.shots.append({"shot_id": sid, "video": str(video), "source_start": 0,
                "source_end": 10, "duration": 10, "status": "ready_for_matching"})
            self.sentences.append({"index": i, "text": text["text"], "kind": "selling_point",
                "selling_points": [{"id": "p1", "text": text["text"]}], "candidate_ids": [sid], "scene_ids": []})
            self.reviews[str(i)] = [{"shot_id": sid, "verdict": "matched", "same_product": True,
                "basis": "visible_result", "observation": "合成夹具的可见特点", "reason": "对应当前具体卖点",
                "confidence": .9, "supported_point_ids": ["p1"], "time_sensitive": False,
                "scene_ids": [], "source_identity": contract.source_identity(video),
                "evidence_intervals_us": [{"start": 2_000_000, "end": 2_500_000}]}]
            self.rows.append({"index": i, "parts": [{"shot_id": sid, "audio_duration_us": 1_000_000,
                                                    "source_start_us": 2_000_000}]})
        self.analysis = {"shots": self.shots}

    def plan(self):
        return {"schema": contract.PLAN_SCHEMA, "policy_version": contract.POLICY_VERSION,
            "sentences": copy.deepcopy(self.sentences), "reviews": copy.deepcopy(self.reviews),
            "display_reviews": [], "rows": copy.deepcopy(self.rows), "coverage": {},
            "identity": {"model": "offline", "manifest_sha256": contract.digest(self.manifest),
                "timing_sha256": contract.digest(None), "analysis_sha256": contract.digest(self.analysis),
                "editorial_rules": contract.rules_identity(self.manifest)}}

    def broken_window(self):
        plan = self.plan()
        plan["reviews"]["2"][0]["evidence_intervals_us"] = [{"start": 1_000_000, "end": 8_000_000}]
        plan["rows"][1]["parts"][0]["source_start_us"] = 6_000_000
        return plan

    def test_invalid_inapplicable_taxonomy_is_recorded_without_granting_support(self):
        model = {"sentences": [{**s, "scene_ids": ["absorb_rewet"]} for s in self.sentences], "display_pool_ids": []}
        with patch.object(editor, "_call", return_value=copy.deepcopy(model)):
            result = editor._inventory(self.manifest, self.shots, self.root)
        self.assertEqual(result[0]["scene_ids"], [])
        self.assertEqual(result[0]["inventory_ignored_scene_ids"], ["absorb_rewet"])
        with patch.object(editor, "_call", return_value=model), self.assertRaisesRegex(ValueError, "SCENE_INVALID"):
            editor._inventory({**self.manifest, "category": "zhipin"}, self.shots, self.root)

    def test_requested_small_window_is_reobserved_and_other_sentence_is_unchanged(self):
        plan = self.broken_window()
        preserved = copy.deepcopy(plan["rows"][0])
        reviewed = {**self.reviews["2"][0], "review_window_us": {"start": 6_000_000, "end": 8_000_000},
                    "evidence_intervals_us": [{"start": 6_100_000, "end": 7_000_000}]}
        with patch.object(editor, "_call", return_value={"rows": copy.deepcopy(plan["rows"])}) as edit, \
                patch.object(editor, "_review", return_value=reviewed) as review:
            editor._edit_plan(self.manifest, self.analysis, plan, self.root)
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["rows"][0], preserved)
        self.assertEqual(edit.call_count, 1)
        self.assertEqual(review.call_count, 1)
        self.assertEqual((review.call_args.args[2]["source_start"], review.call_args.args[2]["source_end"]), (6, 8))
        output = editor._assemble(self.manifest, self.analysis, plan, narration_timing=None, verify_sources=True)
        receipt = output["segments"][1]["ai_selection"]
        self.assertEqual(receipt["evidence_intervals_us"], reviewed["evidence_intervals_us"])
        self.assertTrue(contract.validate_segment_receipt(output["segments"][1], index=2, manifest=self.manifest)["ok"])

    def test_rejected_window_is_not_clipped_into_evidence_or_retried_forever(self):
        plan = self.broken_window()
        original = copy.deepcopy(plan["rows"])
        def proposal(*args):
            rows = copy.deepcopy(original)
            rows[0]["parts"][0]["source_start_us"] = 9_000_000 if len(plan.get("repair_history", [])) else 2_000_000
            return {"rows": rows}
        with patch.object(editor, "_call", side_effect=proposal) as edit, \
                patch.object(editor, "_review", return_value={"verdict": "mismatch", "reason": "没拍到结果"}) as review, \
                self.assertRaisesRegex(ValueError, "EVIDENCE_OUT_OF_RANGE"):
            editor._edit_plan(self.manifest, self.analysis, plan, self.root)
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["rows"][0], original[0])
        self.assertEqual(edit.call_count, 3)
        self.assertEqual(review.call_count, 1)
        self.assertFalse(plan.get("window_reviews"))

    def test_transient_resume_reuses_successful_sentence_and_invalidates_changed_input(self):
        coverage = {"feasible": True, "exhausted": True, "uncertain": 0, "checked": 1, "total": 1}
        def first(manifest, sentence, *args, **kwargs):
            if sentence["index"] == 2:
                raise ValueError("AI_EDITOR_CALL_FAILED:vision request failed [DEADLINE_EXCEEDED]")
            return self.reviews["1"], coverage
        with patch.object(editor, "_editor_model", return_value="offline"), \
                patch.object(editor, "_inventory", return_value=copy.deepcopy(self.sentences)), \
                patch.object(editor, "_review_pool", side_effect=first):
            failed = editor.prepare(self.manifest, self.analysis, self.root)
        self.assertTrue(failed["ai_editor_plan"]["failure"]["retryable"])
        resumed = {**self.manifest, "draft_name": "new-recovery", "parent_run_id": "new-parent"}
        with patch.object(editor, "_editor_model", return_value="offline"), \
                patch.object(editor, "_inventory") as inventory, \
                patch.object(editor, "_review_pool", return_value=(self.reviews["2"], coverage)) as pool, \
                patch.object(editor, "_call", return_value={"rows": copy.deepcopy(self.rows)}):
            result = editor.prepare(resumed, self.analysis, self.root)
        self.assertTrue(result["ai_editor_plan"]["ok"], result["ai_editor_plan"].get("issues"))
        inventory.assert_not_called()
        self.assertEqual([call.args[1]["index"] for call in pool.call_args_list], [2])
        changed = copy.deepcopy(resumed)
        changed["segments"][0]["text"] = "新文案不能借用旧观察"
        with patch.object(editor, "_editor_model", return_value="offline"), \
                patch.object(editor, "_inventory", side_effect=ValueError("stop before model")) as inventory:
            editor.prepare(changed, self.analysis, self.root)
        inventory.assert_called_once()

    def test_provider_cause_survives_match_cli_and_task_contract(self):
        input_path = self.root / "input.json"
        input_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        for message, retryable in (("AI_EDITOR_CALL_FAILED:vision request failed [DEADLINE_EXCEEDED]", True),
                                   ("AI_EDITOR_CALL_FAILED:HTTP 401 unauthorized; request timeout", False)):
            with self.subTest(message=message), patch.object(editor, "_editor_model", return_value="offline"), \
                    patch.object(editor, "_inventory", side_effect=ValueError(message)):
                analysis = editor.prepare(self.manifest, self.analysis, self.root)
            with patch.object(cli, "_run_shot_analysis", return_value=self.analysis), \
                    patch("orchestrator.vision.material_corrections.apply", side_effect=lambda m, a, r: a), \
                    patch.object(editor, "prepare", return_value=analysis):
                _, blocker = cli._temporary_match(self.manifest, input_path, self.root)
            result = result_contract(blocker, task_id="offline", run_id="offline", task_log="offline")
            self.assertEqual(result["status"], "VISION_CALL_FAILED")
            self.assertEqual(result["retryable"], retryable)
            self.assertEqual(result["resume_stage"], "semantic_match")
            self.assertEqual(result["next_action"], "retry_stage" if retryable else "manual_review")

    def test_current_qc_protocol_keeps_confidence_floor_and_rejects_old_response(self):
        model = {"response_version": visual_qc_contract.AI_RESPONSE_VERSION, "overall": "PASS", "segments": [
            {"index": 1, "match_level": "DIRECT", "visual_ok": True, "observed": "合成可见事实",
             "reason": "支持其中一个具体点，另一点未确认", "confidence": .1}]}
        self.assertTrue(visual_qc_contract.response_errors(model, 1, expected_version=visual_qc_contract.AI_RESPONSE_VERSION))
        model["segments"][0]["confidence"] = .9
        self.assertEqual(visual_qc_contract.response_errors(model, 1, expected_version=visual_qc_contract.AI_RESPONSE_VERSION), [])
        model["response_version"] = "chatcut-visual-qc-response-v4"
        self.assertIn("RESPONSE_VERSION_UNSUPPORTED", visual_qc_contract.response_errors(
            model, 1, expected_version=visual_qc_contract.AI_RESPONSE_VERSION))
