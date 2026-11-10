"""Queue decisions remain usable without initializing the production worker."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.queueing import contracts


class QueueContractTests(unittest.TestCase):
    def test_import_does_not_initialize_worker_or_orchestrator(self):
        result = subprocess.run(
            [sys.executable, "-X", "utf8", "-c",
             "import json,sys; from tools.queueing import contracts; "
             "print(json.dumps([n for n in sys.modules "
             "if n == 'batch_worker' or n.startswith('orchestrator')]))"],
            cwd=ROOT, text=True, encoding="utf-8", capture_output=True,
            check=True)
        self.assertEqual(json.loads(result.stdout), [])

    def test_credentials_override_retryable_network_signal(self):
        classified = contracts.classify_build_failure(
            "VISION_CALL_FAILED: 503 network; unauthorized invalid api key")
        self.assertFalse(classified["transient"])
        self.assertTrue(classified["deterministic_block"])
        self.assertIn("503", classified["markers"])

    def test_nonretryable_stage_failure_survives_draft_log_paths(self):
        for kind, code in (("timing", "TIMING_INFEASIBLE"), ("semantic", "EXPERIMENT_GROUP_UNCONFIRMED"),
                           ("vision", "VISION_CALL_FAILED")):
            with self.subTest(kind=kind):
                result = contracts.batch_result_contract("ERROR", pipeline_version="test-runtime", run_id="r",
                    failures=[{"type": "transient", "draft": "example", "message": "build rc=1 log=draft.log timeout",
                               "retryable": False, "resume_stage": "semantic_match",
                               "failure_class": kind, "failure_code": code}])
                self.assertEqual((result["failure_class"], result["failure_code"]), (kind, code))
                self.assertFalse(result["retryable"])
                self.assertFalse(result["safe_to_continue"])

    def test_structured_failure_does_not_override_changed_source_guard(self):
        result = contracts.batch_result_contract("ERROR", pipeline_version="test-runtime", run_id="r",
            failures=[{"message": "MATERIAL_SOURCE_CHANGED_DURING_PREPARATION timeout", "retryable": True,
                       "resume_stage": "semantic_match", "failure_class": "vision", "failure_code": "VISION_CALL_FAILED"}])
        self.assertEqual(result["failure_code"], "MATERIAL_DURATION_UNAVAILABLE")
        self.assertFalse(result["retryable"])

    def test_network_retry_does_not_certify_task(self):
        classified = contracts.classify_build_failure(
            "VISION_CALL_FAILED: 503 network timeout")
        self.assertTrue(classified["transient"])
        result = contracts.batch_result_contract(
            "ERROR", pipeline_version="test-runtime", run_id="run-test",
            failures=[{"type": "transient", "error": "VISION_CALL_FAILED 503 network timeout"}])
        self.assertEqual(result["terminal_state"], "ERROR")
        self.assertEqual(result["failure_class"], "vision")
        self.assertTrue(result["retryable"])
        self.assertFalse(result["safe_to_continue"])

    def test_preview_and_partial_never_become_success(self):
        for status in ("PREVIEW_READY", "PARTIAL"):
            with self.subTest(status=status):
                result = contracts.batch_result_contract(
                    status, pipeline_version="test-runtime", run_id="run-test")
                self.assertEqual(result["pipeline_version"], "test-runtime")
                self.assertEqual(result["terminal_state"], "UNCERTIFIED")
                self.assertEqual(result["next_action"], "manual_review")
                self.assertFalse(result["safe_to_continue"])

    def test_deterministic_visual_failure_requires_review(self):
        result = contracts.batch_result_contract(
            "ERROR", pipeline_version="test-runtime", run_id="run-test",
            error="VISUAL_MATCH_BLOCKED: missing evidence")
        self.assertEqual(result["terminal_state"], "BLOCKED")
        self.assertEqual(result["failure_class"], "semantic")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["safe_to_continue"])

    def test_fixed_input_change_cannot_retry_as_a_new_build(self):
        for reason in ("FIXED_NARRATION_CHANGED_DURING_BUILD", "SELECTION_AUDIO_CHANGED",
                       "BGM_CHANGED_DURING_BUILD", "BGM_SOURCE_AUDIO_INVALID", "MATERIAL_SOURCE_LIST_INVALID"):
            with self.subTest(reason=reason):
                classified = contracts.classify_build_failure(reason)
                self.assertTrue(classified["deterministic_block"])
                result = contracts.batch_result_contract("ERROR", pipeline_version="test-runtime", run_id="run-test",
                    failures=[{"type": "transient", "message": reason + " network timeout"}])
                self.assertEqual(result["failure_class"], "environment")
                self.assertFalse(result["retryable"])

    def test_unconfirmed_material_bound_is_a_deterministic_environment_block(self):
        for reason in ("MATERIAL_DURATION_BOUND_UNCONFIRMED", "MATERIAL_DURATION_PROBE_FAILED",
                       "MATERIAL_DURATION_PROBE_REQUIRED", "MATERIAL_DURATION_RECHECK_FAILED",
                       "MATERIAL_GEOMETRY_REQUIRES_REANALYSIS", "MATERIAL_SOURCE_CHANGED_DURING_PREPARATION"):
            with self.subTest(reason=reason):
                message = reason + " SHOT_MATCH_BLOCKED network timeout"
                classification = contracts.classify_build_failure(message)
                self.assertTrue(classification["deterministic_block"])
                self.assertFalse(classification["transient"])
                result = contracts.batch_result_contract("ERROR", pipeline_version="test-runtime", run_id="run-test",
                    failures=[{"type": "transient", "message": message}])
                self.assertEqual((result["terminal_state"], result["failure_class"], result["failure_code"]),
                                 ("BLOCKED", "environment", "MATERIAL_DURATION_UNAVAILABLE"))
                self.assertFalse(result["retryable"])

    def test_queue_target_requires_unique_record_and_does_not_mutate(self):
        queue = [{"record_id": "first"}, {"record_id": "second"}]
        original = copy.deepcopy(queue)
        selected = contracts.select_queue_for_single_run(
            queue, record_id="second", force_rebuild=True)
        self.assertEqual(selected, [{"record_id": "second", "force_rebuild": True}])
        self.assertEqual(queue, original)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            contracts.select_queue_for_single_run(
                [{"record_id": "same"}, {"record_id": "same"}], record_id="same")
        with self.assertRaisesRegex(ValueError, "exact record_id"):
            contracts.select_queue_for_single_run(queue, force_rebuild=True)

    def test_partial_resume_is_explicit_input(self):
        selected = contracts.select_queue_for_single_run(
            [{"record_id": "first", "retry_error": True}],
            record_id="first", force_rebuild=True, resume_partial=True)
        self.assertEqual(selected, [{"record_id": "first", "retry_error": False,
                                     "force_rebuild": False, "retry_deferred": True}])

    def test_parallel_rows_must_have_distinct_titles_and_materials(self):
        left = {"title": "Paper", "material_dir": "pool-a"}
        self.assertFalse(contracts.parallel_pair_safe(
            left, {"title": " paper ", "material_dir": "pool-b"}))
        self.assertFalse(contracts.parallel_pair_safe(
            left, {"title": "Tissue", "material_dir": "POOL-A"}))
        self.assertFalse(contracts.parallel_pair_safe(left, {"title": "Tissue"}))
        self.assertTrue(contracts.parallel_pair_safe(
            left, {"title": "Tissue", "material_dir": "pool-b"}))

    def test_explicit_bgm_input_never_turns_into_random_selection(self):
        self.assertEqual(contracts.meeting_bgm_spec("song-id"), {"music_id": "song-id"})
        self.assertEqual(contracts.meeting_bgm_spec(r"C:\audio\song.mp3"),
                         {"path": r"C:\audio\song.mp3"})
        value = {"music_id": "song-id"}
        self.assertEqual(contracts.meeting_bgm_spec(value), value)
        self.assertIsNot(contracts.meeting_bgm_spec(value), value)

    def test_completed_negative_observation_is_reusable_but_not_invented(self):
        rejected = {"description": "No usable action", "visual_tags": ["paper"],
                    "evidence_tags": [], "analysis_confidence": 0.9,
                    "visual_observation_complete": True, "action_complete": False,
                    "status": "pending_analysis"}
        self.assertTrue(contracts.is_reusable_analysis_row(rejected))
        self.assertFalse(contracts.is_reusable_analysis_row(
            {**rejected, "visual_observation_complete": False}))

    def test_worker_wrapper_preserves_runtime_and_marker_patch_points(self):
        import batch_worker
        with patch.object(batch_worker, "_pipeline_version", return_value="patched-runtime"):
            result = batch_worker._batch_result_contract("OK", run_id="run-test")
        self.assertEqual(result["pipeline_version"], "patched-runtime")
        with patch.object(batch_worker, "TRANSIENT_BUILD_MARKERS", ("custom",)):
            result = batch_worker.classify_build_failure("custom network timeout")
        self.assertEqual(result["markers"], ["custom"])
        self.assertTrue(result["transient"])
        with patch.dict(os.environ, {"JY_TASK_RESUME_PARTIAL": "1"}):
            selected = batch_worker.select_queue_for_single_run(
                [{"record_id": "first"}], record_id="first", force_rebuild=True)
        self.assertTrue(selected[0]["retry_deferred"])
        self.assertFalse(selected[0]["force_rebuild"])


class QCRejectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        paths = patch.object(sys, "path", [str(ROOT / "doubao-jianying-orchestrator"), *sys.path])
        paths.start()
        self.addCleanup(paths.stop)
        import batch_worker
        self.worker = batch_worker
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"fixture media identity")
        self.manifest = {"record_id": "fixture-record", "full_script": "First sentence.",
                         "product": "fixture", "category": "fixture",
                         "material_sources": [str(self.source)]}
        self.report_dir = self.root / "report"
        self.report_dir.mkdir()

    def latest_failure(self, shot, *, claim="First sentence.", manifest=None, source=None):
        manifest = copy.deepcopy(self.manifest if manifest is None else manifest)
        source = source or self.worker.qc_rejection_identity(manifest)["sources"][0]
        match = {"input_manifest": manifest, "segments": [{
            "text": "First sentence.", "shot_id": shot,
            "ai_selection": {"source_identity": source}}]}
        (self.report_dir / "shot_match_report.json").write_text(json.dumps(match), encoding="utf-8")
        qc = {"status": "FAIL", "model_review": [{"index": 1, "claim": claim, "visual_ok": False, "match_level": "MISMATCH"}]}
        (self.report_dir / "draft_visual_qc.json").write_text(json.dumps(qc), encoding="utf-8")
        return qc

    def test_nonsemantic_qc_issues_never_blacklist_a_source(self):
        for level in ("INSUFFICIENT_EVIDENCE", "ACCEPTABLE_DEGRADED", "MATERIAL_GAP"):
            with self.subTest(level=level):
                report = self.latest_failure("shot-A")
                report["status"] = "PASS_WITH_DEGRADED"
                report["model_review"][0]["match_level"] = level
                self.assertEqual(self.worker.qc_rejected_shots_from_report(self.report_dir, report), {})

    def test_qc_feedback_requires_validated_draft_claim_and_bound_source(self):
        self.latest_failure('shot-A')
        identity = self.worker.qc_rejection_identity(self.manifest)['sources'][0]
        row = {'index': 1, 'claim': 'First sentence.', 'match_level': 'ACCEPTABLE_DEGRADED',
               'visual_ok': True, 'observed': 'visible package', 'reason': 'requested action is absent', 'confidence': .9}
        report = {'draft': str(self.root / 'draft'), 'response_contract': {'status': 'VALIDATED', 'errors': []},
                  'source_bindings': [{'identity': identity}], 'model_review': [row],
                  'segment_map': [{'claim_index': 1, 'draft_segments': [1]}]}
        with patch.object(self.worker.production_eval.visual_qc_contract, 'report_matches_draft', return_value=True), \
             patch('orchestrator.quality.draft_visual_qc._load_json', return_value={}), \
             patch('orchestrator.quality.draft_visual_qc._timeline_items', return_value=([{'index': 1, 'source': str(self.source)}], [])), \
             patch('orchestrator.media.source_normalization.read_prepared_sources', return_value=[]), \
             patch('orchestrator.quality.ai_editor_qc.source_matches_receipt', return_value=True) as source_match:
            result = self.worker.qc_refill_feedback_from_report(self.report_dir, report, {1}, expected_sources=[identity])
            self.assertEqual(result['1']['reason'], 'requested action is absent')
            self.assertEqual(result['1']['prior_shot_ids'], ['shot-A'])
            self.assertEqual(self.worker.qc_refill_feedback_from_report(
                self.report_dir, {**report, 'model_review': [{**row, 'claim': 'different'}]}, {1}, expected_sources=[identity]), {})
            self.assertEqual(self.worker.qc_refill_feedback_from_report(
                self.report_dir, {**report, 'source_bindings': []}, {1}, expected_sources=[identity]), {})
            source_match.return_value = False
            self.assertEqual(self.worker.qc_refill_feedback_from_report(self.report_dir, report, {1}, expected_sources=[identity]), {})
        with patch.object(self.worker.production_eval.visual_qc_contract, 'report_matches_draft', return_value=False):
            self.assertEqual(self.worker.qc_refill_feedback_from_report(self.report_dir, report, {1}, expected_sources=[identity]), {})

    def test_localized_media_copy_binds_when_qc_reviewed_that_copy(self):
        original = self.worker.qc_rejection_identity(self.manifest)['sources'][0]
        copy = self.root / 'draft' / 'media' / 'source_copy.mp4'
        copy.parent.mkdir(parents=True)
        copy.write_bytes(self.source.read_bytes())
        copy_identity = self.worker.production_eval.visual_qc_contract._source_identity(str(copy))
        self.latest_failure('shot-A', source=original)
        row = {'index': 1, 'claim': 'First sentence.', 'match_level': 'MISMATCH',
               'visual_ok': False, 'observed': 'lab equipment', 'reason': 'object mismatch', 'confidence': .8}
        report = {'draft': str(self.root / 'draft'), 'response_contract': {'status': 'VALIDATED', 'errors': []},
                  'source_bindings': [{'path': str(copy), 'identity': copy_identity}],
                  'model_review': [row], 'segment_map': [{'claim_index': 1, 'draft_segments': [1]}]}
        other = self.root / 'other.mp4'
        other.write_bytes(self.source.read_bytes())
        with patch.object(self.worker.production_eval.visual_qc_contract, 'report_matches_draft', return_value=True), \
             patch('orchestrator.quality.draft_visual_qc._load_json', return_value={}), \
             patch('orchestrator.quality.draft_visual_qc._timeline_items', return_value=([{'index': 1, 'source': str(copy)}], [])), \
             patch('orchestrator.media.source_normalization.read_prepared_sources', return_value=[]):
            result = self.worker.qc_refill_feedback_from_report(self.report_dir, report, {1}, expected_sources=[original])
            self.assertEqual(result['1']['prior_shot_ids'], ['shot-A'])
            self.assertEqual(result['1']['reason'], 'object mismatch')
        with patch.object(self.worker.production_eval.visual_qc_contract, 'report_matches_draft', return_value=True), \
             patch('orchestrator.quality.draft_visual_qc._load_json', return_value={}), \
             patch('orchestrator.quality.draft_visual_qc._timeline_items', return_value=([{'index': 1, 'source': str(other)}], [])), \
             patch('orchestrator.media.source_normalization.read_prepared_sources', return_value=[]):
            self.assertEqual(self.worker.qc_refill_feedback_from_report(
                self.report_dir, report, {1}, expected_sources=[original]), {})

    def test_same_shot_recut_moves_to_the_uncovered_action(self):
        segment = {"shot_id": "shot-A", "source_start": 0.0, "source_end": 2.0, "audio_duration_us": 2_000_000}
        shot = {"shot_id": "shot-A", "source_start": 0.0, "source_end": 10.0,
                "action_segments": [{"start": 0.0, "end": 6.0}]}
        self.assertEqual(self.worker.same_shot_recut_window(segment, shot),
                         {"shot_id": "shot-A", "start": 4.0, "end": 6.0})
        covered = {**segment, "source_start": 4.0, "source_end": 6.0}
        self.assertIsNone(self.worker.same_shot_recut_window(covered, shot))
        self.assertIsNone(self.worker.same_shot_recut_window({**segment, "span_parts": [{}]}, shot))

    def test_degraded_report_restores_target_without_excluding_source(self):
        report = self.latest_failure('shot-A')
        report['status'] = 'PASS_WITH_DEGRADED'
        report['model_review'][0].update(match_level='ACCEPTABLE_DEGRADED', visual_ok=True)
        (self.report_dir / 'draft_visual_qc.json').write_text(json.dumps(report), encoding='utf-8')
        _, excluded = self.worker.restore_qc_rejections(self.report_dir, self.manifest)
        self.assertEqual(excluded, {})
        state = json.loads((self.report_dir / 'qc_rejected_shots.json').read_text(encoding='utf-8'))
        self.assertEqual(state['repair_indices'], [1])

    def test_restart_unions_history_and_latest_uncheckpointed_failure(self):
        self.latest_failure("shot-A")
        identity, rejected = self.worker.restore_qc_rejections(self.report_dir, self.manifest)
        self.assertEqual(rejected, {1: {"shot-A"}})
        # A later QC report replaces the first; a crash happens before checkpoint B.
        self.latest_failure("shot-B")
        result = subprocess.run([sys.executable, "-X", "utf8", "-c",
            "import json,sys,pathlib; sys.path.insert(0, 'doubao-jianying-orchestrator'); "
            "import batch_worker; "
            "_, rows = batch_worker.restore_qc_rejections(pathlib.Path(sys.argv[1]), json.loads(sys.argv[2])); "
            "print(json.dumps({str(k): sorted(v) for k,v in rows.items()}))",
            str(self.report_dir), json.dumps(self.manifest)], cwd=ROOT,
            text=True, encoding="utf-8", capture_output=True, check=True)
        restored = {int(k): set(v) for k, v in json.loads(result.stdout).items()}
        self.assertEqual(restored, {1: {"shot-A", "shot-B"}})
        # The cumulative record survives even when another build overwrites both reports.
        (self.report_dir / "draft_visual_qc.json").write_text("{}", encoding="utf-8")
        (self.report_dir / "shot_match_report.json").write_text("{}", encoding="utf-8")
        _, again = self.worker.restore_qc_rejections(self.report_dir, self.manifest)
        self.assertEqual(again, restored)
        self.assertEqual(json.loads((self.report_dir / "qc_rejected_shots.json").read_text())["identity"], identity)

    def test_changed_script_discards_history_and_stale_report(self):
        self.latest_failure("shot-A")
        self.worker.restore_qc_rejections(self.report_dir, self.manifest)
        for changed in ({"full_script": "A different sentence."}, {"product": "different"},
                        {"revision_feedback": "Use a different demonstration"}):
            with self.subTest(changed=changed):
                _, rejected = self.worker.restore_qc_rejections(
                    self.report_dir, {**self.manifest, **changed})
                self.assertEqual(rejected, {})

    def test_recovered_selection_crash_binds_exact_enriched_input_to_original(self):
        identity = self.worker.qc_rejection_identity(self.manifest)
        self.worker.save_qc_rejections(self.report_dir, identity, {1: {"shot-A"}})
        prepared = {**self.manifest, "voice_selected": "fixture", "planned_sentences": [{"text": "First sentence."}]}
        frozen = {"run_id": "fixture-run", "input_manifest": self.manifest, "manifest": prepared}
        state = {"status": "SUCCESS", "source_run_id": "fixture-run"}
        for name, value in (("selection_resume.json", frozen), ("selection_recovery_state.json", state)):
            (self.report_dir / name).write_text(json.dumps(value), encoding="utf-8")
        enriched = {**prepared, "parent_run_id": "fixture-run", "draft_name": "recovered",
                    "narration_timing_file": str(self.report_dir / "narration_timing.json")}
        self.latest_failure("shot-B", manifest=enriched)
        _, rejected = self.worker.restore_qc_rejections(self.report_dir, self.manifest)
        self.assertEqual(rejected, {1: {"shot-A", "shot-B"}})
        # An enriched input with changed editorial fields cannot borrow that lineage.
        self.worker.save_qc_rejections(self.report_dir, identity, {1: {"shot-A"}})
        self.latest_failure("shot-C", manifest={**enriched, "voice_selected": "different"})
        _, rejected = self.worker.restore_qc_rejections(self.report_dir, self.manifest)
        self.assertEqual(rejected, {1: {"shot-A"}})

    def test_replaced_source_discards_history_and_old_source_receipt(self):
        self.latest_failure("shot-A")
        self.worker.restore_qc_rejections(self.report_dir, self.manifest)
        self.source.write_bytes(b"new fixture media, different pixels and length")
        _, rejected = self.worker.restore_qc_rejections(self.report_dir, self.manifest)
        self.assertEqual(rejected, {})

    def test_recovery_controls_do_not_discard_valid_history(self):
        self.latest_failure("shot-A")
        identity, rejected = self.worker.restore_qc_rejections(self.report_dir, self.manifest)
        other = {**self.manifest, "draft_name": "another-output", "parent_run_id": "new-run",
                 "recovery_stage": "build", "qc_rejected_shots": {"1": ["shot-A"]},
                 "variant_index": 0, "shot_analysis": {"shots": []}}
        new_identity, restored = self.worker.restore_qc_rejections(self.report_dir, other)
        self.assertEqual(new_identity, identity)
        self.assertEqual(restored, rejected)

    def test_legacy_report_requires_input_and_source_binding(self):
        for mutate in ("missing_input", "different_claim", "missing_claim", "missing_source", "old_qc"):
            with self.subTest(mutate=mutate):
                self.latest_failure("shot-A")
                match_path = self.report_dir / "shot_match_report.json"
                qc_path = self.report_dir / "draft_visual_qc.json"
                match = json.loads(match_path.read_text())
                if mutate == "missing_input":
                    match.pop("input_manifest")
                elif mutate == "missing_source":
                    match["segments"][0].pop("ai_selection")
                if mutate in {"missing_input", "missing_source"}:
                    match_path.write_text(json.dumps(match), encoding="utf-8")
                elif mutate in {"different_claim", "missing_claim"}:
                    self.latest_failure("shot-A", claim="other" if mutate == "different_claim" else "")
                else:
                    os.utime(qc_path, ns=(1, 1))
                # Control report freshness so missing identity, not timestamp ordering, is tested.
                if mutate != "old_qc":
                    stamp = max(qc_path.stat().st_mtime_ns, match_path.stat().st_mtime_ns) + 1
                    os.utime(qc_path, ns=(stamp, stamp))
                _, rejected = self.worker.restore_qc_rejections(self.report_dir, self.manifest)
                self.assertEqual(rejected, {})

    def test_malformed_checkpoint_is_ignored_without_inventing_ids(self):
        identity = self.worker.qc_rejection_identity(self.manifest)
        state = {"schema": "sentence-qc-rejections-v1", "identity": identity,
                 "rejected_shots": {"1": ["valid"], "2": "not-a-list", "3": [None], "0": ["zero"]}}
        (self.report_dir / "qc_rejected_shots.json").write_text(json.dumps(state), encoding="utf-8")
        _, restored = self.worker.restore_qc_rejections(self.report_dir, self.manifest)
        self.assertEqual(restored, {1: {"valid"}})


if __name__ == "__main__":
    unittest.main()
