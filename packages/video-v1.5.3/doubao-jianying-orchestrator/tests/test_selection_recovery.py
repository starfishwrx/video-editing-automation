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

    def test_matched_infeasible_evidence_stops_before_full_pool(self):
        sentence = copy.deepcopy(self.sentences[0])
        shots = {}
        for i in range(24):
            sid = f'candidate{i}'
            shots[sid] = {**self.shots[0], 'shot_id': sid}
        sentence['candidate_ids'] = list(shots)
        def reviewed(_manifest, _sentence, shot, _directory, **kwargs):
            return {**copy.deepcopy(self.reviews['1'][0]), 'shot_id': shot['shot_id'],
                    'evidence_intervals_us': [{'start': 1_000_000, 'end': 8_000_000}]}
        def serial(ids, _shots, _directory, call, **kwargs):
            return ((sid, call(sid)) for sid in ids)
        with patch.object(editor, '_review', side_effect=reviewed), patch.object(editor, '_iter_review_round', side_effect=serial):
            reviews, coverage = editor._review_pool(self.manifest, sentence, shots, self.root, {}, early_window_repair=True)
            self.assertEqual((len(reviews), coverage['checked'], coverage['total']), (8, 8, 24))
            self.assertFalse(coverage['exhausted'])
            self.assertFalse(coverage['feasible'])
            self.assertTrue(coverage['evidence_found'])
            self.assertEqual(coverage['reason'], 'KNOWN_WINDOWS_INFEASIBLE')
            full, full_coverage = editor._review_pool(self.manifest, sentence, shots, self.root, {},
                                                      exhaustive=True, existing_reviews=reviews)
        self.assertEqual(len(full), 24)
        self.assertTrue(full_coverage['exhausted'])
        self.assertFalse(full_coverage['feasible'])

    def test_placement_failure_exhausts_only_failed_sentence_then_retries(self):
        plan = self.plan()
        plan['coverage'] = {'1': {'reason': 'KNOWN_WINDOWS_INFEASIBLE', 'exhausted': False,
                                  'evidence_found': True},
                            '2': {'reason': 'READY', 'exhausted': False}}
        failure = editor.WindowFailure(1, 'shot1', 'EVIDENCE_SPAN_TOO_LONG')
        full = {'checked': 24, 'total': 24, 'exhausted': True, 'feasible': True, 'reason': 'READY',
                'evidence_found': True}
        before = copy.deepcopy(plan['reviews']['2'])
        with patch.object(editor, '_edit_plan', side_effect=[failure, None]) as edit, \
             patch.object(editor, '_review_pool', return_value=(copy.deepcopy(self.reviews['1']), full)) as review:
            editor._edit_with_review_fallback(self.manifest, self.analysis, plan, self.root, None)
        self.assertEqual(edit.call_count, 2)
        self.assertEqual(review.call_count, 1)
        self.assertTrue(review.call_args.kwargs['exhaustive'])
        self.assertEqual(review.call_args.args[1]['index'], 1)
        self.assertEqual(plan['reviews']['2'], before)
        self.assertEqual(plan['review_expansions'][0]['index'], 1)
        self.assertTrue(plan['_resume_rows'])

    def test_mismatches_keep_searching_until_feasible_match(self):
        sentence = copy.deepcopy(self.sentences[0])
        shots = {f'candidate{i}': {**self.shots[0], 'shot_id': f'candidate{i}'} for i in range(24)}
        sentence['candidate_ids'] = list(shots)
        def reviewed(_manifest, _sentence, shot, _directory, **kwargs):
            if shot['shot_id'] == 'candidate15':
                return {'shot_id': shot['shot_id'], 'verdict': 'uncertain',
                        'error': 'AI_EDITOR_CALL_FAILED:REQUEST_TIMEOUT'}
            if shot['shot_id'] == 'candidate16':
                return {**copy.deepcopy(self.reviews['1'][0]), 'shot_id': shot['shot_id']}
            return {'shot_id': shot['shot_id'], 'verdict': 'mismatch', 'reason': 'not the selling point'}
        def serial(ids, _shots, _directory, call, **kwargs):
            return ((sid, call(sid)) for sid in ids)
        with patch.object(editor, '_review', side_effect=reviewed), \
             patch.object(editor, '_iter_review_round', side_effect=serial):
            reviews, coverage = editor._review_pool(self.manifest, sentence, shots, self.root, {}, early_window_repair=True)
        self.assertTrue(coverage['feasible'])
        self.assertTrue(coverage['evidence_found'])
        self.assertIn('candidate16', {r['shot_id'] for r in reviews})
        self.assertEqual(coverage['checked'], 24)

    def test_placement_failure_remains_failure_after_full_pool(self):
        plan = self.plan()
        plan['coverage'] = {'1': {'reason': 'KNOWN_WINDOWS_INFEASIBLE', 'exhausted': False,
                                  'evidence_found': True}}
        failure = editor.WindowFailure(1, 'shot1', 'EVIDENCE_SPAN_TOO_LONG')
        full = {'checked': 24, 'total': 24, 'exhausted': True, 'feasible': False,
                'reason': 'KNOWN_WINDOWS_INFEASIBLE', 'evidence_found': True}
        with patch.object(editor, '_edit_plan', side_effect=[failure, failure]), \
             patch.object(editor, '_review_pool', return_value=(copy.deepcopy(self.reviews['1']), full)) as review:
            with self.assertRaises(editor.WindowFailure):
                editor._edit_with_review_fallback(self.manifest, self.analysis, plan, self.root, None)
        self.assertEqual(review.call_count, 1)

    def test_long_matched_evidence_seeds_placement_without_more_reviews(self):
        plan = self.plan()
        plan['rows'][0] = {'index': 1, 'parts': [], 'reason': 'no short window yet'}
        plan['reviews']['1'][0]['evidence_intervals_us'] = [{'start': 1_000_000, 'end': 8_000_000}]
        failure = ValueError('AI_EDITOR_NO_SELECTION:1')
        self.assertTrue(editor._retrieve_empty_selection(
            self.manifest, self.analysis, plan, failure, None))
        self.assertEqual(plan['rows'][0]['parts'][0]['shot_id'], 'shot1')
        self.assertIn('window or continuity repair', plan['rows'][0]['reason'])

    def test_out_of_range_matched_evidence_seeds_guarded_start_for_placement(self):
        plan = self.plan()
        plan['rows'][0] = {'index': 1, 'parts': [], 'reason': 'no short window yet'}
        self.manifest['head_trim_s'] = 0.3
        plan['reviews']['1'][0]['evidence_intervals_us'] = [{'start': 0, 'end': 2_133_333}]
        self.analysis['shots'][0].update(source_end=2.133333, duration=2.133333,
                                         material_duration_us=2_133_333)
        failure = ValueError('AI_EDITOR_NO_SELECTION:1')
        self.assertTrue(editor._retrieve_empty_selection(
            self.manifest, self.analysis, plan, failure, None))
        part = plan['rows'][0]['parts'][0]
        self.assertEqual(part['shot_id'], 'shot1')
        self.assertEqual(part['source_start_us'], 300_000)

    def test_empty_out_of_range_places_before_earlier_span_repair(self):
        plan = self.plan()
        self.manifest['head_trim_s'] = 0.3
        plan['reviews']['1'][0]['evidence_intervals_us'] = [{'start': 1_000_000, 'end': 8_000_000}]
        plan['rows'][0]['parts'][0]['source_start_us'] = 1_000_000
        plan['rows'][1] = {'index': 2, 'parts': [], 'reason': 'model left empty for out-of-range'}
        plan['reviews']['2'][0]['evidence_intervals_us'] = [{'start': 0, 'end': 2_133_333}]
        self.analysis['shots'][1].update(source_end=2.133333, duration=2.133333,
                                         material_duration_us=2_133_333)
        placed = {**copy.deepcopy(plan['reviews']['2'][0]), 'placement_review': True,
                  'placement_verdict': 'suitable', 'verdict': 'matched',
                  'review_window_us': {'start': 300_000, 'end': 2_133_333},
                  'evidence_intervals_us': [{'start': 400_000, 'end': 1_200_000}]}
        order = []

        def place(manifest, sentence, shot, directory, parent=None):
            order.append(sentence['index'])
            return {**placed, 'shot_id': shot['shot_id'],
                    'review_window_us': {'start': round(shot['source_start'] * 1e6),
                                        'end': round(shot['source_end'] * 1e6)},
                    'evidence_intervals_us': [
                        {'start': round(shot['source_start'] * 1e6) + 100_000,
                         'end': round(shot['source_start'] * 1e6) + 900_000}]}

        with patch.object(editor, '_review_placement_window', side_effect=place) as review:
            self.assertTrue(editor._place_empty_matched_evidence(
                self.manifest, self.analysis, plan, self.root, None))
        self.assertEqual(order, [2])
        self.assertEqual(review.call_count, 1)
        self.assertEqual(plan['rows'][1]['parts'][0]['shot_id'], 'shot2')
        self.assertIn('window_review_key', plan['rows'][1]['parts'][0])

    def test_retryable_failed_placement_attempt_can_rerun(self):
        plan = self.plan()
        shot = self.analysis['shots'][0]
        window = (2_000_000, 4_000_000)
        key = contract.digest([
            'placement', plan['sentences'][0], shot['shot_id'],
            contract.source_identity(shot['video']), window,
            plan['reviews']['1'][0].get('evidence_intervals_us')])[:24]
        plan['window_repair_attempts'] = {
            key: {'index': 1, 'shot_id': 'shot1',
                  'window_us': {'start': window[0], 'end': window[1]},
                  'status': 'failed',
                  'reason': 'AI_EDITOR_CALL_FAILED:vision request failed [DEADLINE_EXCEEDED]',
                  'mode': 'placement'}}
        reviewed = {**copy.deepcopy(plan['reviews']['1'][0]), 'placement_review': True,
                    'placement_verdict': 'suitable', 'verdict': 'matched',
                    'review_window_us': {'start': window[0], 'end': window[1]},
                    'evidence_intervals_us': [{'start': 2_100_000, 'end': 3_000_000}]}
        with patch.object(editor, '_review_placement_window', return_value=reviewed) as review:
            got = editor._review_proposed_window(
                self.manifest, plan, self.root, plan['sentences'][0], shot, window)
        self.assertEqual(got, key)
        self.assertEqual(review.call_count, 1)
        self.assertEqual(plan['window_repair_attempts'][key]['status'], 'reviewed')

    def test_invalid_inapplicable_taxonomy_is_recorded_without_granting_support(self):
        model = {"sentences": [{**s, "scene_ids": ["absorb_rewet"]} for s in self.sentences], "display_pool_ids": []}
        with patch.object(editor, "_call", return_value=copy.deepcopy(model)):
            result = editor._inventory(self.manifest, self.shots, self.root)
        self.assertEqual(result[0]["scene_ids"], [])
        self.assertEqual(result[0]["inventory_ignored_scene_ids"], ["absorb_rewet"])
        with patch.object(editor, "_call", return_value=model), self.assertRaisesRegex(ValueError, "SCENE_INVALID"):
            editor._inventory({**self.manifest, "category": "zhipin"}, self.shots, self.root)

    def test_suitable_short_window_speeds_up_inside_two_x_when_evidence_is_longer(self):
        plan = self.plan()
        review = {**copy.deepcopy(self.reviews["1"][0]), "placement_review": True,
                  "placement_verdict": "suitable", "sentence_index": 1,
                  "review_window_us": {"start": 2_000_000, "end": 4_000_000},
                  "evidence_intervals_us": [{"start": 2_000_000, "end": 3_500_000}]}
        key = "placement-fit"
        plan["window_reviews"] = {key: review}
        plan["rows"][0]["parts"][0].update(window_review_key=key, source_start_us=2_000_000)
        output = editor._assemble(self.manifest, self.analysis, plan, narration_timing=None, verify_sources=True)
        segment = output["segments"][0]
        self.assertGreater(segment["video_speed"], 1)
        self.assertLessEqual(segment["video_speed"], 2)
        self.assertEqual(segment["playback_observation"]["kind"], "placement_fit")
        self.assertTrue(contract.validate_segment_receipt(segment, index=1, manifest=self.manifest)["ok"])
        too_long = {**review, "evidence_intervals_us": [{"start": 2_000_000, "end": 4_600_000}],
                    "review_window_us": {"start": 2_000_000, "end": 5_000_000}}
        plan["window_reviews"] = {key: too_long}
        with self.assertRaises(editor.WindowFailure) as caught:
            editor._assemble(self.manifest, self.analysis, plan, narration_timing=None, verify_sources=True)
        self.assertEqual(caught.exception.reason, "EVIDENCE_SPAN_TOO_LONG")

    def test_requested_small_window_is_reobserved_and_other_sentence_is_unchanged(self):
        plan = self.broken_window()
        preserved = copy.deepcopy(plan["rows"][0])
        reviewed = {**self.reviews["2"][0], "placement_review": True,
                    "review_window_us": {"start": 6_000_000, "end": 8_000_000},
                    "evidence_intervals_us": [{"start": 6_100_000, "end": 7_000_000}]}
        with patch.object(editor, "_call", return_value={"rows": copy.deepcopy(plan["rows"])}) as edit, \
                patch.object(editor, "_review_placement_window", return_value=reviewed) as review:
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
                patch.object(editor, "_review_placement_window",
                             return_value={"verdict": "mismatch", "reason": "没拍到完整结果",
                                           "same_product": True, "supported_point_ids": ["p1"],
                                           "source_identity": plan["reviews"]["2"][0]["source_identity"],
                                           "review_window_us": {"start": 0, "end": 1}}) as review, \
                self.assertRaisesRegex(ValueError, "EVIDENCE_OUT_OF_RANGE"):
            editor._edit_plan(self.manifest, self.analysis, plan, self.root)
        self.assertFalse(plan["ok"])
        self.assertEqual(plan["rows"][0], original[0])
        self.assertEqual(edit.call_count, 3)
        self.assertEqual(review.call_count, 1)
        self.assertFalse(plan.get("window_reviews"))

    def test_rounded_source_end_gets_fresh_review_before_inpoint_moves(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["segments"][0]["audio_duration_us"] = 2_139_479
        analysis, plan = copy.deepcopy(self.analysis), self.plan()
        analysis["shots"][0].update(source_end=4.167, material_duration_us=4_166_667)
        part = plan["rows"][0]["parts"][0]
        part.update(source_start_us=2_027_521, audio_duration_us=2_139_479)
        old_review = plan["reviews"]["1"][0]
        old_review["evidence_intervals_us"] = [{"start": 2_358_000, "end": 4_167_000}]
        saved_review, saved_other = copy.deepcopy(old_review), copy.deepcopy(plan["rows"][1])
        reviewed = {**copy.deepcopy(old_review), "placement_review": True,
                    "review_window_us": {"start": 2_027_188, "end": 4_166_667},
                    "evidence_intervals_us": [{"start": 2_200_000, "end": 3_900_000}]}
        failure = editor.WindowFailure(1, "shot1", "EVIDENCE_OUT_OF_RANGE")
        with patch.object(editor, "_review_placement_window", return_value=reviewed) as review:
            self.assertTrue(editor._repair_selected_window(manifest, analysis, plan, self.root, failure))
        self.assertEqual((review.call_args.args[2]["source_start"], review.call_args.args[2]["source_end"]),
                         (2.027188, 4.166667))
        self.assertIs(review.call_args.kwargs["parent"], plan["reviews"]["1"][0])
        self.assertEqual(plan["reviews"]["1"][0], saved_review)
        self.assertEqual(plan["rows"][1], saved_other)
        self.assertEqual(part["source_start_us"], 2_027_188)
        self.assertEqual(plan["window_repair_attempts"][part["window_review_key"]]["original_source_start_us"], 2_027_521)
        output = editor._assemble(manifest, analysis, plan, narration_timing=None, verify_sources=True)
        self.assertLessEqual(round(output["segments"][0]["source_end"] * 1e6), 4_166_667)
        self.assertEqual(output["segments"][0]["ai_selection"]["evidence_intervals_us"], reviewed["evidence_intervals_us"])

    def test_rejected_boundary_review_does_not_move_inpoint_or_change_evidence(self):
        analysis, plan = copy.deepcopy(self.analysis), self.plan()
        analysis["shots"][0].update(source_end=4.167, material_duration_us=4_166_667)
        part = plan["rows"][0]["parts"][0]
        part.update(source_start_us=2_027_521, audio_duration_us=2_139_479)
        plan["reviews"]["1"][0]["evidence_intervals_us"] = [{"start": 2_358_000, "end": 4_167_000}]
        old_review = copy.deepcopy(plan["reviews"]["1"][0])
        with patch.object(editor, "_review_placement_window",
                          return_value={"verdict": "mismatch", "reason": "切在动作中途", "same_product": True,
                                        "supported_point_ids": ["p1"], "source_identity": old_review["source_identity"],
                                        "review_window_us": {"start": 0, "end": 1}}):
            self.assertFalse(editor._repair_selected_window(self.manifest, analysis, plan, self.root,
                editor.WindowFailure(1, "shot1", "EVIDENCE_OUT_OF_RANGE")))
        self.assertEqual(part["source_start_us"], 2_027_521)
        self.assertEqual(plan["reviews"]["1"][0], old_review)
        self.assertFalse(plan.get("window_reviews"))

    def test_repair_targets_failing_part_when_one_shot_is_used_twice(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["segments"][0]["audio_duration_us"] = 2_000_000
        analysis, plan = copy.deepcopy(self.analysis), self.plan()
        analysis["shots"][0].update(source_end=4.167, material_duration_us=4_166_667)
        original = plan["reviews"]["1"][0]
        original["evidence_intervals_us"] = [{"start": 2_358_000, "end": 4_167_000}]
        first_review = {**copy.deepcopy(original), "sentence_index": 1,
            "review_window_us": {"start": 0, "end": 1_500_000},
            "evidence_intervals_us": [{"start": 200_000, "end": 800_000}]}
        plan["window_reviews"] = {"first": first_review}
        plan["rows"][0]["parts"] = [
            {"shot_id": "shot1", "source_start_us": 0, "audio_duration_us": 1_000_000,
             "window_review_key": "first"},
            {"shot_id": "shot1", "source_start_us": 3_166_800, "audio_duration_us": 1_000_000}]
        first_part = copy.deepcopy(plan["rows"][0]["parts"][0])
        with self.assertRaises(editor.WindowFailure) as caught:
            editor._assemble(manifest, analysis, plan, narration_timing=None, verify_sources=True)
        self.assertEqual(caught.exception.part_index, 1)
        reviewed = {**copy.deepcopy(original), "placement_review": True,
                    "review_window_us": {"start": 3_166_667, "end": 4_166_667},
                    "evidence_intervals_us": [{"start": 3_300_000, "end": 4_000_000}]}
        with patch.object(editor, "_review_placement_window", return_value=reviewed):
            self.assertTrue(editor._repair_selected_window(manifest, analysis, plan, self.root, caught.exception))
        self.assertEqual(plan["rows"][0]["parts"][0], first_part)
        self.assertEqual(plan["window_reviews"]["first"], first_review)
        self.assertEqual(plan["rows"][0]["parts"][1]["source_start_us"], 3_166_667)
        self.assertFalse(editor._repair_selected_window(manifest, analysis, plan, self.root,
                         editor.WindowFailure(1, "shot1", "EVIDENCE_OUT_OF_RANGE")))

    def test_interrupted_window_review_reattaches_only_failing_part_without_model(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["segments"][0]["audio_duration_us"] = 2_000_000
        analysis, plan = copy.deepcopy(self.analysis), self.plan()
        shot = analysis["shots"][0]
        shot.update(source_end=4.167, material_duration_us=4_166_667)
        original = plan["reviews"]["1"][0]
        original["evidence_intervals_us"] = [{"start": 2_358_000, "end": 4_167_000}]
        first_review = {**copy.deepcopy(original), "sentence_index": 1,
            "review_window_us": {"start": 0, "end": 1_500_000},
            "evidence_intervals_us": [{"start": 200_000, "end": 800_000}]}
        plan["window_reviews"] = {"first": first_review}
        plan["rows"][0]["parts"] = [
            {"shot_id": "shot1", "source_start_us": 0, "audio_duration_us": 1_000_000,
             "window_review_key": "first"},
            {"shot_id": "shot1", "source_start_us": 3_166_800, "audio_duration_us": 1_000_000}]
        first_part = copy.deepcopy(plan["rows"][0]["parts"][0])
        window = (3_166_667, 4_166_667)
        reviewed = {**copy.deepcopy(original), "review_window_us": {"start": window[0], "end": window[1]},
                    "evidence_intervals_us": [{"start": 3_300_000, "end": 4_000_000}]}
        reviewed = {**reviewed, "placement_review": True}
        with patch.object(editor, "_review_placement_window", return_value=reviewed) as review:
            key = editor._review_proposed_window(manifest, plan, self.root, plan["sentences"][0], shot, window,
                                                  attempt_metadata={"original_source_start_us": 3_166_800})
        review.assert_called_once()
        interrupted = json.loads((self.root / "plan.json").read_text(encoding="utf-8"))
        self.assertNotIn("window_review_key", interrupted["rows"][0]["parts"][1])
        with patch.object(editor, "_review_placement_window", side_effect=AssertionError("repeat model call")):
            fixed = editor._repair_selected_window(manifest, analysis, interrupted, self.root,
                editor.WindowFailure(1, "shot1", "EVIDENCE_OUT_OF_RANGE", part_index=1))
        self.assertTrue(fixed)
        self.assertEqual(interrupted["rows"][0]["parts"][0], first_part)
        self.assertEqual(interrupted["rows"][0]["parts"][1]["window_review_key"], key)
        self.assertEqual(interrupted["rows"][0]["parts"][1]["source_start_us"], window[0])
        self.assertTrue(editor._assemble(manifest, analysis, interrupted, narration_timing=None, verify_sources=True)["ok"])
        interrupted["window_reviews"][key]["evidence_intervals_us"][0]["end"] = window[1] + 1
        with patch.object(editor, "_review_placement_window") as review:
            self.assertIsNone(editor._review_proposed_window(manifest, interrupted, self.root,
                              interrupted["sentences"][0], shot, window))
        review.assert_not_called()

    def test_duration_only_migration_reuses_rows_and_original_observations(self):
        old = copy.deepcopy(self.analysis)
        old["shots"][0]["material_duration_us"] = 10_000_000
        plan = self.plan()
        plan["identity"].update(analysis_sha256=contract.digest(old), policy_version=contract.POLICY_VERSION)
        plan["resume_identity"] = copy.deepcopy(plan["identity"])
        folder = self.root / "ai_editor"
        folder.mkdir()
        editor._save(folder / "plan.json", plan)
        editor.snapshot_previous_analysis(old, self.root)
        new = copy.deepcopy(old)
        new["shots"][0]["material_duration_us"] = 9_999_667
        with patch.object(editor, "_editor_model", return_value="offline"), \
                patch.object(editor, "_inventory", side_effect=AssertionError("inventory rerun")), \
                patch.object(editor, "_call", side_effect=AssertionError("model call")), \
                patch.object(editor, "_review_pool", side_effect=AssertionError("full review rerun")), \
                patch.object(editor, "_restore_interrupted_groups", side_effect=AssertionError("stale group restored")):
            result = editor.prepare(self.manifest, new, self.root)
        resumed = result["ai_editor_plan"]
        self.assertTrue(resumed["ok"], resumed.get("issues"))
        self.assertEqual(resumed["reviews"], plan["reviews"])
        self.assertEqual(resumed["rows"], plan["rows"])
        self.assertEqual(resumed["duration_migration"]["previous_analysis_sha256"], contract.digest(old))
        self.assertTrue(editor.match_manifest(self.manifest, result)["ok"])
        first_snapshot = folder / resumed["duration_migration"]["snapshot"]
        self.assertEqual(contract.digest(json.loads(first_snapshot.read_text(encoding="utf-8"))), contract.digest(old))
        editor.snapshot_previous_analysis(new, self.root)
        newest = copy.deepcopy(new)
        newest["shots"][0]["material_duration_us"] = 9_999_334
        with patch.object(editor, "_editor_model", return_value="offline"), \
                patch.object(editor, "_inventory", side_effect=AssertionError("inventory rerun")), \
                patch.object(editor, "_call", side_effect=AssertionError("model call")), \
                patch.object(editor, "_review_pool", side_effect=AssertionError("full review rerun")):
            second = editor.prepare(self.manifest, newest, self.root)["ai_editor_plan"]
            third = editor.prepare(self.manifest, newest, self.root)["ai_editor_plan"]
        self.assertTrue(second["ok"] and third["ok"])
        self.assertEqual(len(second["duration_migrations"]), 2)
        self.assertEqual(third["duration_migrations"], second["duration_migrations"])
        self.assertEqual(contract.digest(json.loads(first_snapshot.read_text(encoding="utf-8"))), contract.digest(old))
        self.assertNotEqual(second["duration_migrations"][0]["snapshot"], second["duration_migrations"][1]["snapshot"])

    def test_duration_migration_rejects_changed_observation_or_snapshot(self):
        old = copy.deepcopy(self.analysis)
        old["shots"][0]["material_duration_us"] = 10_000_000
        plan = self.plan()
        plan["identity"].update(analysis_sha256=contract.digest(old), policy_version=contract.POLICY_VERSION)
        plan["resume_identity"] = copy.deepcopy(plan["identity"])
        folder = self.root / "ai_editor"
        folder.mkdir()
        editor._save(folder / "plan.json", plan)
        editor.snapshot_previous_analysis(old, self.root)
        new = copy.deepcopy(old)
        new["shots"][0].update(material_duration_us=9_999_667, description="different observed action")
        with patch.object(editor, "_editor_model", return_value="offline"), \
                patch.object(editor, "_inventory", side_effect=ValueError("changed observation")) as inventory:
            result = editor.prepare(self.manifest, new, self.root)
        inventory.assert_called_once()
        self.assertFalse(result["ai_editor_plan"]["ok"])
        self.assertNotIn("duration_migration", result["ai_editor_plan"])

    def test_migration_rejects_tampered_snapshot_and_out_of_bounds_review(self):
        old = copy.deepcopy(self.analysis)
        old["shots"][0]["material_duration_us"] = 10_000_000
        plan = self.plan()
        plan["identity"].update(analysis_sha256=contract.digest(old), policy_version=contract.POLICY_VERSION)
        plan["resume_identity"] = copy.deepcopy(plan["identity"])
        folder = self.root / "ai_editor"
        folder.mkdir()
        editor._save(folder / "plan.json", plan)
        editor.snapshot_previous_analysis(old, self.root)
        new = copy.deepcopy(old)
        new["shots"][0]["material_duration_us"] = 9_999_667
        identity = {**plan["resume_identity"], "analysis_sha256": contract.digest(new)}
        plan["rows"][0]["parts"][0]["window_review_key"] = "wide"
        plan["window_reviews"] = {"wide": {"review_window_us": {"start": 0, "end": 10_000_000}}}
        self.assertFalse(editor._duration_resume_safe(plan, identity, new, folder))
        plan["rows"][0]["parts"][0].pop("window_review_key")
        path = folder / f"previous_analysis_{contract.digest(old)}.json"
        path.write_text('{"tampered": true}', encoding="utf-8")
        editor.snapshot_previous_analysis(old, self.root)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"tampered": True})
        self.assertFalse(editor._duration_resume_safe(plan, identity, new, folder))

    def test_duration_migration_checks_legacy_and_current_analysis_digests(self):
        old = copy.deepcopy(self.analysis)
        old["shots"][0]["material_duration_us"] = 10_000_000
        old["vision_analysis"] = {"model": "offline", "cache_wait_s": 0.1, "cache_hits": 2}
        new = copy.deepcopy(old)
        new["shots"][0]["material_duration_us"] = 9_999_667
        new["vision_analysis"]["cache_wait_s"] = 3.2
        for mode, old_digest in (("legacy", contract.digest(old)), ("current", editor._analysis_digest(old))):
            with self.subTest(mode=mode):
                root = self.root / mode
                folder = root / "ai_editor"
                folder.mkdir(parents=True)
                plan = self.plan()
                plan["identity"].update(analysis_sha256=old_digest, policy_version=contract.POLICY_VERSION)
                plan["resume_identity"] = copy.deepcopy(plan["identity"])
                editor._save(folder / "plan.json", plan)
                editor.snapshot_previous_analysis(old, root)
                identity = {**plan["resume_identity"], "analysis_sha256": editor._analysis_digest(new)}
                self.assertTrue(editor._duration_resume_safe(plan, identity, new, folder))
                changed = copy.deepcopy(new)
                changed["vision_analysis"]["cache_hits"] += 1
                self.assertFalse(editor._duration_resume_safe(plan, identity, changed, folder))
                inconsistent = copy.deepcopy(plan)
                inconsistent["identity"]["analysis_sha256"] = "0" * 64
                self.assertFalse(editor._duration_resume_safe(inconsistent, identity, new, folder))

    def test_proposed_window_never_reviews_past_physical_end(self):
        shot = {**self.shots[0], "source_end": 4.167, "material_duration_us": 4_166_667}
        with patch.object(editor, "_review") as review, \
                patch.object(editor, "_review_placement_window") as placement:
            result = editor._review_proposed_window(self.manifest, self.plan(), self.root,
                                                    self.sentences[0], shot, (2_027_521, 4_167_000))
        self.assertIsNone(result)
        review.assert_not_called()
        placement.assert_not_called()

    def test_placement_window_inherits_selling_point_and_only_judges_cut(self):
        plan = self.plan()
        parent = plan["reviews"]["1"][0]
        shot = self.analysis["shots"][0]
        window = (300_000, 1_823_166)
        model = {"verdict": "suitable", "same_product": True, "confidence": 0.92,
                 "observation": "裁切内可见完整检测读数片段", "reason": "未拦腰切断",
                 "evidence_intervals_us": [{"start": 300_000, "end": 1_061_583}],
                 # Model wrongly tries to clear selling-point support; placement must ignore it.
                 "supported_point_ids": [], "basis": "product_display", "time_sensitive": True}
        with patch.object(editor, "_review_media", return_value={
                "start": 0.3, "end": 1.823166, "before": parent["source_identity"],
                "times": [0.3, 1.0, 1.8], "sheet": self.root / "sheet.jpg"}), \
                patch.object(editor, "_call", return_value=model) as call, \
                patch.object(editor, "_review", side_effect=AssertionError("semantic re-review")):
            key = editor._review_proposed_window(
                self.manifest, plan, self.root, plan["sentences"][0], shot, window)
        self.assertIsNotNone(key)
        self.assertIn("放置短窗复核", call.call_args.args[0])
        self.assertIn("不要重新判定卖点是否成立", call.call_args.args[0])
        saved = plan["window_reviews"][key]
        self.assertTrue(saved["placement_review"])
        self.assertEqual(saved["verdict"], "matched")
        self.assertEqual(saved["supported_point_ids"], parent["supported_point_ids"])
        self.assertEqual(saved["basis"], parent["basis"])
        self.assertEqual(saved["time_sensitive"], parent["time_sensitive"])
        self.assertEqual(saved["evidence_intervals_us"], model["evidence_intervals_us"])

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
                patch.object(editor.sentence_continuity, "repair", return_value=False), \
                patch.object(editor, "_revalidate_rows", side_effect=failures):
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

    def test_duplicate_repair_prefers_ungrouped_earlier_sentence(self):
        plan = self.plan()
        plan['rows'][1]['continuity_key'] = 'reviewed-group'
        grouped = copy.deepcopy(plan['rows'][1])
        failure = ValueError('AI_EDITOR_REPEATED_SOURCE_WINDOW:1:2')
        self.assertEqual(editor._duplicate_repair_focus(plan, failure), 1)
        def repair(_m, _a, target_plan, _d, _t, index, _expanded):
            self.assertEqual(index, 1)
            target_plan['rows'][0]['parts'][0]['source_start_us'] = 3_000_000
            return True
        with patch.object(editor, '_revalidate_rows', side_effect=[failure, {'ok': True}]), \
             patch.object(editor, '_repair_repeated_window', side_effect=repair):
            result = editor._repair_reviewed_rows(self.manifest, self.analysis, plan, self.root, None, set())
        self.assertTrue(result['ok'])
        self.assertEqual(plan['rows'][1], grouped)

    def test_duplicate_editor_focus_preserves_grouped_later_sentence(self):
        plan = self.plan()
        plan['rows'][1]['continuity_key'] = 'reviewed-group'
        grouped = copy.deepcopy(plan['rows'][1])
        revised = copy.deepcopy(plan['rows'][0])
        revised['parts'][0]['source_start_us'] = 3_000_000
        with patch.object(editor, '_call', side_effect=[{'rows': copy.deepcopy(plan['rows'])}, {'rows': [revised]}]) as call, \
             patch.object(editor, '_repair_reviewed_rows', side_effect=[ValueError('AI_EDITOR_REPEATED_SOURCE_WINDOW:1:2'), None]):
            editor._edit_plan(self.manifest, self.analysis, plan, self.root)
        self.assertIn('只返回index=1', call.call_args_list[1].args[0])
        self.assertEqual(plan['rows'][1], grouped)

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
