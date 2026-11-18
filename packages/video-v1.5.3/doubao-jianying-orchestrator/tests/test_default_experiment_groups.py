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

    def test_known_experiment_is_tried_before_unrelated_shortlisted_pairs(self):
        from orchestrator.vision import experiment_evidence
        with patch.object(experiment_evidence, "preferred_pairs", return_value=[("shot2", "shot3")]), \
             patch.object(groups, "observe_pair", side_effect=ValueError("no valid evidence")) as observe:
            with self.assertRaisesRegex(ValueError, "EXPERIMENT_GROUP_PENDING"):
                groups.repair(self.manifest, self.analysis, self.plan, self.root, None)
        self.assertGreater(observe.call_count, 0)
        self.assertEqual([observe.call_args_list[0].args[2]["shot_id"],
                          observe.call_args_list[0].args[3]["shot_id"]], ["shot2", "shot3"])

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

    def test_review_cache_depends_on_window_and_evidence_independently_of_cap(self):
        from PIL import Image
        source = contract.source_identity(self.shots[0]["video"])
        cut = {"start_us": 0, "end_us": 100000, "target_us": 100000, "source_identity": source}
        context = {"members": [{"index": 1, "parts": [cut]}], "evidence_digest": "original",
                   "task_absorb_speed_cap": 2}
        tile = self.root / "tile.jpg"
        Image.new("RGB", (10, 10)).save(tile)
        with patch.object(editor, "_review_media", return_value={"before": source, "sheet": tile}), \
             patch.object(editor, "_call") as call:
            def request():
                groups.review_group(self.manifest, context, self.root)
                return call.call_args.args[:2]
            original = request()
            context["task_absorb_speed_cap"] = 3
            self.assertEqual(request(), original)
            context["evidence_digest"] = "different observation"
            self.assertNotEqual(request(), original)
            context["evidence_digest"] = "original"
            cut["end_us"] += 1
            self.assertNotEqual(request(), original)

    def test_exact_paired_cut_obeys_phase_and_task_speed_limits(self):
        from orchestrator.selection import timing_contract as timing
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = {**self.manifest, "max_absorb_demo_speed": 2}
        start, end, target = 123457, 1987654, 1000000
        clip = Clip(video="source", text="吸收", source_start=start / 1e6,
            source_end=end / 1e6, video_duration=10, duration=1, guard_override=[0, 0],
            continuity_group={"schema": continuity.EXPERIMENT_SCHEMA}, stable_start=0.8,
            playback_observation={"schema": contract.playback.OBSERVATION_SCHEMA, "kind": "process",
                "observed": "continuous absorption", "start_us": start, "end_us": end})
        def solve():
            return timing.solve_window(engine._timing_request(clip, target))
        fitted = solve()
        self.assertTrue(fitted.ok, fitted.to_dict())
        self.assertEqual((fitted.source_start_us, fitted.source_end_us), (start, end))
        self.assertEqual(round(fitted.speed * target), end - start)
        engine.m["max_absorb_demo_speed"] = 1.5
        self.assertFalse(solve().ok)
        engine.m["max_absorb_demo_speed"] = 10
        clip.time_sensitive = True
        self.assertFalse(solve().ok)
        clip.time_sensitive = False
        clip.playback_observation["kind"] = "experiment_unbounded"
        self.assertFalse(solve().ok)
        clip.playback_observation["kind"] = "process"
        target = 500000
        self.assertFalse(solve().ok)

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

    def test_reviewed_group_ignores_legacy_cap_and_qc_binds_actual_speed_material(self):
        self.manifest["max_absorb_demo_speed"] = 3.0
        result = self.repair()
        for index, row in enumerate(result["segments"][:2], 1):
            self.assertNotIn("task_absorb_speed_cap", row["ai_selection"])
            self.assertTrue(all("task_absorb_speed_cap" not in p["ai_selection"] for p in row["span_parts"]))
            missing = {k: v for k, v in self.manifest.items() if k != "max_absorb_demo_speed"}
            self.assertTrue(contract.validate_segment_receipt(row, index=index, manifest=missing)["ok"])
            actual = [{"source": p["video"], "source_start": p["source_start"], "source_duration": p["duration"],
                "target_duration": p["segment_s"], "segment": {"speed": p["video_speed"]},
                "speed_materials": [{"type": "speed", "speed": p["video_speed"]}]} for p in row["span_parts"]]
            claim = {"index": index, "product": self.manifest["product"], "category": "weipin",
                "ai_segment": row, "max_absorb_demo_speed": 3.0}
            self.assertFalse(ai_editor_qc.validate_cut(claim, actual))
            claim.pop("max_absorb_demo_speed")
            self.assertFalse(ai_editor_qc.validate_cut(claim, actual))
            actual[0]["speed_materials"] = []
            self.assertIn("AI_QC_FINAL_SPEED_MATERIAL_MISSING", ai_editor_qc.validate_cut(claim, actual))

    def test_default_all_stages_can_exceed_old_limits_and_native_plan_binds_the_cut(self):
        from orchestrator.application import headless_production
        self.manifest["max_absorb_demo_speed"] = 2
        self.manifest["segments"][0]["audio_duration_us"] = 400_000
        result = self.repair()
        parts = [p for s in result["segments"][:2] for p in s["span_parts"]]
        self.assertEqual([p["playback_observation"]["phase"] for p in parts], ["pour", "absorb", "press", "lift", "result"])
        self.assertTrue(all(p["video_speed"] > 1 for p in parts))
        self.assertGreater(parts[0]["video_speed"], 20)
        self.assertGreaterEqual(parts[-1]["segment_us"] * 30, 15_000_000)
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = result["manifest"]
        clips = self.clips(result)
        for clip in clips:
            clip.audio_mode = "native"
        plan, _, _ = headless_production.make_plan(engine, clips, None, {}, [], "fixture", None)
        actual = plan["tracks"][0]["segments"]
        self.assertTrue(actual[0]["authorized_experiment_phase_speedup"])
        self.assertEqual([v["speed"] for v in actual], [p["video_speed"] for p in parts])
        clips[0].source_start += .05
        with self.assertRaisesRegex(ValueError, "HEADLESS_EXPERIMENT_CUT_CHANGED"):
            headless_production.make_plan(engine, clips, None, {}, [], "fixture", None)
        clips[0].continuity_group = None
        with self.assertRaisesRegex(ValueError, "HEADLESS_EXPERIMENT_SPEED_AUTHORIZATION_MISSING"):
            headless_production.make_plan(engine, clips, None, {}, [], "fixture", None)

    def test_output_stage_removal_or_crop_is_rejected(self):
        original = self.repair()["segments"][0]["continuity_group"]
        for missing in ("pour", "absorb", "press", "lift", "result"):
            group = copy.deepcopy(original)
            part = next(p for m in group["input"]["members"] for p in m["parts"] if p["experiment_phase"] == missing)
            part["experiment_phase"] = "context"
            with self.assertRaisesRegex(ValueError, "COMPLETE_CHAIN_REQUIRED"):
                continuity.validate(group)
        group = copy.deepcopy(original)
        cut = group["input"]["members"][0]["parts"][0]
        fact = group["review"]["members"][0]["parts"][0]
        cut["start_us"] += 100_000
        fact["evidence_intervals_us"][0]["start"] = cut["start_us"]
        with self.assertRaisesRegex(ValueError, "PHASE_WINDOW_CHANGED"):
            continuity.validate(group)

    def test_short_single_claim_still_needs_a_complete_group(self):
        for role in ("absorption", "rewet"):
            sentences = [{**self.plan["sentences"][0], "experiment_role": role}]
            with self.assertRaisesRegex(ValueError, "three_visible_stages_required"):
                groups.require_complete(sentences, [{}])
            context = groups._context(self.manifest, self.analysis, sentences, None,
                self.fixture.evidence, self.fixture.motion, ("shot1", "shot2"))
            self.assertEqual([p["experiment_phase"] for p in context["members"][0]["parts"]],
                             ["pour", "absorb", "press", "lift", "result"])
        for row in ({"text": "倒蓝水看它吸收完", "experiment_role": "promotion"},
                    {"text": "看看这个效果", "experiment_role": "breathability", "scene_ids": ["absorb"]}):
            with self.assertRaisesRegex(ValueError, "three_visible_stages_required"):
                groups.require_complete([{"index": 1, **row}], [{}])
        from orchestrator import rulepack
        from orchestrator.policy import scene_rules
        for text in ("吸水层的结构", "吸水层很柔软", "网面透气", "荧光检测"):
            self.assertEqual(list(groups.groups([{"index": 1, "text": text}])), [])
            rule_groups = scene_rules._split_groups([{"text": text, "video": "fixture.mp4"}],
                                                   rulepack.load("weipin"), {}, category="weipin")
            self.assertFalse(any(g["scene"]["id"] == "absorb_test" for g in rule_groups))

    def test_independent_group_observations_can_span_sentences_but_must_cover_tissue_result(self):
        from orchestrator.visual_qc_contract import group_events_visible
        members = [{"index": 1, "parts": [{"experiment_phase": p} for p in ("pour", "absorb")]},
                   {"index": 2, "parts": [{"experiment_phase": "press"}]},
                   {"index": 3, "parts": [{"experiment_phase": p} for p in ("lift", "result")]}]
        group = {"input": {"members": members}}
        def event(name, *frames):
            return {"event": name, "frames": list(frames)}
        rows = {1: {"visual_ok": True, "visible_experiment_events": [event("pour", 1, 2), event("absorb", 3, 4)],
                    "experiment_output_frames": [[1, 0], [1, 1], [2, 3], [2, 10]], "observed": "蓝水倒入并被吸收"},
                2: {"visual_ok": True, "visible_experiment_events": [event("press", 1)],
                    "experiment_output_frames": [[1, 20]], "observed": "纸巾按压巾面"},
                3: {"visual_ok": True, "visible_experiment_events": [event("lift", 1), event("result", 2, 3)],
                    "experiment_output_frames": [[1, 22], [2, 25], [2, 39]], "observed": "纸巾被提起，纸面干净"}}
        for invalid in ([1], [1, 1], [1, 3], [1, 99]):
            rows[1]["visible_experiment_events"][0]["frames"] = invalid
            self.assertFalse(group_events_visible(group, rows))
        rows[1]["visible_experiment_events"][0]["frames"] = [1, 2]
        self.assertTrue(group_events_visible(group, rows))
        for description in ("纸巾被手指按压在巾面上", "纸巾放在巾面上，手指轻轻按压"):
            rows[2]["observed"] = description
            self.assertTrue(group_events_visible(group, rows))
        rows[3]["observed"] = "手指提起这张纸巾，纸面看得清"
        self.assertTrue(group_events_visible(group, rows))
        rows[2]["observed"] = "手掌按压巾体，旁边有纸巾"
        self.assertFalse(group_events_visible(group, rows))
        rows[2]["observed"] = "纸巾按压巾面"
        rows[3]["experiment_output_frames"][-1] = [2, 26]
        self.assertFalse(group_events_visible(group, rows))
        rows[3]["visible_experiment_events"].pop()
        self.assertFalse(group_events_visible(group, rows))

    def test_fast_cut_is_reviewed_only_on_actual_output_ticks(self):
        group = {"schema": continuity.EXPERIMENT_SCHEMA}
        claim = {"ai_segment": {"continuity_group": group}}
        cut = {"source_start": 0, "source_duration": 5, "target_start": .01, "target_duration": .066667}
        plan = ai_editor_qc.frame_plan(claim, [cut], fps=30)
        self.assertEqual(ai_editor_qc.experiment_output_frames([{"suffix": label} for _, label, _ in plan]),
                         [[1, 1], [1, 2]])
        for (_, label, fraction), tick in zip(plan, (1, 2)):
            self.assertAlmostEqual(cut["target_start"] + fraction * cut["target_duration"], tick / 30)

    def test_rule_selection_uses_the_same_review_for_its_group(self):
        originals = self.manifest["segments"]
        rows = [{**m, "selection_mode": "rule", "video": s["video"], "temporary_shot_id": s["shot_id"]}
                for m, s in zip(originals, self.shots)]
        for row, status in zip(rows[:2], ("unmet", "partial")):
            row["scene_rule"] = {"id": "absorb_test", "group": [1, 2], "status": status,
                                 "reason": "SCENE_ACTION_INCOMPLETE"}
        rows[0].update(scene_preview_unresolved=True, preview_unresolved=True)
        rows[1].update(visual_missing=True, preview_unresolved=True, visual_missing_reason_code="SCENE_ACTION_INCOMPLETE")
        rows[2]["scene_rule"] = {"id": "other", "status": "unmet"}
        matched = {"ok": True, "manifest": {**self.manifest, "segments": rows}, "segments": rows}
        cfg = {**matched["manifest"], "selection_mode": "rule_then_ai"}
        with patch.object(groups, "observe_pair", return_value=(self.fixture.evidence, self.fixture.motion)), \
                patch.object(groups, "review_group", side_effect=self.observed_group):
            groups.certify_rule_groups(cfg, self.analysis, matched, self.root, None)
        self.assertEqual(matched["selection_mode"], "rule_then_ai")
        self.assertTrue(continuity.is_experiment(matched["segments"][0]["continuity_group"]))
        self.assertEqual(matched["segments"][2]["selection_mode"], "rule")
        for segment in matched["segments"][:2]:
            self.assertIsNone(segment.get("scene_rule"))
            self.assertFalse(segment.get("visual_missing"))
            self.assertFalse(segment.get("preview_unresolved"))
        self.assertEqual(matched["segments"][2]["scene_rule"], {"id": "other", "status": "unmet"})
        # Historical matches may already have a valid receipt and an obsolete placeholder.
        cached = matched["segments"][0]
        cached.update(visual_missing=True, visual_missing_reason_code="SCENE_ACTION_INCOMPLETE",
                      preview_unresolved=True)
        gaps = [{"index": 1, "source": "scene_rules"}, {"index": 3, "source": "scene_rules"}]
        matched.update(visual_missing_matches=list(gaps), visual_missing_count=2)
        matched["manifest"]["visual_missing_matches"] = list(gaps)
        previews = [{"index": 1, "type": "SCENE_RELATED_PREVIEW_UNCERTIFIED"},
                    {"index": 1, "type": "OTHER_PREVIEW_REASON"}]
        matched.update(preview_unresolved=previews[:], preview_unresolved_count=2)
        matched["manifest"]["preview_unresolved"] = previews[:]
        with patch.object(groups, "observe_pair", side_effect=AssertionError("already reviewed")):
            groups.certify_rule_groups(matched["manifest"], self.analysis, matched, self.root, None)
        self.assertFalse(matched["segments"][0].get("visual_missing"))
        self.assertFalse(matched["segments"][0].get("preview_unresolved"))
        self.assertEqual(matched["visual_missing_count"], 1)
        self.assertEqual(matched["visual_missing_matches"], [gaps[1]])
        self.assertEqual(matched["manifest"]["visual_missing_matches"], [gaps[1]])
        self.assertEqual(matched["preview_unresolved_count"], 1)
        self.assertEqual(matched["preview_unresolved"], [previews[1]])
        self.assertEqual(matched["manifest"]["preview_unresolved"], [previews[1]])

    def test_direct_legacy_match_and_recovery_reject_unverified_single_experiment_shot(self):
        from argparse import Namespace
        from orchestrator.application import cli
        from orchestrator.vision import material_cache
        source = {**self.manifest, "selection_mode": "legacy", "segments": [self.manifest["segments"][0]]}
        input_path = self.root / "input.json"
        analysis_path = self.root / "analysis.json"
        output = self.root / "match"
        for path, data in [(input_path, source), (analysis_path, self.analysis)]:
            path.write_text(json.dumps(data), encoding="utf-8")
        row = {**source["segments"][0], "video": self.shots[0]["video"],
               "temporary_shot_id": self.shots[0]["shot_id"], "selection_mode": "rule"}
        initial = {"ok": True, "segments": [row], "manifest": {**source, "segments": [row]}}
        args = Namespace(input=str(input_path), analysis=str(analysis_path), report_dir=str(output),
                         topk=3, require_selection=False)
        with patch.object(cli, "environment_report", return_value={}), \
                patch.object(material_cache, "refresh_external_analysis", return_value=self.analysis), \
                patch.object(cli.shot_analyzer, "match_manifest", return_value=initial), \
                patch.object(groups, "observe_pair", side_effect=ValueError("missing tissue verification")), \
                patch.object(cli, "_emit", side_effect=lambda result, code: code):
            self.assertEqual(cli.cmd_match_shots(args), 1)
            saved = json.loads((output / "shot_match_report.json").read_text(encoding="utf-8"))
            self.assertFalse(saved["ok"])
            self.assertEqual(saved["pending_items"][-1]["type"], "EXPERIMENT_GROUP_PENDING")
            recovery = Namespace(input=str(input_path), report_dir=str(output), stage="draft_write")
            with patch.object(cli, "_production_environment_report", return_value={"ready": True}), \
                    patch.object(cli.OrchestrationEngine, "write_draft") as writer:
                self.assertEqual(cli.cmd_recover(recovery), 1)
                writer.assert_not_called()

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
        self.plan["sentences"][1].update(text="网面透气", experiment_role="breathability")
        self.assertEqual([[m["index"] for m in g] for g in groups.groups(self.plan["sentences"])], [[1]])
        with patch.object(groups, "observe_pair", side_effect=ValueError("no complete same experiment")):
            with self.assertRaisesRegex(ValueError, "EXPERIMENT_GROUP_PENDING:1"):
                groups.repair(self.manifest, self.analysis, self.plan, self.root, None)

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
                "source_duration": p["duration"], "target_duration": p["segment_s"],
                "segment": {"speed": p["video_speed"]},
                "speed_materials": [{"type": "speed", "speed": p["video_speed"]}]}
                for p in claim["ai_segment"]["span_parts"]]
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

    def test_event_images_require_requested_coverage_in_initial_and_dense_reviews(self):
        sheet = self.root / "coverage.jpg"
        sheet.write_bytes(b"fixture")
        for wrong_call in (1, 2):
            with self.subTest(wrong_call=wrong_call):
                responses = [facts("A") for _ in range(wrong_call)]
                responses[-1]["coverage"] = "absorption_focus"
                with patch.object(experiment_event_review, "_frame_sheet", return_value=sheet), \
                     patch.object(editor, "_call", side_effect=responses) as call:
                    with self.assertRaisesRegex(ValueError, "EXPERIMENT_EVENTS_COVERAGE_CHANGED"):
                        experiment_event_review.review(self.manifest, self.fixture.evidence,
                            self.root / f"coverage-{wrong_call}.json", focus="complete_pour",
                            frames_only=True, font_path="unused")
                    self.assertEqual(call.call_count, wrong_call)

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

    def test_single_source_cannot_substitute_an_ordinary_continuity_review(self):
        self.plan["sentences"][1]["candidate_ids"] = ["shot1"]
        self.plan["rows"][1]["parts"][0]["shot_id"] = "shot1"
        self.plan["reviews"]["2"] = copy.deepcopy(self.plan["reviews"]["1"])
        with patch.object(groups, "observe_pair", side_effect=ValueError("wipe missing")) as observe:
            with self.assertRaisesRegex(ValueError, "EXPERIMENT_GROUP_PENDING"):
                groups.repair(self.manifest, self.analysis, self.plan, self.root, None)
        self.assertTrue(any(call.args[2]["shot_id"] == call.args[3]["shot_id"] for call in observe.call_args_list))

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
