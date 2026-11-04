"""Default selection/recovery/writer regression; all observations are frozen."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import ai_editor_contract as contract
from orchestrator.application.engine import OrchestrationEngine
from orchestrator.application import headless_production
from orchestrator.domain.clips import Clip
from orchestrator.policy import playback
from orchestrator.selection import ai_editor as editor, shot_analyzer, timing_contract as timing


def process(start=0, end=20_000_000):
    return {"schema": playback.OBSERVATION_SCHEMA, "kind": "process",
            "start_us": start, "end_us": end, "observed": "Reviewed continuous process without key contact or result"}


class ProductionPlaybackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.manifest = {"head_trim_s": 0, "tail_trim_s": 0, "tail_pad_s": 0,
                         "product": "synthetic", "selection_mode": "ai_editor",
                         "segments": [{"text": "first", "audio_duration_us": 2_000_000},
                                      {"text": "second", "audio_duration_us": 2_000_000}]}
        self.shots, self.reviews = [], []
        for sid, length in (("long", 10), ("short", 1), ("other", 1)):
            video = self.root / (sid + '.mp4')
            video.write_bytes(sid.encode())
            self.shots.append({"shot_id": sid, "video": str(video), "status": "ready_for_matching",
                               "source_start": 0, "source_end": 12, "material_duration_us": 12_000_000})
            self.reviews.append({"shot_id": sid, "verdict": "matched", "same_product": True,
                "basis": "editorial_fit", "observation": "Complete action and visible result",
                "reason": "frozen review", "confidence": .9, "supported_point_ids": ["p1"],
                "time_sensitive": False, "scene_ids": [],
                "evidence_intervals_us": [{"start": 0, "end": length * 1_000_000}],
                "source_identity": contract.source_identity(video)})
        self.analysis = {"shots": self.shots}
        identity = {"model": "fixture", "manifest_sha256": contract.digest(self.manifest),
                    "timing_sha256": contract.digest(None), "analysis_sha256": contract.digest(self.analysis),
                    "policy_version": contract.POLICY_VERSION, "editorial_rules": contract.rules_identity(self.manifest)}
        self.plan = {"schema": contract.PLAN_SCHEMA, "ok": True, "policy_version": contract.POLICY_VERSION,
                     "identity": identity, "resume_identity": identity, "display_reviews": [],
                     "sentences": [{"index": i, "text": text, "kind": "selling_point",
                         "selling_points": [{"id": "p1", "text": text}],
                         "candidate_ids": ids} for i, text, ids in
                         ((1, "first", ["long", "short"]), (2, "second", ["other"]))],
                     "reviews": {"1": self.reviews[:2], "2": self.reviews[2:]},
                     "coverage": {str(i): {"feasible": True} for i in (1, 2)},
                     "rows": [{"index": i, "parts": [{"shot_id": sid, "audio_duration_us": 2_000_000}]}
                              for i, sid in ((1, "long"), (2, "other"))]}

    def test_cached_success_reselects_only_bad_sentence_without_api_or_analysis(self):
        before = copy.deepcopy(self.plan)
        with patch.object(editor, '_call', side_effect=AssertionError('API forbidden')):
            result = shot_analyzer.match_manifest(self.manifest, {**self.analysis, "ai_editor_plan": self.plan})
        self.assertTrue(result['ok'], result)
        self.assertEqual([s['shot_id'] for s in result['segments']], ['short', 'other'])
        self.assertEqual([r['index'] for r in result['playback_repairs']], [1])
        self.assertEqual(self.plan, before)
        self.assertTrue(all(s['video_speed'] == 1 for s in result['segments']))

    def test_prepare_rechecks_old_feasible_flag_and_reuses_observations(self):
        folder = self.root / 'ai_editor'
        folder.mkdir()
        (folder / 'plan.json').write_text(json.dumps(self.plan), encoding='utf-8')
        before = copy.deepcopy(self.plan['reviews'])
        with patch.object(editor, '_editor_model', return_value='fixture'), \
                patch.object(editor, '_call', side_effect=AssertionError('API forbidden')), \
                patch.object(editor, '_review', side_effect=AssertionError('reidentification forbidden')):
            result = editor.prepare(self.manifest, self.analysis, self.root)
        plan = result['ai_editor_plan']
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['reviews'], before)
        self.assertEqual(plan['rows'][0]['parts'][0]['shot_id'], 'short')
        self.assertEqual(plan['rows'][1], self.plan['rows'][1])
        self.assertEqual(plan['playback_policy_version'], playback.VERSION)

    def test_qc_rejection_preserves_observations_and_skips_rejected_repair_paths(self):
        folder = self.root / 'ai_editor'
        folder.mkdir()
        (folder / 'plan.json').write_text(json.dumps(self.plan), encoding='utf-8')
        self.manifest['qc_rejected_shots'] = {'1': ['long']}
        with patch.object(editor, '_editor_model', return_value='fixture'), \
             patch.object(editor, '_call', side_effect=AssertionError('API forbidden')), \
             patch.object(editor, '_review', side_effect=AssertionError('reidentification forbidden')):
            prepared = editor.prepare(self.manifest, self.analysis, self.root)['ai_editor_plan']
        self.assertTrue(prepared['ok'], prepared)
        self.assertEqual(prepared['rows'][0]['parts'][0]['shot_id'], 'short')
        self.assertEqual(prepared['rows'][1], self.plan['rows'][1])
        self.assertEqual(prepared['reviews']['2'], self.plan['reviews']['2'])
        # Neither a cached narrow observation nor the empty-selection fallback
        # can spend a recovery attempt on the rejected shot.
        self.manifest['qc_rejected_shots'] = {'1': ['short', 'long']}
        self.plan['window_reviews'] = {'old': {**self.reviews[1], 'sentence_index': 1,
            'review_window_us': {'start': 0, 'end': 2_000_000}}}
        self.assertEqual(list(editor._short_complete_rows(self.manifest, self.analysis, self.plan, 1, None)), [])
        before = copy.deepcopy(self.plan['rows'])
        self.assertFalse(editor._retrieve_empty_selection(self.manifest, self.analysis, self.plan,
            ValueError('AI_EDITOR_NO_SELECTION:1'), None))
        self.assertEqual(self.plan['rows'], before)

    def test_no_short_window_is_structured_conflict_and_evidence_unchanged(self):
        self.plan['reviews']['1'] = self.reviews[:1]
        frozen = copy.deepcopy(self.plan)
        result = editor.match_manifest(self.manifest, {**self.analysis, 'ai_editor_plan': self.plan})
        self.assertFalse(result['ok'])
        self.assertEqual(result['failure_code'], 'PLAYBACK_POLICY_CONFLICT')
        self.assertEqual(result['resume_stage'], 'semantic_match')
        self.assertEqual(self.plan, frozen)

    def test_long_cached_window_gets_bounded_pixel_review_without_rewriting_original(self):
        self.plan['reviews']['1'] = self.reviews[:1]
        original = copy.deepcopy(self.reviews[0])
        # A separate real review can confirm a short complete repetition. The
        # test injects that observation; it never edits the broad observation.
        reviewed = {**copy.deepcopy(original), 'evidence_intervals_us': [{'start': 0, 'end': 1_000_000}],
                    'review_window_us': {'start': 0, 'end': 4_000_000}}
        with patch.object(editor, '_call', return_value={'rows': copy.deepcopy(self.plan['rows'])}), \
                patch.object(editor, '_review', return_value=reviewed) as review:
            editor._edit_plan(self.manifest, self.analysis, self.plan, self.root)
        self.assertTrue(self.plan['ok'])
        review.assert_called_once()
        self.assertEqual(self.plan['reviews']['1'][0], original)
        self.assertTrue(self.plan['rows'][0]['parts'][0]['window_review_key'])
        self.assertEqual(self.plan['rows'][1]['parts'][0]['shot_id'], 'other')

    def test_ai_refill_uses_same_short_complete_selection(self):
        manifest = {**self.manifest, 'segments': self.manifest['segments'][:1]}
        with patch.object(editor, '_review_pool', return_value=(self.reviews[:2], {'feasible': True})), \
                patch.object(editor, '_call', side_effect=AssertionError('API forbidden')):
            result = editor.refill_indices(manifest, self.analysis,
                {'segments': [], 'pending_items': []}, [1], self.root)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['segments'][0]['shot_id'], 'short')
        self.assertEqual(result['segments'][0]['video_speed'], 1)

    def test_refill_infeasible_evidence_reports_timing_not_missing_material(self):
        manifest = {**self.manifest, 'segments': self.manifest['segments'][:1]}
        with patch.object(editor, '_review_pool', return_value=(self.reviews[:1],
                {'feasible': False, 'reason': 'KNOWN_WINDOWS_INFEASIBLE'})):
            result = editor.refill_indices(manifest, self.analysis,
                {'segments': [], 'pending_items': []}, [1], self.root)
        self.assertFalse(result['ok'])
        self.assertEqual(result['failure_code'], 'PLAYBACK_POLICY_CONFLICT')

    def test_final_engine_solver_carries_reviewed_permission_and_caps_override(self):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = self.manifest
        clip = Clip('synthetic', 0, 2, source_end=6, video_duration=12,
            evidence_intervals=[{'start': 0, 'end': 5}], speed_range_override=[1, 20],
            playback_observation=process(0, 6_000_000))
        req = engine._timing_request(clip, 2_000_000)
        self.assertEqual((req.speed_min, req.speed_max), (1, 3))
        self.assertTrue(timing.solve_window(req).ok)
        clip.time_sensitive = True
        req = engine._timing_request(clip, 2_000_000)
        self.assertEqual((req.speed_min, req.speed_max), (1, 1))
        self.assertFalse(timing.solve_window(req).ok)

    def test_process_can_use_two_to_three_only_with_reviewed_boundaries(self):
        shot = editor._observed_shot(self.shots[0], {**self.reviews[0],
            'evidence_intervals_us': [{'start': 0, 'end': 5_000_000}],
            'playback_observation': process(0, 6_000_000)})
        solution = shot_analyzer.candidate_window(shot, audio_duration_us=2_000_000, manifest=self.manifest)
        self.assertTrue(solution.ok, solution)
        self.assertGreater(solution.speed, 2)
        self.assertLessEqual(solution.speed, 3)
        self.assertGreaterEqual(solution.source_end_us, 5_000_000)
        self.assertFalse(shot_analyzer.candidate_window(shot, audio_duration_us=2_000_000,
                                                       manifest=self.manifest, time_sensitive=True).ok)

    def test_unknown_stages_and_old_overrides_do_not_authorize_acceleration(self):
        for mode in ('ai_editor', 'legacy', 'rule_then_ai'):
            self.assertEqual(shot_analyzer._fit_speed_range(
                {'selection_mode': mode, 'visual_role': 'product_display', 'speed_range_override': [.85, 20]},
                manifest=self.manifest, time_sensitive=False), (1, 1))
        self.assertEqual(timing.effective_speed_range('direct_evidence', time_sensitive=True,
            override=[1, 20], playback_observation=process()), (1, 1))

    def test_solver_does_not_accept_legacy_twenty_override(self):
        solution = timing.solve_window(timing.WindowRequest(source_in_us=0, source_out_us=20_000_000,
            audio_duration_us=1_000_000, tail_pad_us=0, head_guard_us=0, tail_guard_us=0,
            evidence_start_us=0, evidence_end_us=10_000_000, speed_min=1, speed_max=20))
        self.assertFalse(solution.ok)
        self.assertEqual(solution.detail['speed_max'], 3)

    def test_old_receipt_at_realtime_remains_usable_without_changing_visual_identity(self):
        self.plan['rows'][0]['parts'][0]['shot_id'] = 'short'
        item = editor.match_manifest(self.manifest, {**self.analysis, 'ai_editor_plan': self.plan})['segments'][0]
        item['speed_range_override'] = [.85, 20]
        item['ai_selection']['speed_range_override'] = [.85, 20]
        item['ai_selection'].pop('playback_policy_version')
        self.assertTrue(contract.validate_segment_receipt(item)['ok'])

    def test_actual_duration_cannot_hide_high_speed_with_a_forged_number(self):
        issues = playback.conflicts(start_us=0, end_us=10_000_000, target_us=1_000_000,
                                    speed=1, observation=process())
        self.assertEqual(issues[0]['actual_speed'], 10)

    def test_process_and_protected_result_are_checked_per_physical_part(self):
        self.manifest['segments'] = self.manifest['segments'][:1]
        self.plan['sentences'] = self.plan['sentences'][:1]
        self.plan['rows'] = [{'index': 1, 'parts': [
            {'shot_id': 'long', 'audio_duration_us': 1_000_000},
            {'shot_id': 'short', 'audio_duration_us': 1_000_000}]}]
        self.reviews[0].update(evidence_intervals_us=[{'start': 0, 'end': 2_000_000}],
                               playback_observation=process(0, 3_000_000))
        self.reviews[1]['time_sensitive'] = True
        result = editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None)
        self.assertTrue(result['ok'])
        parts = result['segments'][0]['span_parts']
        self.assertGreater(parts[0]['video_speed'], 1)
        self.assertLessEqual(parts[0]['video_speed'], 3)
        self.assertEqual(parts[1]['video_speed'], 1)
        self.assertTrue(contract.validate_segment_receipt(result['segments'][0])['ok'])

    def test_nonfinite_playback_is_a_structured_writer_block(self):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = {}
        for speed in (float('nan'), float('inf'), -1):
            result = engine.write_draft([Clip('bad', 0, 1, video_speed=speed)], None)
            self.assertEqual(result['status'], 'TIMING_INFEASIBLE')

    def test_process_permission_cannot_expand_outside_reviewed_window(self):
        self.assertTrue(playback.conflicts(start_us=0, end_us=4_000_000,
            target_us=2_000_000, speed=2, observation=process(1_000_000, 3_000_000)))

    def test_real_failure_windows_block_both_writers_before_any_side_effect(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/production_speed_failures.json').read_text())
        for case in fixture['cases']:
            for backend in ('headless', 'legacy'):
                with self.subTest(case=case['case'], backend=backend):
                    engine = OrchestrationEngine.__new__(OrchestrationEngine)
                    engine.m = {'draft_backend': backend, 'delivery_mode': 'preview'}
                    clip = Clip('frozen.mp4', case['source_start_us'] / 1e6, case['target_us'] / 1e6,
                        video_speed=case['video_speed'], time_sensitive=case['time_sensitive'],
                        solved_window={'source_end': case['source_end_us'] / 1e6}, raw_index=case['sentence_index'] - 1)
                    with patch.object(headless_production, 'write_draft') as native, \
                            patch.object(engine, '_write_legacy_draft') as legacy:
                        result = engine.write_draft([clip], None)
                    self.assertEqual(result['status'], 'TIMING_INFEASIBLE')
                    self.assertEqual(result['conflicts'][0]['index'], case['sentence_index'])
                    native.assert_not_called()
                    legacy.assert_not_called()

    def test_rejected_task_does_not_poison_following_valid_write(self):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = {}
        with patch.object(headless_production, 'write_draft', return_value={'status': 'SUCCESS'}) as native:
            self.assertEqual(engine.write_draft([Clip('bad', 0, 1, video_speed=10)], None)['status'], 'TIMING_INFEASIBLE')
            self.assertEqual(engine.write_draft([Clip('good', 0, 1)], None)['status'], 'SUCCESS')
            native.assert_called_once()


if __name__ == '__main__':
    unittest.main()
