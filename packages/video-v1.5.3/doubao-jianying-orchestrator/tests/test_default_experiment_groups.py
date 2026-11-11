"""Default-route group fits with frozen visual responses and owned sample fixtures."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import unittest
import wave

import test_experiment_retiming as fixture
from orchestrator import ai_editor_contract as contract
from orchestrator.application.engine import OrchestrationEngine
from orchestrator.domain.clips import Clip
from orchestrator.policy import continuity
from orchestrator.quality import continuity as output_qc, ai_editor_qc
from orchestrator.selection import ai_editor as editor, experiment_groups as groups
from orchestrator.selection import sentence_continuity
from orchestrator.vision import experiment_event_review
from test_experiment_events import facts


class DefaultExperimentGroupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ExperimentRetimingTests(); self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base = self.fixture.base; self.root = self.base.root
        self.manifest = {**self.base.manifest, "selection_mode": "ai_editor", "delivery_mode": "formal",
            "head_trim_s": 0, "tail_trim_s": 0, "tail_pad_s": 0,
            "segments": [{"text": text, "audio_duration_us": duration, "audio": f"original-{i}.wav"}
                for i, (text, duration) in enumerate([
                    ("吸收后锁在中央", 2_300_000), ("纸巾按压不反渗", 2_300_000), ("大品牌放心囤", 2_000_000)])]}
        originals = [m["samples"]["original"]["path"] for m in self.fixture.evidence["members"]]
        originals.append(str(self.base.files["raw-old"]))
        self.shots = [{"shot_id": f"shot{i}", "video": source, "source_start": 0, "source_end": 10,
            "duration": 10, "status": "ready_for_matching", "material_duration_us": 10_000_000}
            for i, source in enumerate(originals, 1)]
        self.analysis = {"shots": self.shots}
        self.plan = {"schema": contract.PLAN_SCHEMA, "policy_version": contract.POLICY_VERSION,
            "sentences": [], "reviews": {}, "display_reviews": [], "rows": [], "coverage": {},
            "identity": {"model": "offline", "editorial_rules": contract.rules_identity(self.manifest)}}
        for i, (row, role, shot) in enumerate(zip(self.manifest["segments"], ("absorption", "rewet", "promotion"), self.shots), 1):
            self.plan["sentences"].append({"index": i, "text": row["text"], "kind": "selling_point",
                "selling_points": [{"id": "p1", "text": row["text"]}], "scene_ids": [],
                "experiment_role": role, "candidate_ids": [shot["shot_id"]]})
            self.plan["reviews"][str(i)] = [{"shot_id": shot["shot_id"], "verdict": "matched", "same_product": True,
                "basis": "visible_result", "observation": "frozen actual frame observation", "reason": "supports claim",
                "confidence": .9, "supported_point_ids": ["p1"], "time_sensitive": False, "scene_ids": [],
                "source_identity": contract.source_identity(shot["video"]),
                "evidence_intervals_us": [{"start": 2_000_000, "end": 2_500_000}]}]
            self.plan["rows"].append({"index": i, "parts": [{"shot_id": shot["shot_id"],
                "audio_duration_us": row["audio_duration_us"], "source_start_us": 2_000_000}]})

    def observed_group(self, _manifest, context, _directory):
        rows = []
        for member in context["members"]:
            rows.append({"index": member["index"], "verdict": "matched", "same_product": True,
                "basis": "editorial_fit", "phase": "actual fixed sentence phase", "observation": "observed phase",
                "reason": "corresponds to sentence", "supported_point_ids": ["p1"], "time_sensitive": False,
                "scene_ids": [], "parts": [{"observation": "actual source phase", "evidence_intervals_us": [
                    {"start": p["start_us"], "end": p["end_us"]}]} for p in member["parts"]]})
        return {"verdict": "matched", "same_product": True, "same_demonstration": True,
            "same_selling_point": True, "semantic_continuity": True, "complete_action": True, "result_preserved": True,
            "shared_point": "absorption and verification", "product_observation": "same product visible",
            "demonstration_observation": "同一样品倒液吸收后纸巾按压并提起，纸面干燥", "observation": "complete experiment",
            "reason": "fixed phases support each clause", "confidence": .9, "members": rows}

    def repair(self):
        with patch.object(editor, "_place_absorb_experiment", return_value=False), \
             patch.object(groups, "observe_pair", return_value=(self.fixture.evidence, self.fixture.motion)) as observe, \
             patch.object(groups, "review_group", side_effect=self.observed_group):
            result = editor._repair_reviewed_rows(self.manifest, self.analysis, self.plan, self.root, None, set())
        self.assertEqual(observe.call_count, 1)
        return result

    def promotion_before_group(self):
        order = [2, 0, 1]
        self.manifest["segments"] = [copy.deepcopy(self.manifest["segments"][i]) for i in order]
        sentences = [copy.deepcopy(self.plan["sentences"][i]) for i in order]
        rows = [copy.deepcopy(self.plan["rows"][i]) for i in order]
        reviews = copy.deepcopy(self.plan["reviews"])
        for index, sentence in enumerate(sentences, 1): sentence["index"] = index
        for index, row in enumerate(rows, 1): row["index"] = index
        self.plan.update(sentences=sentences, rows=rows,
            reviews={str(index + 1): reviews[str(old + 1)] for index, old in enumerate(order)})

    def mixed_group(self):
        self.promotion_before_group()
        matched = self.repair()
        matched["segments"][0].pop("ai_selection")
        matched["segments"][0]["selection_mode"] = "rule"
        return matched

    def test_experiment_group_requires_observed_tissue_contact_and_lift(self):
        result = self.repair()
        group = copy.deepcopy(result["segments"][0]["continuity_group"])
        self.assertEqual(group["schema"], continuity.EXPERIMENT_SCHEMA)
        self.assertEqual(group["input"]["category"], "weipin")
        continuity.validate(group)
        group["review"]["demonstration_observation"] = "未见纸巾，只有手掌按压后抬起"
        group["review"]["observation"] = "仅手掌接触巾体"
        group["review"]["reason"] = "纸巾按压后提起，纸面干燥"
        for row in group["review"]["members"]:
            row["observation"] = "仅手掌接触巾体"
            row["reason"] = "纸巾按压后提起，纸面干燥"
            for part in row["parts"]:
                part["observation"] = "仅手掌接触巾体"
        with self.assertRaisesRegex(ValueError, "CONTINUITY_TISSUE_PRESS_REQUIRED"):
            continuity.validate(group)
        group["review"]["demonstration_observation"] = "同一样品倒液后纸巾按压并提起，未见湿痕"
        continuity.validate(group)

    def test_default_repair_pairs_files_and_keeps_voice_and_unrelated_row(self):
        original = copy.deepcopy(self.manifest)
        old = editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None)
        result = self.repair()
        self.assertTrue(result["ok"])
        self.assertEqual(self.manifest, original)
        self.assertEqual(result["segments"][2], old["segments"][2])
        parts = [p for s in result["segments"][:2] for p in s["span_parts"]]
        self.assertTrue(any(1 < p["video_speed"] <= 3 for p in parts))
        self.assertTrue(all(p["video_speed"] == 1 for p in parts if not p["playback_observation"]))
        self.assertEqual([s["audio"] for s in result["segments"]], [s["audio"] for s in original["segments"]])
        self.assertEqual([s["audio_duration_us"] for s in result["segments"]],
                         [s["audio_duration_us"] for s in original["segments"]])
        self.assertFalse(contract.sequence_errors(result["segments"]))
        for i, row in enumerate(result["segments"], 1):
            self.assertTrue(contract.validate_segment_receipt(row, index=i, manifest=self.manifest, verify_source=True)["ok"])

    def test_default_can_jointly_review_fragments_with_empty_individual_selection(self):
        self.plan["rows"][0]["parts"] = []
        self.plan["reviews"]["1"] = []
        self.assertTrue(self.repair()["ok"])

    def test_experiment_receipts_and_output_qc_bind_explicit_task_cap(self):
        self.manifest["max_absorb_demo_speed"] = 3.0
        result = self.repair()
        for index, row in enumerate(result["segments"][:2], 1):
            self.assertEqual(row["ai_selection"]["task_absorb_speed_cap"], 3.0)
            self.assertTrue(all(p["ai_selection"]["task_absorb_speed_cap"] == 3.0 for p in row["span_parts"]))
            missing = {k: v for k, v in self.manifest.items() if k != "max_absorb_demo_speed"}
            self.assertIn("AI_SELECTION_TASK_SPEED_LIMIT_CHANGED",
                contract.validate_segment_receipt(row, index=index, manifest=missing)["reasons"])
            actual = [{"source": p["video"], "source_start": p["source_start"], "source_duration": p["duration"],
                "target_duration": p["segment_s"], "segment": {"speed": p["video_speed"]},
                "speed_materials": [{"type": "speed", "speed": p["video_speed"]}]} for p in row["span_parts"]]
            claim = {"index": index, "product": self.manifest["product"], "category": "weipin",
                "ai_segment": row, "max_absorb_demo_speed": 3.0}
            self.assertFalse(ai_editor_qc.validate_cut(claim, actual))
            claim.pop("max_absorb_demo_speed")
            self.assertIn("AI_SELECTION_TASK_SPEED_LIMIT_CHANGED", ai_editor_qc.validate_cut(claim, actual))
            actual[0]["speed_materials"] = []
            self.assertIn("AI_QC_FINAL_SPEED_MATERIAL_MISSING", ai_editor_qc.validate_cut(claim, actual))

    def test_outside_reuse_conflict_does_not_reject_experiment_pair(self):
        self.manifest.update(source_reuse_policy="strict-v1", max_source_reuse=1)
        self.manifest["segments"].append({"text": "额外展示", "audio_duration_us": 2_000_000})
        self.shots.append({**self.shots[2], "shot_id": "shot4"})
        self.plan["sentences"].append({"index": 4, "text": "额外展示", "kind": "selling_point",
            "selling_points": [{"id": "p1", "text": "额外展示"}], "scene_ids": [],
            "experiment_role": "promotion", "candidate_ids": ["shot4"]})
        self.plan["reviews"]["4"] = [{**copy.deepcopy(self.plan["reviews"]["3"][0]), "shot_id": "shot4",
            "evidence_intervals_us": [{"start": 5_000_000, "end": 5_500_000}]}]
        self.plan["rows"].append({"index": 4, "parts": [{"shot_id": "shot4",
            "audio_duration_us": 2_000_000, "source_start_us": 5_000_000}]})
        with patch.object(groups, "observe_pair", return_value=(self.fixture.evidence, self.fixture.motion)), \
             patch.object(groups, "review_group", side_effect=self.observed_group):
            self.assertTrue(groups.repair(self.manifest, self.analysis, self.plan, self.root, None))
        with self.assertRaisesRegex(ValueError, "SOURCE_REUSE_LIMIT:4"):
            editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None)
        replacement = self.root / "replacement.mp4"; replacement.write_bytes(b"synthetic replacement")
        self.shots[3]["video"] = str(replacement)
        self.plan["reviews"]["4"][0]["source_identity"] = contract.source_identity(replacement)
        result = editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None, verify_sources=True)
        self.assertTrue(result["ok"])
        self.assertFalse(contract.source_reuse_conflict_details(result["segments"], 1))

    def test_earlier_ordinary_row_can_fix_reuse_conflict_reported_on_group(self):
        self.promotion_before_group()
        self.manifest.update(source_reuse_policy="strict-v1", max_source_reuse=1)
        alternate = copy.deepcopy(self.plan["reviews"]["1"][0])
        same_as_experiment = {**copy.deepcopy(alternate), "shot_id": "shot1",
            "source_identity": contract.source_identity(self.shots[0]["video"]),
            "evidence_intervals_us": [{"start": 6_000_000, "end": 6_500_000}]}
        self.plan["reviews"]["1"] = [same_as_experiment, alternate]
        self.plan["sentences"][0]["candidate_ids"] = ["shot1", "shot3"]
        self.plan["rows"][0]["parts"] = [{"shot_id": "shot1", "audio_duration_us": 2_000_000,
                                           "source_start_us": 6_000_000}]
        result = self.repair()
        self.assertTrue(result["ok"])
        self.assertEqual(result["segments"][0]["shot_id"], "shot3")
        self.assertFalse(contract.source_reuse_conflict_details(result["segments"], 1))

    def test_refill_experiment_group_keeps_prior_rule_row_and_other_member(self):
        matched = self.mixed_group(); prior = copy.deepcopy(matched["segments"])
        with patch.object(groups, "observe_pair", return_value=(self.fixture.evidence, self.fixture.motion)), \
             patch.object(groups, "review_group", side_effect=self.observed_group):
            result = editor.refill_indices({**self.manifest, "ai_refill_indices": [2]}, self.analysis,
                                           matched, [2], self.root / "mixed-group-refill")
        self.assertTrue(result["ok"], result.get("refill_errors"))
        self.assertEqual(result["refill_replaced"], [2])
        self.assertEqual(result["segments"][0], prior[0])
        cuts = lambda row: [(p["video"], p["source_start"], p["source_end"], p["segment_us"], p["video_speed"])
                            for p in row["span_parts"]]
        self.assertEqual(cuts(result["segments"][2]), cuts(prior[2]))
        self.assertEqual(result["selection_mode"], "rule_then_ai")

    def test_refill_after_rule_row_and_experiment_group_keeps_original_indices(self):
        matched = self.mixed_group(); prior = copy.deepcopy(matched["segments"])
        self.manifest["segments"].append({"text": "新文案", "audio_duration_us": 2_000_000})
        matched["segments"].append({"index": 4, "text": "新文案"})
        source = self.root / "new-refill.mp4"; source.write_bytes(b"new synthetic footage")
        shot = {**self.shots[2], "shot_id": "new", "video": str(source)}
        self.shots.append(shot)
        review = {**copy.deepcopy(self.plan["reviews"]["1"][0]), "shot_id": "new",
                  "source_identity": contract.source_identity(source)}
        with patch.object(editor, "_review_pool", return_value=([review], {"feasible": True})):
            result = editor.refill_indices({**self.manifest, "ai_refill_indices": [4]}, self.analysis,
                                           matched, [4], self.root / "mixed-outside-refill")
        self.assertTrue(result["ok"], result.get("refill_errors"))
        self.assertEqual(result["refill_replaced"], [4])
        self.assertEqual(result["segments"][:3], prior)
        self.assertEqual(result["selection_mode"], "rule_then_ai")


    def test_legacy_pair_and_motion_caches_do_not_poison_new_sampling_scope(self):
        import json
        members = next(groups.groups(self.plan["sentences"]))
        legacy_scope = contract.digest({"indices": [m["index"] for m in members],
            "sentences": members, "rules": contract.rules_identity(self.manifest)})[:24]
        self.plan["experiment_attempts"] = {legacy_scope: {
            str(i): {"status": "rejected"} for i in range(groups.PAIR_LIMIT)}}
        legacy_root = self.root / "experiments" / legacy_scope / "old-pair"
        legacy_root.mkdir(parents=True)
        old_pair = legacy_root / "pair.pair.json"
        old_motion = legacy_root / "events-complete_pour.motion.json"
        old_pair.write_text(json.dumps({"old_sampling": "8 frames"}))
        old_motion.write_text(json.dumps({"old_motion": True}))
        before = (old_pair.read_bytes(), old_motion.read_bytes())
        with patch.object(editor, "_place_absorb_experiment", return_value=False), \
             patch.object(groups, "observe_pair", return_value=(self.fixture.evidence, self.fixture.motion)) as observe, \
             patch.object(groups, "review_group", side_effect=self.observed_group):
            result = editor._repair_reviewed_rows(self.manifest, self.analysis, self.plan, self.root, None, set())
        self.assertTrue(result["ok"])
        self.assertEqual(observe.call_count, 1)
        self.assertNotEqual(observe.call_args.args[4].parent.name, legacy_scope)
        self.assertEqual((old_pair.read_bytes(), old_motion.read_bytes()), before)
        self.assertEqual(len(self.plan["experiment_attempts"][legacy_scope]), groups.PAIR_LIMIT)

    def test_frozen_group_resumes_without_new_observation(self):
        result = self.repair()
        with patch.object(groups, "observe_pair", side_effect=AssertionError("paid call forbidden")), \
             patch.object(groups, "review_group", side_effect=AssertionError("paid call forbidden")):
            self.assertFalse(groups.repair(self.manifest, self.analysis, self.plan, self.root, None))
            resumed = editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None, verify_sources=True)
        self.assertEqual(resumed, result)

    def test_topic_boundary_cannot_borrow_promotion_or_breathability_time(self):
        self.assertEqual([[m["index"] for m in g] for g in groups.groups(self.plan["sentences"])], [[1, 2]])
        self.plan["sentences"][1]["experiment_role"] = "breathability"
        with patch.object(groups, "observe_pair") as observe:
            self.assertFalse(groups.repair(self.manifest, self.analysis, self.plan, self.root, None))
        observe.assert_not_called()

    def test_single_row_refill_does_not_rewrite_accepted_neighbour(self):
        original = copy.deepcopy(self.plan["rows"])
        self.manifest["ai_refill_indices"] = [2]
        with patch.object(groups, "observe_pair") as observe:
            self.assertFalse(groups.repair(self.manifest, self.analysis, self.plan, self.root, None))
        self.assertEqual(self.plan["rows"], original); observe.assert_not_called()

    def test_unconfirmed_pair_stays_blocked_and_budget_survives_resume(self):
        with patch.object(groups, "observe_pair", side_effect=ValueError("EXPERIMENT_PAIR_NOT_CONFIRMED")) as observe:
            for _ in range(2):
                with self.assertRaisesRegex(ValueError, "EXPERIMENT_GROUP_PENDING"):
                    groups.repair(self.manifest, self.analysis, self.plan, self.root, None)
        self.assertEqual(observe.call_count, 1)
        self.assertFalse(any(r.get("continuity_key") for r in self.plan["rows"]))

    def test_wrong_sentence_phase_is_not_installed(self):
        review = self.observed_group
        def mismatch(*args):
            value = review(*args); value["members"][1]["verdict"] = "uncertain"; return value
        with patch.object(groups, "observe_pair", return_value=(self.fixture.evidence, self.fixture.motion)), \
             patch.object(groups, "review_group", side_effect=mismatch):
            with self.assertRaisesRegex(ValueError, "EXPERIMENT_GROUP_PENDING"):
                groups.repair(self.manifest, self.analysis, self.plan, self.root, None)
        self.assertFalse(any(r.get("continuity_key") for r in self.plan["rows"]))

    def test_changed_audio_source_allocation_or_product_cannot_reuse_group(self):
        self.repair(); plan = copy.deepcopy(self.plan)
        self.manifest["segments"][0]["audio_duration_us"] += 100_000
        with self.assertRaises(ValueError):
            editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None)
        self.manifest["segments"][0]["audio_duration_us"] -= 100_000
        self.plan["rows"][0]["parts"][0]["source_start_us"] += 1
        with self.assertRaisesRegex(ValueError, "CONTINUITY_INPUT_CHANGED"):
            editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None)
        self.plan = plan
        Path(self.shots[0]["video"]).write_bytes(b"changed source bytes")
        with self.assertRaises(ValueError):
            editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None)

    def clips(self, result):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.resolve = lambda value: Path(value)
        clips, cursor = [], 0
        for index, segment in enumerate(result["segments"][:2]):
            parent = Clip(segment["video"], segment["source_start"], 0, audio_mode="file", raw_index=index,
                          audio=segment["audio"])
            actual = engine._span_part_clips(index, parent, segment)
            self.assertIsNotNone(actual)
            for clip, cut in zip(actual, segment["span_parts"]):
                clip.start_us = cursor; clip.runtime_audio_us = cut["segment_us"]
                cursor += cut["segment_us"]; clips.append(clip)
        return clips

    def test_native_and_legacy_writers_share_exact_runtime_and_draft_readback(self):
        result = self.repair(); clips = self.clips(result)
        self.assertFalse(output_qc.runtime_errors(clips, result["segments"]))
        sources = [{"id": str(i), "path": c.video} for i, c in enumerate(clips)]
        draft = {"tracks": [{"type": "video", "segments": [{"material_id": str(i),
            "source_timerange": {"start": round(c.source_start * 1e6), "duration": round(c.duration * 1e6 * c.video_speed)},
            "target_timerange": {"start": c.start_us, "duration": round(c.duration * 1e6)}, "speed": c.video_speed}
            for i, c in enumerate(clips)]}], "materials": {"videos": sources}}
        with patch("orchestrator.runtime.draft_documents.documents", return_value={"timeline": draft}):
            self.assertFalse(output_qc.draft_errors(self.root, clips))
            draft["tracks"][0]["segments"][1]["speed"] += .1
            self.assertIn("CONTINUITY_DRAFT_CUT_CHANGED", output_qc.draft_errors(self.root, clips))
        clips[1].source_start += .01
        self.assertIn("CONTINUITY_RUNTIME_CHANGED", output_qc.runtime_errors(clips, result["segments"]))

    def test_independent_qc_batches_whole_group_and_checks_each_physical_cut(self):
        result = self.repair()
        claims = [{"index": i, "product": self.manifest["product"], "category": "weipin", "claim": row["text"],
            "ai_segment": row, "slots": len(row["span_parts"])} for i, row in enumerate(result["segments"][:2], 1)]
        self.assertEqual([len(batch) for batch in ai_editor_qc.claim_batches(claims, 1)], [2])
        self.assertIn("同次演示", ai_editor_qc.prompt(claims, 24))
        for claim in claims:
            actual = [{"source": p["video"], "source_start": p["source_start"],
                "source_duration": p["duration"], "target_duration": p["segment_s"]} for p in claim["ai_segment"]["span_parts"]]
            self.assertFalse(ai_editor_qc.validate_cut(claim, actual))
            actual[0]["target_duration"] += .01
            self.assertIn("CONTINUITY_QC_ALLOCATION_CHANGED", ai_editor_qc.validate_cut(claim, actual))

    def test_removing_group_or_physical_part_cannot_leave_certified_receipts(self):
        result = self.repair(); segment = copy.deepcopy(result["segments"][0])
        segment["span_parts"].pop()
        self.assertFalse(contract.validate_segment_receipt(segment, index=1, manifest=self.manifest)["ok"])
        segment = copy.deepcopy(result["segments"][0])
        segment.pop("continuity_group"); segment["ai_selection"].pop("continuity_group")
        self.assertFalse(contract.validate_segment_receipt(segment, index=1, manifest=self.manifest)["ok"])

    def test_event_observation_uses_configured_image_route_without_qwen_video(self):
        output = self.root / "default-events.json"
        sheet = self.root / "event-sheet.jpg"; sheet.write_bytes(b"frozen sheet")
        def observed(prompt, *_):
            return facts("A" if '"part": "A"' in prompt else "B")
        with patch.object(experiment_event_review, "_frame_sheet", return_value=sheet), \
             patch.object(editor, "_call", side_effect=observed), \
             patch.object(experiment_event_review, "window_video", side_effect=AssertionError("video route forbidden")), \
             patch.object(experiment_event_review.temporal_video, "review", side_effect=AssertionError("video route forbidden")):
            motion = experiment_event_review.review(self.manifest, self.fixture.evidence, output,
                font_path="unused", frames_only=True)
        self.assertEqual(len(motion["observations"]), 2)
        self.assertEqual(motion["method"], "actual_frame_events_with_dense_refinement")

    def test_same_source_can_use_the_existing_continuous_sentence_strategy(self):
        self.plan["sentences"][1]["candidate_ids"] = ["shot1"]
        self.plan["rows"][1]["parts"][0]["shot_id"] = "shot1"
        self.plan["reviews"]["2"] = copy.deepcopy(self.plan["reviews"]["1"])
        def reviewed(_manifest, context, _shot, _directory):
            review = {"verdict": "matched", "same_product": True, "same_demonstration": True,
                "same_selling_point": True, "semantic_continuity": True, "complete_action": True,
                "result_preserved": True, "confidence": .9,
                **{k: "actual source fact" for k in ("shared_point", "product_observation", "demonstration_observation", "observation", "reason")},
                "demonstration_observation": "倒液吸收后纸巾按压巾体并提起，纸面干燥",
                "complete_intervals_us": [context["window_us"]], "members": []}
            for member in context["members"]:
                review["members"].append({"index": member["index"], "verdict": "matched", "same_product": True,
                    "basis": "editorial_fit", "phase": "observed phase", "observation": "actual frame",
                    "reason": "supports sentence", "supported_point_ids": ["p1"], "time_sensitive": False,
                    "scene_ids": [], "evidence_intervals_us": [{"start": member["start_us"], "end": member["end_us"]}]})
            return {"schema": continuity.SCHEMA, "input": context, "review": review}
        with patch.object(editor, "_place_absorb_experiment", return_value=False), \
             patch.object(sentence_continuity, "review_group", side_effect=reviewed), \
             patch.object(groups, "observe_pair", side_effect=AssertionError("unneeded pair review")):
            result = editor._repair_reviewed_rows(self.manifest, self.analysis, self.plan, self.root, None, set())
        self.assertTrue(result["ok"])
        self.assertEqual(result["segments"][0]["continuity_group"]["schema"], continuity.SCHEMA)

    def test_qc_refill_rechecks_pair_but_freezes_accepted_neighbour_and_outside(self):
        matched = self.repair()
        prior = copy.deepcopy(matched["segments"])
        with patch.object(groups, "observe_pair", return_value=(self.fixture.evidence, self.fixture.motion)), \
             patch.object(groups, "review_group", side_effect=self.observed_group):
            result = editor.refill_indices({**self.manifest, "ai_refill_indices": [1]}, self.analysis,
                matched, [1], self.root / "refill")
        self.assertTrue(result["ok"], result.get("refill_errors"))
        self.assertEqual(result["refill_replaced"], [1])
        self.assertEqual(result["segments"][2], prior[2])
        cuts = lambda row: [(p["video"], p["source_start"], p["source_end"], p["segment_us"], p["video_speed"])
                            for p in row["span_parts"]]
        self.assertEqual(cuts(result["segments"][1]), cuts(prior[1]))
        self.assertFalse(contract.sequence_errors(result["segments"]))

    def test_failed_group_refill_cannot_fall_back_to_independent_sentence_match(self):
        matched = self.repair()
        with patch.object(groups, "observe_pair", side_effect=ValueError("EXPERIMENT_PAIR_NOT_CONFIRMED")), \
             patch.object(editor, "_review_pool", side_effect=AssertionError("unsafe individual fallback")):
            result = editor.refill_indices({**self.manifest, "ai_refill_indices": [1]}, self.analysis,
                matched, [1], self.root / "refill-failed")
        self.assertFalse(result["ok"])
        self.assertEqual(result["segments"], matched["segments"])

    def test_finite_pair_budget_covers_distinct_pour_hypotheses(self):
        for i in (4, 5):
            source = self.root / f"other-{i}.mp4"; source.write_bytes(f"other {i}".encode())
            self.shots.append({**self.shots[0], "shot_id": f"shot{i}", "video": str(source)})
        self.plan["sentences"][0]["candidate_ids"] = ["shot1", "shot4", "shot5"]
        self.plan["sentences"][1]["candidate_ids"] = ["shot2", "shot3"]
        with patch.object(groups, "observe_pair", side_effect=ValueError("EXPERIMENT_PAIR_NOT_CONFIRMED")) as observe:
            with self.assertRaisesRegex(ValueError, "EXPERIMENT_GROUP_PENDING"):
                groups.repair(self.manifest, self.analysis, self.plan, self.root, None)
        # PAIR_LIMIT 3 -> 6 (fix_rerun_20261109): all 3x2 hypotheses fit the budget; the
        # first three reviews still cover every distinct pour source.
        self.assertEqual(observe.call_count, min(groups.PAIR_LIMIT, 6))
        self.assertEqual({call.args[2]["shot_id"] for call in observe.call_args_list[:3]}, {"shot1", "shot4", "shot5"})

    def test_overlapping_observation_windows_are_refined_before_selected_source_check(self):
        from orchestrator.selection.experiment_contract import validate_pair_evidence
        from orchestrator.vision.experiment_pair_review import input_binding
        pair, motion = self.fixture.evidence, self.fixture.motion
        original = pair["members"][0]["samples"]["original"]
        second = pair["members"][1]
        second["samples"]["original"] = original
        (Path(second["samples"]["video"]["path"]).parent / "samples.json").write_text(
            json.dumps(second["samples"]), encoding="utf-8")
        second.update(window_frame_ids=[20, 80], visible_frame_ids=[20, 29, 37, 46, 54, 63, 71, 80])
        for p, span in zip([p for p in pair["observation"]["phases"] if p["part"] == "B"],
                           [(46, 54), (63, 63), (71, 80)]):
            p.update(first_frame=span[0], last_frame=span[1])
        self.fixture.pair.bind()
        motion["pair_input_sha256"] = contract.digest(input_binding(self.manifest, pair["members"], pair["sheet"]["path"]))
        b = motion["observations"][1]
        b["effective_window_frame_ids"] = [45, 80]
        spans = [("press", 45, 45, False), ("press", 46, 68, True), ("press", 69, 69, False),
                 ("lift", 70, 72, False), ("result", 73, 80, False)]
        b["segments"] = [{"kind": k, "first_frame": a, "last_frame": z, "can_speed": fast,
                          "observed": "actual event"} for k, a, z, fast in spans]
        self.shots[1]["video"] = original["path"]
        with self.assertRaisesRegex(ValueError, "SOURCE_ORDER_OR_REPLAY"):
            validate_pair_evidence(self.manifest, pair)
        context = groups._context(self.manifest, self.analysis, self.plan["sentences"][:2], None,
                                  pair, motion, ("shot1", "shot2"))
        self.assertLessEqual(context["members"][0]["parts"][-1]["end_us"],
                             context["members"][1]["parts"][0]["start_us"])
        for segment in b["segments"]:
            segment["first_frame"] -= 15; segment["last_frame"] -= 15
        b["effective_window_frame_ids"] = [30, 65]
        with self.assertRaisesRegex(ValueError, "SOURCE_REPLAY"):
            groups._context(self.manifest, self.analysis, self.plan["sentences"][:2], None,
                            pair, motion, ("shot1", "shot2"))
        continuity.source_chain([("same-source", 2, 3), ("same-source", 5, 6)], (("same-source", "same-source"),))
        with self.assertRaisesRegex(ValueError, "SOURCE_TIME_REVERSED"):
            continuity.source_chain([("same-source", 5, 6), ("same-source", 2, 3)], (("same-source", "same-source"),))

    def test_real_pcm_audio_aligns_once_per_sentence_across_all_phase_parts(self):
        result = self.repair()
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = result["manifest"]; engine.sync_issues = []; engine._clips = []
        engine.resolve = lambda value: Path(value)
        engine._event = lambda *args, **kwargs: None
        engine._probe_material = lambda clip: 10.0
        def probe_audio(path):
            with wave.open(str(path), "rb") as audio:
                return {"duration": audio.getnframes() / audio.getframerate()}
        engine.probe = probe_audio
        for index, segment in enumerate(result["segments"][:2]):
            audio = self.root / f"narration-{index}.wav"
            with wave.open(str(audio), "wb") as stream:
                stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(48000)
                stream.writeframes(b"\0\0" * 110400)
            parent = Clip(segment["video"], segment["source_start"], 0, text=segment["text"],
                          audio_mode="file", raw_index=index, audio=str(audio))
            parts = engine._span_part_clips(index, parent, segment)
            engine._clips.extend(engine._span_clip(index, n, parent, p, segment)
                                 for n, p in enumerate(parts, 1))
        engine.align_audio_video(engine._clips)
        self.assertEqual(engine.sync_issues, [])
        self.assertEqual(engine.total_us, 4_600_000)
        self.assertFalse(output_qc.runtime_errors(engine._clips, result["segments"]))
        for index, origin in ((0, 0), (1, 2_300_000)):
            parts = [c for c in engine._clips if c.raw_index == index]
            self.assertEqual({c.audio_clip_start_us for c in parts}, {origin})
            self.assertEqual(sum(round(c.duration * 1e6) for c in parts), 2_300_000)


if __name__ == "__main__":
    unittest.main()
