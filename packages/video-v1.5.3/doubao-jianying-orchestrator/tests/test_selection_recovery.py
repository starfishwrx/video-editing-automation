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

    def test_inventory_length_failure_splits_once_and_preserves_indices(self):
        calls = []
        def call(prompt, path):
            calls.append(path.name)
            if path.name == "inventory.json":
                raise ValueError("AI_EDITOR_CALL_FAILED:vision request failed [OUTPUT_TRUNCATED]")
            index = 1 if "left" in path.name else 2
            return {"sentences": [copy.deepcopy(self.sentences[index - 1])], "display_pool_ids": []}
        for code in ("OUTPUT_TRUNCATED", "EMPTY_CONTENT"):
            calls.clear()
            def attempt(prompt, path):
                if path.name == "inventory.json":
                    calls.append(path.name)
                    raise ValueError("AI_EDITOR_CALL_FAILED: [" + code + "]")
                return call(prompt, path)
            with patch.object(editor, "_call", side_effect=attempt):
                result = editor._inventory(self.manifest, self.shots, self.root)
            self.assertEqual([row["index"] for row in result], [1, 2])
            self.assertEqual(calls, ["inventory.json", "inventory_left.json", "inventory_right.json"])
        with patch.object(editor, "_call", side_effect=ValueError("AI_EDITOR_CALL_FAILED: [OUTPUT_TRUNCATED]")) as call:
            with self.assertRaisesRegex(ValueError, "OUTPUT_TRUNCATED"):
                editor._inventory(self.manifest, self.shots, self.root)
            self.assertEqual(call.call_count, 2)

    def test_scoped_repair_does_not_leak_into_later_full_replan(self):
        plan = self.plan()
        proposals = [{"rows": copy.deepcopy(self.rows)},
                     {"rows": [copy.deepcopy(self.rows[1])]},
                     {"rows": copy.deepcopy(self.rows)}]
        failures = [editor.WindowFailure(2, "shot2", "EVIDENCE_OUT_OF_RANGE"),
                    ValueError("AI_EDITOR_PLAN_COVERAGE:missing=[1]"), {}]
        with patch.object(editor, "_call", side_effect=proposals) as call, \
                patch.object(editor, "_repair_selected_window", return_value=False), \
                patch.object(editor, "_assemble", side_effect=failures):
            editor._edit_plan(self.manifest, self.analysis, plan, self.root)
        self.assertTrue(plan["ok"])
        self.assertEqual([row["index"] for row in plan["rows"]], [1, 2])
        self.assertIn("只返回index=2", call.call_args_list[1].args[0])
        self.assertNotIn("只返回index=2", call.call_args_list[2].args[0])
        self.assertIn("重新输出完整rows", call.call_args_list[2].args[0])

    def test_repeated_window_repairs_later_sentence_only(self):
        self.assertEqual(editor.failed_index(ValueError("AI_EDITOR_REPEATED_SOURCE_WINDOW:1:2")), 2)
        plan = self.plan()
        with patch.object(editor, "_call", side_effect=[{"rows": copy.deepcopy(self.rows)},
                          {"rows": [copy.deepcopy(self.rows[1])]}]) as call, \
                patch.object(editor, "_repair_repeated_window", return_value=False), \
                patch.object(editor, "_assemble", side_effect=[ValueError("AI_EDITOR_REPEATED_SOURCE_WINDOW:1:2"), {}]):
            editor._edit_plan(self.manifest, self.analysis, plan, self.root)
        self.assertEqual(plan["rows"][0], self.rows[0])
        self.assertIn("只返回index=2", call.call_args_list[1].args[0])
        self.assertIn('"shot_id": "shot1"', call.call_args_list[1].args[0])
        self.assertIn('"shot_id": "shot2"', call.call_args_list[1].args[0])

    def test_repeated_window_uses_existing_pixel_reviewed_alternative(self):
        plan = self.plan()
        plan["rows"][1]["parts"][0]["shot_id"] = "shot1"
        duplicate_review = {**copy.deepcopy(self.reviews["2"][0]), "shot_id": "shot1",
                            "source_identity": contract.source_identity(self.shots[0]["video"])}
        plan["reviews"]["2"].insert(0, duplicate_review)
        before = copy.deepcopy(plan["rows"][0])
        with patch.object(editor, "_call", return_value={"rows": copy.deepcopy(plan["rows"])}) as call:
            editor._edit_plan(self.manifest, self.analysis, plan, self.root)
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["rows"][0], before)
        self.assertEqual(plan["rows"][1]["parts"][0]["shot_id"], "shot2")
        self.assertEqual(call.call_count, 1)
        self.assertTrue(editor._assemble(self.manifest, self.analysis, plan,
                          narration_timing=None, verify_sources=True)["ok"])

    def test_repeat_repair_expands_at_most_one_round_of_actual_reviews(self):
        plan = self.plan()
        plan["rows"][1]["parts"][0]["shot_id"] = "shot1"
        duplicate = {**copy.deepcopy(self.reviews["2"][0]), "shot_id": "shot1",
                     "source_identity": contract.source_identity(self.shots[0]["video"])}
        plan["reviews"]["2"] = [duplicate]
        plan["sentences"][1]["candidate_ids"] = ["shot1", "shot2"]
        expanded = set()
        with patch.object(editor, "_review_pool", return_value=([duplicate, self.reviews["2"][0]], {})) as review:
            fixed = editor._repair_repeated_window(self.manifest, self.analysis, plan, self.root, None, 2, expanded)
        self.assertTrue(fixed)
        self.assertEqual(plan["rows"][1]["parts"][0]["shot_id"], "shot2")
        self.assertEqual(review.call_count, 1)
        self.assertEqual(plan["duplicate_repair_reviews"][0]["new_candidates"], ["shot2"])

        plan["rows"][1]["parts"][0]["shot_id"] = "shot1"
        plan["reviews"]["2"] = [duplicate, {"shot_id": "shot2", "verdict": "uncertain"}]
        with patch.object(editor, "_review_pool") as review:
            self.assertFalse(editor._repair_repeated_window(self.manifest, self.analysis, plan, self.root, None, 2, expanded))
            review.assert_not_called()
        self.assertEqual(plan["rows"][1]["parts"][0]["shot_id"], "shot1")

    def test_conflict_details_do_not_fold_distinct_spans_into_one_pair(self):
        def segment(windows):
            return {"span_parts": [{"video": "same", "ai_selection": {"video": "same",
                "window_us": {"start": a * 1_000_000, "end": b * 1_000_000}}} for a, b in windows]}
        before = [segment([(0, 1)]), segment([(2, 3), (5, 6), (7, 8)]), segment([(.5, 2.5)])]
        after = [segment([(0, 1)]), segment([(2, 3), (5, 6), (7, 8)]), segment([(5.5, 7.5)])]
        # Error labels alone misleadingly look like a reduction from two pairs to one.
        self.assertLess(set(contract.sequence_errors_all(after)), set(contract.sequence_errors_all(before)))
        self.assertFalse(set(contract.sequence_conflict_details(after)) < set(contract.sequence_conflict_details(before)))
        self.assertEqual(len(contract.sequence_conflict_details(after)), 2)

    def test_multiple_duplicate_pairs_are_fixed_without_losing_full_gate(self):
        plan = self.plan(); manifest = copy.deepcopy(self.manifest); analysis = copy.deepcopy(self.analysis)
        plan["rows"][1]["parts"][0]["shot_id"] = "shot1"
        plan["reviews"]["2"].insert(0, {**copy.deepcopy(self.reviews["2"][0]), "shot_id": "shot1",
            "source_identity": contract.source_identity(self.shots[0]["video"])})
        for index in (3, 4):
            video = self.root / f"source{index}.mp4"; video.write_bytes(b"synthetic source")
            sid = f"shot{index}"
            analysis["shots"].append({"shot_id": sid, "video": str(video), "source_start": 0,
                "source_end": 10, "duration": 10, "status": "ready_for_matching"})
            text = f"测试卖点{index}"
            manifest["segments"].append({"text": text, "audio_duration_us": 1_000_000})
            plan["sentences"].append({"index": index, "text": text, "kind": "selling_point",
                "selling_points": [{"id": "p1", "text": text}], "candidate_ids": [sid], "scene_ids": []})
            plan["reviews"][str(index)] = [{**copy.deepcopy(self.reviews["2"][0]), "shot_id": sid,
                "source_identity": contract.source_identity(video)}]
            plan["rows"].append({"index": index, "parts": [{"shot_id": sid, "audio_duration_us": 1_000_000,
                                                             "source_start_us": 2_000_000}]})
        plan["rows"][3]["parts"][0]["shot_id"] = "shot3"
        plan["reviews"]["4"].insert(0, {**copy.deepcopy(plan["reviews"]["4"][0]), "shot_id": "shot3",
            "source_identity": contract.source_identity(analysis["shots"][2]["video"])})
        with patch.object(editor, "_call", return_value={"rows": copy.deepcopy(plan["rows"])}) as call:
            editor._edit_plan(manifest, analysis, plan, self.root)
        self.assertTrue(plan["ok"])
        self.assertEqual(plan["rows"][1]["parts"][0]["shot_id"], "shot2")
        self.assertEqual(plan["rows"][3]["parts"][0]["shot_id"], "shot4")
        self.assertEqual(call.call_count, 1)
        self.assertTrue(editor._assemble(manifest, analysis, plan, narration_timing=None, verify_sources=True)["ok"])

    def test_duplicate_expansion_excludes_historical_transient_failures_from_new_budget(self):
        plan = self.plan()
        plan["rows"][1]["parts"][0]["shot_id"] = "shot1"
        duplicate = {**copy.deepcopy(self.reviews["2"][0]), "shot_id": "shot1",
                     "source_identity": contract.source_identity(self.shots[0]["video"])}
        timeout = {"shot_id": "shot2", "verdict": "uncertain", "error": "AI_EDITOR_CALL_FAILED: [REQUEST_TIMEOUT]"}
        plan["reviews"]["2"] = [duplicate, timeout]
        plan["sentences"][1]["candidate_ids"] = ["shot1", "shot2"] + [f"extra{i}" for i in range(12)]
        with patch.object(editor, "_review_pool", return_value=([duplicate], {})) as review:
            self.assertFalse(editor._repair_repeated_window(self.manifest, self.analysis, plan, self.root, None, 2, set()))
        sentence = review.call_args.args[1]
        self.assertNotIn("shot2", sentence["candidate_ids"])
        self.assertEqual(len(plan["duplicate_repair_reviews"][0]["new_candidates"]), 8)
        self.assertNotIn(timeout, review.call_args.kwargs["existing_reviews"])

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
                                   ("AI_EDITOR_CALL_FAILED:HTTP 401 unauthorized; request timeout", False),
                                   ("AI_EDITOR_CALL_FAILED: [EMPTY_CONTENT]", True)):
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
