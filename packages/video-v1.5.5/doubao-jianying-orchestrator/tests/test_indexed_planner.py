"""Offline contracts; synthetic evidence is never a production acceptance receipt."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import contextual_plan as cp, indexed_planner as ip


class IndexedPlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.report = self.root / "report"
        self.calls = []
        self.manifest = {"task_id": "offline-contract", "delivery_mode": "formal", "fps": 30,
                         "tail_pad_s": 0, "segments": []}
        for index, text in enumerate(("开始擦拭", "展示擦净结果", "包装展示"), 1):
            path = self.root / f"audio{index}.ogg"
            path.write_bytes(f"synthetic frozen audio {index}".encode())
            self.manifest["segments"].append({"text": text, "audio": str(path), "audio_duration_us": 2*ip.US})
        self.groups = [{"id": "g1", "members": [1, 2], "query": "完整擦拭到干净", "require_complete_action": True},
                       {"id": "g2", "members": [3], "query": "包装展示", "require_complete_action": False}]
        self.candidates = []
        for cid in ("wipe", "wipe_alternate", "pack"):
            video, frame = self.root / f"{cid}.mp4", self.root / f"{cid}.jpg"
            video.write_bytes(f"synthetic source {cid}".encode())
            frame.write_bytes(f"synthetic observation frame {cid}".encode())
            self.candidates.append({"candidate_id": cid, "video": str(video), "source_start_us": 0,
                "source_end_us": 12*ip.US, "description": "完整擦拭至干净" if cid != "pack" else "包装展示",
                "verification_status": "verified", "provenance": {"kind": "synthetic_test_observation"},
                "action_segments": [{"start": 0, "end": 7.9 if cid != "pack" else 3.9,
                                      "action": "擦拭" if cid != "pack" else "展示", "result": "干净"}],
                "evidence": [{"path": str(frame), "timestamp_us": ip.US}]})

    def model(self, request):
        self.calls.append(copy.deepcopy(request))
        stage, payload = request["stage"], request["payload"]
        if stage == "group":
            return {"groups": copy.deepcopy(self.groups)}
        if stage in {"plan", "repair"}:
            group = payload["group"]
            return {"candidate_id": "wipe" if group["id"] == "g1" else "pack",
                    "source_start_us": 0, "source_end_us": (8 if group["id"] == "g1" else 4)*ip.US,
                    "reason": "Synthetic fixture only; not visual acceptance."}
        return {"status": "complete" if payload["require_complete_action"] else "not_required",
                "action_complete": True, "visible_at_speed": True, "issues": [],
                "observation": "Synthetic independent observation fixture only.",
                "observations": [{"index": i, "status": "supported", "observation": "Synthetic relation."}
                                 for i in payload["members"]]}

    @staticmethod
    def render(rows, output):
        output.write_bytes(json.dumps(rows, sort_keys=True).encode())
        return output

    @staticmethod
    def extract(preview, timestamp_us, output):
        output.write_bytes(preview.read_bytes() + str(timestamp_us).encode())
        return True

    def run_plan(self, model=None, **kwargs):
        return ip.prepare(self.manifest, self.candidates, self.report, base_dir=self.root,
                          model=model or self.model, frame_extractor=self.extract,
                          slice_renderer=self.render, **kwargs)

    def test_compiles_continuous_groups_without_changing_frozen_audio_or_text(self):
        before = copy.deepcopy(self.manifest)
        result = self.run_plan()
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(self.manifest, before)
        contract = result["contextual_plan"]
        self.assertEqual(contract.rows[0]["speed"], 2)
        self.assertEqual(contract.rows[0]["source_end_us"], contract.rows[1]["source_start_us"])
        self.assertEqual([r["text"] for r in contract.rows], [r["text"] for r in before["segments"]])
        self.assertFalse(result["formal_delivery_allowed"])
        self.assertEqual(result["human_review"], "PENDING")
        self.assertEqual(contract.report()["old_scene_swap_calls"], 0)

    def test_explicit_indexed_groups_avoid_duplicate_group_model_call(self):
        self.manifest["indexed_groups"] = self.groups
        result = self.run_plan()
        self.assertTrue(result["ok"], result["issues"])
        self.assertNotIn("group", [r["stage"] for r in self.calls])

    def test_group_cannot_invent_wipe_requirement_for_wet_compress(self):
        self.groups[0]["required_requirements"] = ["wipe_start_to_clean_result"]
        result = self.run_plan()
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["type"], "UNDECLARED_VISUAL_REQUIREMENT")

    def test_ready_label_alone_does_not_become_verified_observation(self):
        for row in self.candidates:
            row.pop("verification_status")
            row["analysis_status"] = "ready_for_matching"
        result = self.run_plan()
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["type"], "NO_VERIFIED_CANDIDATES")
        self.assertEqual([r["stage"] for r in self.calls], ["group"])

    def test_candidate_evidence_tampering_is_rejected_before_selection(self):
        for row in self.candidates:
            row["evidence"][0]["sha256"] = "0"*64
        result = self.run_plan()
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["type"], "NO_VERIFIED_CANDIDATES")

    def test_observer_does_not_receive_planner_labels_or_reasons(self):
        result = self.run_plan()
        self.assertTrue(result["ok"], result["issues"])
        observer = next(r for r in self.calls if r["stage"] == "observe")
        self.assertNotIn("candidates", observer["payload"])
        self.assertNotIn("reason", json.dumps(observer["payload"]))
        self.assertNotIn("full_script", observer["payload"])
        self.assertEqual([r["index"] for r in observer["payload"]["current_utterances"]], [1, 2])
        self.assertFalse(observer["payload"]["surrounding_context"]["is_acceptance_requirement"])
        self.assertGreater(len(observer["images"]), 4)
        self.assertIn("rendered_timestamp_us", observer["images"][0])

    def test_wrong_semantics_are_not_overridden_by_planner_confidence(self):
        def model(request):
            response = self.model(request)
            if request["stage"] == "observe" and request["payload"]["group_id"] == "g1":
                response.update(status="unsupported", action_complete=False,
                                observation="透光不能证明吸水，画面动作与口播不对应")
            return response
        result = self.run_plan(model=model, max_repairs=0)
        self.assertFalse(result["ok"])
        self.assertIn("ACTION_INCOMPLETE_OR_UNVERIFIED", [i["type"] for i in result["issues"]])
        self.assertIsNone(result["prepared_manifest"])

    def test_only_failed_group_is_repaired(self):
        def model(request):
            response = self.model(request)
            if request["stage"] == "repair":
                response["candidate_id"] = "wipe_alternate"
            if (request["stage"] == "observe" and request["payload"]["group_id"] == "g1"
                    and not any(r["stage"] == "repair" for r in self.calls)):
                response.update(status="unsupported", action_complete=False)
            return response
        result = self.run_plan(model=model)
        self.assertTrue(result["ok"], result["issues"])
        repairs = [r for r in self.calls if r["stage"] == "repair"]
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["failed_groups"], ["g1"])
        self.assertEqual(sum(r["stage"] == "plan" and r["payload"]["group"]["id"] == "g2" for r in self.calls), 1)
        self.assertEqual(result["stats"]["group_repairs"], {"g1": 1, "g2": 0})

    def test_repairs_are_bounded_at_two_and_never_regroup(self):
        def model(request):
            response = self.model(request)
            if request["stage"] == "observe":
                response["visible_at_speed"] = False
            return response
        result = self.run_plan(model=model, max_repairs=100)
        self.assertFalse(result["ok"])
        self.assertEqual(result["stats"]["group_repairs"], {"g1": 2, "g2": 2})
        self.assertEqual(sum(r["stage"] == "group" for r in self.calls), 1)
        self.assertEqual(len(list((self.report/"attempts").glob("g1_*.json"))), 3)

    def test_hot_repeat_has_zero_new_model_calls_and_no_material_understanding(self):
        self.assertTrue(self.run_plan()["ok"])
        def forbidden(request):
            raise AssertionError("repeat must use receipts")
        result = self.run_plan(model=forbidden)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["stats"]["model_calls"], 0)
        self.assertEqual(result["stats"]["material_understanding_calls"], 0)

    def test_incomplete_source_window_cannot_ignore_observed_action_boundary(self):
        def model(request):
            response = self.model(request)
            if request["stage"] in {"plan", "repair"} and request["payload"]["group"]["id"] == "g1":
                response["source_start_us"] = 2*ip.US
            return response
        result = self.run_plan(model=model, max_repairs=0)
        self.assertFalse(result["ok"])
        self.assertIn("ACTION_NOT_COVERED", [i["type"] for i in result["issues"]])

    def test_static_lead_in_does_not_satisfy_complete_action(self):
        self.candidates[0]["action_segments"] = [
            {"start": 0, "end": 3, "action": "无动作", "result": None},
            {"start": 6, "end": 10, "action": "擦拭", "result": "擦净"}]
        result = self.run_plan(max_repairs=0)
        self.assertFalse(result["ok"])
        self.assertIn("ACTION_NOT_COVERED", [i["type"] for i in result["issues"]])

    def test_frozen_duration_missing_is_structured_and_makes_no_model_calls(self):
        self.manifest["segments"][0].pop("audio_duration_us")
        result = self.run_plan()
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["type"], "INVALID_TIMING")
        self.assertEqual(self.calls, [])

    def test_invalid_group_coverage_is_blocked(self):
        self.manifest["indexed_groups"] = [{**self.groups[0], "members": [1, 3]}]
        result = self.run_plan()
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["type"], "INVALID_SCRIPT_GROUPS")

    def test_actual_preview_hash_is_checked_before_build(self):
        result = self.run_plan()
        self.assertTrue(result["ok"], result["issues"])
        preview = Path(result["contextual_plan"].observer["groups"][0]["preview_file"])
        preview.write_bytes(b"tampered actual slice")
        with self.assertRaisesRegex(cp.ContextualPlanError, "SHA-256 mismatch"):
            cp.load(result["prepared_manifest"], self.root)

    def test_readback_detects_saved_window_mutation(self):
        result = self.run_plan()
        contract = result["contextual_plan"]
        materials, segments = [], []
        for row in contract.rows:
            mid = str(row["index"])
            materials.append({"id": mid, "path": row["video"]})
            segments.append({"material_id": mid, "source_timerange": {"start": row["source_start_us"],
                "duration": row["source_end_us"]-row["source_start_us"]},
                "target_timerange": {"start": row["timeline_start_us"], "duration": row["duration_us"]}})
        draft = {"materials": {"videos": materials}, "tracks": [{"type": "video", "segments": segments}]}
        self.assertEqual(contract.readback_issues(draft), [])
        segments[1]["source_timerange"]["start"] = 0
        self.assertEqual(contract.readback_issues(draft)[0]["type"], "CONTEXTUAL_READBACK_LOCK_CHANGED")

    def test_align_checks_audio_and_locks_windows_without_running_solver(self):
        result = self.run_plan()
        contract = result["contextual_plan"]
        clips = [SimpleNamespace(video=r["video"], audio=r["audio"], text=r["text"],
                                 source_start=0, duration=0, video_speed=1, start_us=0) for r in contract.rows]
        self.assertEqual(contract.align(clips, lambda _: {"duration": 2}, lambda _: 12, 0), [])
        self.assertEqual(contract.final_issues(clips), [])
        clips[0].video_speed = 1
        self.assertEqual(contract.final_issues(clips)[0]["type"], "CONTEXTUAL_LOCKED_PLAN_CHANGED")

    def test_new_requirements_are_not_inferred_from_old_verified_description(self):
        self.manifest["indexed_groups"] = copy.deepcopy(self.groups)
        self.manifest["indexed_groups"][0]["required_requirements"] = ["wipe_start_to_clean_result"]
        result = self.run_plan()
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["type"], "GROUP_REQUIREMENTS_UNVERIFIED")
        self.assertFalse(any(r["stage"] in {"plan", "repair"} and r["payload"]["group"]["id"] == "g1"
                             for r in self.calls))
        self.candidates[0]["verified_requirements"] = ["wipe_start_to_clean_result"]
        result = self.run_plan()
        self.assertTrue(result["ok"], result["issues"])

    def test_provider_failure_stops_further_group_requests_and_retains_checkpoint(self):
        self.manifest["indexed_groups"] = self.groups
        def unavailable(request):
            self.calls.append(request)
            raise TimeoutError("synthetic provider unavailable")
        result = self.run_plan(model=unavailable)
        self.assertFalse(result["ok"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["issues"][0]["type"], "MODEL_CALL_FAILED")
        self.assertTrue((self.report/"attempts"/"g1_00.json").is_file())

    def test_feedback_cannot_be_applied_to_another_plan_or_media(self):
        result = self.run_plan()
        feedback_file = self.report/"feedback_template.json"
        self.assertEqual(ip.feedback_cases(feedback_file, Path(result["plan_file"]), Path(result["observer_file"])), [])
        feedback = json.loads(feedback_file.read_text(encoding="utf-8"))
        feedback["reviewer_id"] = "offline-test-human"
        feedback["groups"][0].update(decision="reject", issues=["result_truncated"])
        feedback_file.write_text(json.dumps(feedback), encoding="utf-8")
        cases = ip.feedback_cases(feedback_file, Path(result["plan_file"]), Path(result["observer_file"]))
        self.assertEqual(cases[0]["issues"], ["result_truncated"])
        self.assertFalse(cases[0]["formal_delivery_allowed"])
        feedback["groups"][0]["video_sha256"] = "0"*64
        feedback_file.write_text(json.dumps(feedback), encoding="utf-8")
        with self.assertRaisesRegex(ip.IndexedPlanError, "binding changed"):
            ip.feedback_cases(feedback_file, Path(result["plan_file"]), Path(result["observer_file"]))

    def test_saved_speed_material_cannot_override_locked_geometry(self):
        result = self.run_plan()
        contract = result["contextual_plan"]
        materials, segments = [], []
        for row in contract.rows:
            mid = str(row["index"])
            materials.append({"id": mid, "path": row["video"]})
            segments.append({"material_id": mid, "speed": row["speed"],
                "source_timerange": {"start": row["source_start_us"], "duration": row["source_end_us"]-row["source_start_us"]},
                "target_timerange": {"start": row["timeline_start_us"], "duration": row["duration_us"]}})
        draft = {"materials": {"videos": materials, "speeds": [{"id": "bad-speed", "speed": 1}]},
                 "tracks": [{"type": "video", "segments": segments}]}
        segments[0]["extra_material_refs"] = ["bad-speed"]
        self.assertEqual(contract.readback_issues(draft)[0]["type"], "CONTEXTUAL_READBACK_LOCK_CHANGED")

    def test_gap_expands_only_current_group_without_consuming_observer_repairs(self):
        expanded = []
        def model(request):
            response = self.model(request)
            if request["stage"] == "plan" and request["payload"]["group"]["id"] == "g1":
                if not expanded:
                    return {"gap": "first shortlist misses matching action"}
                response["candidate_id"] = "wipe_alternate"
            return response
        def expand(group, pool):
            expanded.append((group["id"], len(pool)))
            return [self.candidates[1]]
        result = self.run_plan(model=model, candidate_expander=expand)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(expanded, [("g1", 3)])
        self.assertEqual(result["stats"]["group_repairs"], {"g1": 0, "g2": 0})
        self.assertEqual(result["stats"]["group_expansions"], {"g1": 1})
        self.assertEqual(sum(r["stage"] == "plan" and r["payload"]["group"]["id"] == "g2" for r in self.calls), 1)

    def test_missing_requirement_can_expand_to_explicitly_verified_candidate(self):
        self.manifest["indexed_groups"] = copy.deepcopy(self.groups)
        self.manifest["indexed_groups"][0]["required_requirements"] = ["wipe_start_to_clean_result"]
        def expand(group, pool):
            candidate = copy.deepcopy(self.candidates[0])
            candidate["verified_requirements"] = ["wipe_start_to_clean_result"]
            return [candidate]
        result = self.run_plan(candidate_expander=expand)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(result["stats"]["group_expansions"], {"g1": 1})

    def test_duration_failure_expands_once_then_keeps_other_groups_locked(self):
        expanded = []
        def model(request):
            response = self.model(request)
            if request["stage"] == "plan" and request["payload"]["group"]["id"] == "g1":
                if not expanded:
                    response["source_end_us"] = 2*ip.US
                else:
                    response["candidate_id"] = "wipe_alternate"
            return response
        def expand(group, pool):
            expanded.append(group["id"])
            return [self.candidates[1]]
        result = self.run_plan(model=model, candidate_expander=expand)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(expanded, ["g1"])
        self.assertEqual(result["stats"]["group_repairs"]["g1"], 0)

    def test_expansion_is_bounded_and_cannot_inject_unverified_fallback(self):
        expanded = []
        def model(request):
            response = self.model(request)
            if request["stage"] == "plan" and request["payload"]["group"]["id"] == "g1":
                return {"gap": "no action in shortlist"}
            return response
        def expand(group, pool):
            expanded.append(group["id"])
            candidate = copy.deepcopy(self.candidates[0])
            candidate.pop("verification_status")
            candidate["analysis_status"] = "ready_for_matching"
            return [candidate]
        result = self.run_plan(model=model, candidate_expander=expand)
        self.assertFalse(result["ok"])
        self.assertEqual(expanded, ["g1"])
        self.assertIn("CANDIDATE_EXPANSION_EXHAUSTED", [i["type"] for i in result["issues"]])
        self.assertFalse(any(r["stage"] == "repair" for r in self.calls))

    def test_expansion_keeps_all_windows_so_ninth_material_reaches_planner(self):
        self.manifest["segments"] = self.manifest["segments"][:2]
        self.groups = self.groups[:1]
        initial = []
        for material in range(8):
            for window in range(3):
                candidate = copy.deepcopy(self.candidates[2])
                candidate.update(candidate_id=f"pack_{material}_{window}", material_id=f"material_{material}")
                initial.append(candidate)
        ninth = copy.deepcopy(self.candidates[0])
        ninth.update(candidate_id="ninth_material_wipe", material_id="material_8")
        self.candidates = initial
        pools = []
        def model(request):
            if request["stage"] in {"plan", "repair"}:
                pool = request["payload"]["candidates"]
                pools.append([candidate["candidate_id"] for candidate in pool])
                if ninth["candidate_id"] not in pools[-1]:
                    return {"gap": "Only packaging in the first eight materials."}
                return {"candidate_id": ninth["candidate_id"], "source_start_us": 0,
                        "source_end_us": 8*ip.US, "reason": "Synthetic fixture only."}
            return self.model(request)
        result = self.run_plan(model=model, candidate_expander=lambda group, current: initial + [ninth])
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(len(pools[0]), 24)
        self.assertEqual(len(pools[1]), 25)
        self.assertIn(ninth["candidate_id"], pools[1])
        receipt = json.loads((self.report / "attempts" / "g1_expansion.json").read_text(encoding="utf-8"))
        self.assertEqual((receipt["maximum_materials"], receipt["material_count"], receipt["window_count"]), (16, 9, 25))

    def test_material_budget_preserves_later_windows_and_limits_distinct_sources(self):
        rows = [{"candidate_id": f"m{material}_w{window}", "material_id": f"m{material}"}
                for material in range(17) for window in range(2)]
        rows.append({"candidate_id": "m0_late_window", "material_id": "m0"})
        selected, count = ip._limit_materials(rows, self.root)
        self.assertEqual(count, 16)
        self.assertEqual(len(selected), 33)
        self.assertFalse(any(row["material_id"] == "m16" for row in selected))
        self.assertEqual(selected[-1]["candidate_id"], "m0_late_window")
        self.assertEqual(ip._material_identity({"material_id": "sku-source", "video_sha256": "abc"}, self.root),
                         ("material_id", "sku-source"))
        self.assertEqual(ip._material_identity({"video_sha256": "ABC", "video": "one.mp4"}, self.root),
                         ("video_sha256", "abc"))
        self.assertEqual(ip._material_identity({"video": "folder/../one.mp4"}, self.root),
                         ip._material_identity({"video": "one.mp4"}, self.root))
        self.assertEqual(ip._material_identity({"candidate_id": "last-resort"}, self.root),
                         ("candidate_id", "last-resort"))

    def test_multiple_windows_hash_same_video_and_frame_once_per_validation(self):
        rows = [{**copy.deepcopy(self.candidates[0]), "candidate_id": f"window_{index}"} for index in range(8)]
        with mock.patch.object(cp, "_hash", wraps=cp._hash) as digest:
            normalized, rejected = ip._candidates(rows, self.root)
        self.assertEqual(len(normalized), 8)
        self.assertEqual(rejected, [])
        self.assertEqual(digest.call_count, 2)

    def test_observer_mismatch_expands_shortlist_before_repairing_same_eight(self):
        expanded = []
        def model(request):
            response = self.model(request)
            payload = request["payload"]
            if request["stage"] == "plan" and payload["group"]["id"] == "g1" and expanded:
                response["candidate_id"] = "wipe_alternate"
            if request["stage"] == "observe" and payload["group_id"] == "g1" and not expanded:
                response.update(status="unsupported", action_complete=False)
            return response
        def expand(group, pool):
            expanded.append(group["id"])
            return [self.candidates[1]]
        result = self.run_plan(model=model, candidate_expander=expand)
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual(expanded, ["g1"])
        self.assertEqual(result["stats"]["group_repairs"]["g1"], 0)

    def test_task_owner_acceptance_only_invalidates_selected_observer_cache(self):
        baseline = self.run_plan()
        self.assertTrue(baseline["ok"], baseline["issues"])
        old_groups = {g["id"]: g for g in baseline["contextual_plan"].observer["groups"]}
        self.calls.clear()
        self.manifest["indexed_contextual_acceptance"] = {
            "utterance_indices": [3], "basis": "task_owner", "criterion": "same_product_contextual"}
        result = self.run_plan()
        self.assertTrue(result["ok"], result["issues"])
        self.assertEqual([(r["stage"], r["payload"].get("group_id")) for r in self.calls], [("observe", "g2")])
        request = self.calls[0]
        self.assertEqual(request["payload"]["contextual_acceptance"]["utterance_indices"], [3])
        self.assertIn("不要求包装上的片数/规格文字可读", request["prompt"])
        new_groups = {g["id"]: g for g in result["contextual_plan"].observer["groups"]}
        self.assertEqual(new_groups["g1"]["response_file"], old_groups["g1"]["response_file"])
        self.assertNotEqual(new_groups["g2"]["response_file"], old_groups["g2"]["response_file"])
        self.assertFalse(result["formal_delivery_allowed"])
        self.assertEqual(result["human_review"], "PENDING")
        self.manifest.pop("indexed_contextual_acceptance")
        self.calls.clear()
        resumed = self.run_plan()
        self.assertTrue(resumed["ok"], resumed["issues"])
        self.assertEqual(self.calls, [])
        resumed_groups = {g["id"]: g for g in resumed["contextual_plan"].observer["groups"]}
        self.assertEqual(resumed_groups["g2"]["response_file"], old_groups["g2"]["response_file"])

    def test_contextual_acceptance_cannot_relax_action_group(self):
        self.manifest["indexed_groups"] = self.groups
        self.manifest["indexed_contextual_acceptance"] = {
            "utterance_indices": [1, 3], "basis": "task_owner", "criterion": "same_product_contextual"}
        result = self.run_plan()
        self.assertFalse(result["ok"])
        self.assertEqual(result["issues"][0]["type"], "CONTEXTUAL_ACCEPTANCE_ACTION_CONFLICT")
        self.assertEqual(result["issues"][0]["utterance_indices"], [1])
        self.assertEqual(self.calls, [])

    def test_acceptance_scope_does_not_expand_to_other_group_members(self):
        self.groups = [dict(self.groups[0], members=[1], require_complete_action=False),
                       dict(self.groups[1], members=[2, 3])]
        self.manifest["product"] = "测试纸品"
        self.manifest["indexed_contextual_acceptance"] = {
            "utterance_indices": [3], "basis": "task_owner", "criterion": "same_product_contextual"}
        original = copy.deepcopy(self.manifest)
        result = self.run_plan()
        self.assertTrue(result["ok"], result["issues"])
        calls = [r for r in self.calls if r["stage"] == "observe"]
        untouched = next(r for r in calls if r["payload"]["group_id"] == "g1")
        selected = next(r for r in calls if r["payload"]["group_id"] == "g2")
        self.assertEqual(untouched["prompt"], ip.PROMPTS["observe"])
        self.assertNotIn("contextual_acceptance", untouched["payload"])
        self.assertEqual(selected["payload"]["contextual_acceptance"]["utterance_indices"], [3])
        self.assertEqual(selected["payload"]["contextual_acceptance"]["expected_product"], {"product": "测试纸品"})
        self.assertEqual(self.manifest, original)

    def test_explicit_acceptance_still_obeys_actual_observer_rejection_and_speed(self):
        self.manifest["indexed_contextual_acceptance"] = {
            "utterance_indices": [3], "basis": "task_owner", "criterion": "same_product_contextual"}
        for failure in ("wrong_product", "unreadable_speed"):
            with self.subTest(failure=failure):
                self.report = self.root/failure
                def model(request):
                    response = self.model(request)
                    if request["stage"] == "observe" and request["payload"]["group_id"] == "g2":
                        if failure == "wrong_product":
                            response.update(status="unsupported", observation="实际切片为另一商品，缺少同商品关联")
                            response["observations"][0].update(status="unsupported")
                        else:
                            response["visible_at_speed"] = False
                    return response
                result = self.run_plan(model=model, max_repairs=0)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["prepared_manifest"])
                expected = "SLICE_SEMANTIC_UNSUPPORTED" if failure == "wrong_product" else "PLAYBACK_NOT_INTELLIGIBLE"
                self.assertIn(expected, [r["type"] for r in result["issues"]])

    def test_contextual_acceptance_requires_explicit_valid_owner_and_sentence_binding(self):
        for contract in (
            {"utterance_indices": [True], "basis": "task_owner", "criterion": "same_product_contextual"},
            {"utterance_indices": [4], "basis": "task_owner", "criterion": "same_product_contextual"},
            {"utterance_indices": [3], "basis": "model", "criterion": "same_product_contextual"},
        ):
            with self.subTest(contract=contract):
                self.manifest["indexed_contextual_acceptance"] = contract
                result = self.run_plan()
                self.assertFalse(result["ok"])
                self.assertEqual(result["issues"][0]["type"], "CONTEXTUAL_ACCEPTANCE_INVALID")
        self.assertEqual(self.calls, [])

    def test_remote_route_identity_changes_only_image_stage_cache_when_vision_model_changes(self):
        owner = self
        class RoutedModel:
            vision = "glm"
            def cache_identity(self, *, stage, with_images):
                return {"model": self.vision if with_images else "frozen-text-model",
                        "transport": "agy-cli" if with_images and self.vision == "gemini" else "codex-file"}
            def __call__(self, request):
                return owner.model(request)
        routed = RoutedModel()
        first = self.run_plan(model=routed)
        self.assertTrue(first["ok"], first["issues"])
        self.calls.clear()
        routed.vision = "gemini"
        second = self.run_plan(model=routed)
        self.assertTrue(second["ok"], second["issues"])
        self.assertEqual([r["stage"] for r in self.calls], ["observe", "observe"])
        self.calls.clear()
        routed.vision = "glm"
        restored = self.run_plan(model=routed)
        self.assertTrue(restored["ok"], restored["issues"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
