"""Frozen joint pixel observations; no service, installed runtime, or real draft writes."""
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
from orchestrator.policy import continuity
from orchestrator.quality import continuity as qc
from orchestrator.quality import ai_editor_qc
from orchestrator.selection import ai_editor as editor, sentence_continuity as sharing, timing_contract as timing
from orchestrator.selection.ai_editor_recovery import WindowFailure
import test_production_playback as baseline
process = baseline.process


class SentenceContinuityTests(unittest.TestCase):
    def setUp(self):
        baseline.ProductionPlaybackTests.setUp(self)
        self.analysis = {'shots': self.shots[:1]}
        self.shot = self.shots[0]
        self.original_review = {**self.reviews[0], 'evidence_intervals_us': [{'start': 0, 'end': 4_000_000}]}
        self.plan['reviews'] = {str(i): [copy.deepcopy(self.original_review)] for i in (1, 2)}
        for sentence, row in zip(self.plan['sentences'], self.plan['rows']):
            sentence['candidate_ids'] = ['long']
            row['parts'][0]['shot_id'] = 'long'
        self.identity()

    def identity(self):
        self.plan['identity'].update(manifest_sha256=contract.digest(self.manifest),
                                     analysis_sha256=contract.digest(self.analysis))
        self.plan['resume_identity'] = copy.deepcopy(self.plan['identity'])

    def test_qc_rejected_member_cannot_trigger_joint_review(self):
        self.assertTrue(list(sharing.proposals(self.manifest, self.analysis, self.plan, 1, None)))
        self.manifest['qc_rejected_shots'] = {'1': ['long']}
        self.assertEqual(list(sharing.proposals(self.manifest, self.analysis, self.plan, 1, None)), [])
        self.manifest['qc_rejected_shots'] = {'3': ['long']}
        self.assertTrue(list(sharing.proposals(self.manifest, self.analysis, self.plan, 1, None)))

    def group(self, context=None):
        context = context or next(sharing.proposals(self.manifest, self.analysis, self.plan, 1, None))
        observations = [{'index': m['index'], 'verdict': 'matched', 'same_product': True,
            'basis': 'editorial_fit', 'phase': 'process' if n == 0 else 'completion and result',
            'observation': 'Observed assigned continuous phase', 'reason': 'This stage matches this sentence',
            'supported_point_ids': ['p1'], 'time_sensitive': False, 'scene_ids': [],
            'evidence_intervals_us': [{'start': m['start_us'], 'end': m['end_us']}]}
            for n, m in enumerate(context['members'])]
        return {'schema': continuity.SCHEMA, 'input': context, 'review': {
            'verdict': 'matched', 'same_product': True, 'same_demonstration': True,
            'same_selling_point': True, 'semantic_continuity': True, 'complete_action': True,
            'result_preserved': True, 'shared_point': 'Same specific benefit',
            'product_observation': 'Same observed package and pad',
            'demonstration_observation': 'Continuous setup, sample and liquid',
            'observation': 'Complete action and readable result', 'reason': 'Continuous stages fit the narration',
            'confidence': .9, 'complete_intervals_us': [copy.deepcopy(context['window_us'])],
            'members': observations}}

    def install(self):
        group = self.group()
        sharing.install(self.plan, contract.digest(group['input'])[:24], group)
        return editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None, verify_sources=True)

    def clips(self, result):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = result['manifest']
        clips = []
        cursor = 0
        for i, row in enumerate(result['segments']):
            clip = Clip(row['video'], row['source_start'], row['segment_s'], text=row['text'],
                source_end=row['source_end'], video_duration=12, raw_index=i, start_us=cursor,
                audio_duration_us=row['audio_duration_us'], runtime_audio_us=row['audio_duration_us'],
                video_speed=1, continuity_group=row['continuity_group'], guard_override=[0, 0],
                evidence_intervals=row['evidence_intervals'], solved_window=row['window_solution'])
            clips.append(clip)
            cursor += round(clip.duration * 1e6)
        return engine, clips

    def multipoint_group(self, order):
        for sentence in self.plan['sentences']:
            sentence['selling_points'].append({'id': 'p2', 'text': 'second visible benefit'})
        self.identity()
        group = self.group()
        for observed in group['review']['members']:
            observed['supported_point_ids'] = list(order)
        return group

    def assemble_group(self, group):
        sharing.install(self.plan, contract.digest(group['input'])[:24], group)
        return editor._assemble(self.manifest, self.analysis, self.plan,
                                narration_timing=None, verify_sources=True)

    def test_multiple_supported_points_assemble_in_either_review_order(self):
        original_plan = copy.deepcopy(self.plan)
        for order in (['p1', 'p2'], ['p2', 'p1']):
            with self.subTest(order=order):
                self.plan = copy.deepcopy(original_plan)
                group = self.multipoint_group(order)
                frozen = copy.deepcopy(group)
                result = self.assemble_group(group)
                self.assertTrue(result['ok'])
                self.assertEqual(group, frozen)
                self.assertEqual(contract.sequence_errors(result['segments']), [])
                for index, segment in enumerate(result['segments'], 1):
                    self.assertEqual(segment['ai_selection']['supported_point_ids'], ['p1', 'p2'])
                    self.assertEqual(continuity.member_errors(segment, index), [])
                    segment['ai_selection']['supported_point_ids'] = list(reversed(order))
                    self.assertEqual(continuity.member_errors(segment, index), [])

    def test_supported_point_membership_changes_still_block(self):
        result = self.assemble_group(self.multipoint_group(['p1', 'p2']))
        for ids in (['p1'], ['p2'], [], ['p1', 'p2', 'invented'], ['p1', 'invented']):
            with self.subTest(ids=ids):
                segment = copy.deepcopy(result['segments'][0])
                segment['ai_selection']['supported_point_ids'] = ids
                self.assertIn('CONTINUITY_MEMBER_OBSERVATION_CHANGED',
                              continuity.member_errors(segment, 1))

    def test_malformed_supported_point_receipts_still_block(self):
        result = self.assemble_group(self.multipoint_group(['p1', 'p2']))
        for ids in (None, 'p1', {'p1': True, 'p2': True}, [['p1'], 'p2']):
            with self.subTest(ids=ids):
                segment = copy.deepcopy(result['segments'][0])
                segment['ai_selection']['supported_point_ids'] = ids
                self.assertTrue(continuity.member_errors(segment, 1))

    def test_other_member_observation_changes_still_block(self):
        result = self.assemble_group(self.multipoint_group(['p1', 'p2']))
        changes = {'observation': 'different pixels', 'reason': 'different reason',
                   'basis': 'visible_result', 'time_sensitive': True,
                   'evidence_intervals_us': [{'start': 0, 'end': 1_000_000}]}
        for field, value in changes.items():
            with self.subTest(field=field):
                segment = copy.deepcopy(result['segments'][0])
                segment['ai_selection'][field] = value
                self.assertIn('CONTINUITY_MEMBER_OBSERVATION_CHANGED',
                              continuity.member_errors(segment, 1))

    def test_reversed_point_group_repair_keeps_original_observations(self):
        group = self.multipoint_group(['p2', 'p1'])
        frozen = copy.deepcopy(self.plan['reviews'])
        with patch.object(sharing, 'review_group', return_value=group) as review:
            self.assertTrue(sharing.repair(self.manifest, self.analysis, self.plan, self.root,
                            WindowFailure(1, 'long', 'TOO_LONG'), None))
        review.assert_called_once()
        self.assertEqual(self.plan['reviews'], frozen)
        self.assertEqual(self.plan['continuity_repairs'][0]['indices'], [1, 2])
        self.assertTrue(editor._assemble(self.manifest, self.analysis, self.plan,
                                        narration_timing=None)['ok'])

    def test_reversed_point_cached_group_recovers_without_paid_review(self):
        group = self.multipoint_group(['p2', 'p1'])
        self.assemble_group(group)
        frozen = copy.deepcopy(self.plan)
        folder = self.root / 'ai_editor'
        folder.mkdir()
        (folder / 'plan.json').write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(editor, '_editor_model', return_value='fixture'), \
                patch.object(editor, '_call', side_effect=AssertionError('API forbidden')), \
                patch.object(editor, '_review_pool', side_effect=AssertionError('paid rereview forbidden')), \
                patch.object(sharing, 'review_group', side_effect=AssertionError('joint rereview forbidden')):
            analysis = editor.prepare(self.manifest, self.analysis, self.root)
            result = editor.match_manifest(self.manifest, analysis)
        self.assertTrue(result['ok'], result)
        self.assertEqual(analysis['ai_editor_plan']['reviews'], frozen['reviews'])
        self.assertEqual(analysis['ai_editor_plan']['continuity_reviews'], frozen['continuity_reviews'])

    def test_default_edit_path_tries_short_review_before_joint_group(self):
        frozen = copy.deepcopy(self.plan['reviews'])
        calls = []
        def narrow(*args):
            calls.append('short')
            return False
        def joint(manifest, context, shot, directory):
            calls.append('group')
            return self.group(context)
        with patch.object(editor, '_call', return_value={'rows': copy.deepcopy(self.plan['rows'])}), \
                patch.object(editor, '_repair_selected_window', side_effect=narrow), \
                patch.object(sharing, 'review_group', side_effect=joint):
            editor._edit_plan(self.manifest, self.analysis, self.plan, self.root)
        self.assertTrue(self.plan['ok'], self.plan)
        self.assertEqual(calls, ['short', 'group'])
        self.assertEqual(self.plan['reviews'], frozen)
        result = editor.match_manifest(self.manifest, {**self.analysis, 'ai_editor_plan': self.plan})
        self.assertTrue(result['ok'], result)
        self.assertEqual([(s['source_start'], s['source_end'], s['video_speed']) for s in result['segments']],
                         [(0, 2, 1), (2, 4, 1)])
        self.assertEqual([s['text'] for s in result['segments']], ['first', 'second'])
        self.assertEqual(result['continuity_repairs'][0]['indices'], [1, 2])

    def test_short_complete_alternative_wins_without_joint_review(self):
        self.analysis['shots'] += self.shots[1:]
        self.plan['reviews']['1'].append(self.reviews[1])
        self.plan['reviews']['2'] = [self.reviews[2]]
        self.plan['rows'][1]['parts'][0]['shot_id'] = 'other'
        with patch.object(editor, '_call', return_value={'rows': copy.deepcopy(self.plan['rows'])}), \
                patch.object(sharing, 'review_group', side_effect=AssertionError('unnecessary group review')):
            editor._edit_plan(self.manifest, self.analysis, self.plan, self.root)
        self.assertTrue(self.plan['ok'])
        self.assertEqual(self.plan['rows'][0]['parts'][0]['shot_id'], 'short')

    def test_empty_model_selection_still_reviews_known_timing_conflict(self):
        for row in self.plan['rows']:
            row['parts'] = []
        frozen = copy.deepcopy(self.plan['reviews'])
        order = []
        def narrow(*args):
            order.append('short')
            return False
        def joint(manifest, context, shot, directory):
            order.append('group')
            return self.group(context)
        with patch.object(editor, '_call', return_value={'rows': copy.deepcopy(self.plan['rows'])}) as edit, \
                patch.object(editor, '_repair_selected_window', side_effect=narrow), \
                patch.object(sharing, 'review_group', side_effect=joint):
            editor._edit_plan(self.manifest, self.analysis, self.plan, self.root)
        self.assertTrue(self.plan['ok'])
        edit.assert_called_once()
        self.assertEqual(order, ['short', 'group'])
        self.assertEqual(self.plan['reviews'], frozen)
        result = editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None)
        self.assertEqual([(s['source_start'], s['source_end']) for s in result['segments']], [(0, 2), (2, 4)])

    def test_empty_selection_uses_cached_short_candidates_before_paid_review(self):
        self.analysis['shots'] += self.shots[1:]
        self.plan['reviews']['1'].append(self.reviews[1])
        self.plan['reviews']['2'] = [self.reviews[2]]
        for row in self.plan['rows']:
            row['parts'] = []
        with patch.object(editor, '_call', return_value={'rows': copy.deepcopy(self.plan['rows'])}) as edit, \
                patch.object(editor, '_repair_selected_window', side_effect=AssertionError('Unneeded review')), \
                patch.object(sharing, 'review_group', side_effect=AssertionError('Unneeded group')):
            editor._edit_plan(self.manifest, self.analysis, self.plan, self.root)
        self.assertTrue(self.plan['ok'])
        edit.assert_called_once()
        self.assertEqual([r['parts'][0]['shot_id'] for r in self.plan['rows']], ['short', 'other'])

    def test_empty_selection_without_matched_evidence_does_not_start_joint_review(self):
        self.plan['rows'][0]['parts'] = []
        self.plan['reviews']['1'][0]['verdict'] = 'uncertain'
        with patch.object(editor, '_call', return_value={'rows': copy.deepcopy(self.plan['rows'])}) as edit, \
                patch.object(editor, '_repair_selected_window', side_effect=AssertionError('No observed match')), \
                patch.object(sharing, 'review_group', side_effect=AssertionError('No observed match')):
            with self.assertRaisesRegex(ValueError, 'AI_EDITOR_NO_SELECTION:1'):
                editor._edit_plan(self.manifest, self.analysis, self.plan, self.root)
        self.assertFalse(self.plan['ok'])
        self.assertEqual(edit.call_count, 3)

    def four_groups(self, empty=False):
        self.manifest['segments'] = [{'text': f'sentence {i}', 'audio_duration_us': 2_000_000} for i in range(1, 9)]
        self.analysis['shots'] = []
        self.plan.update(sentences=[], rows=[], reviews={})
        for pair in range(4):
            video = self.root / f'group-{pair}.mp4'
            video.write_bytes(f'synthetic demonstration {pair}'.encode())
            shot = {**self.shot, 'shot_id': f'group-{pair}', 'video': str(video)}
            self.analysis['shots'].append(shot)
            reviewed = {**copy.deepcopy(self.original_review), 'shot_id': shot['shot_id'],
                        'source_identity': contract.source_identity(video)}
            for index in (pair * 2 + 1, pair * 2 + 2):
                self.plan['sentences'].append({'index': index, 'text': f'sentence {index}', 'kind': 'selling_point',
                    'selling_points': [{'id': 'p1', 'text': f'benefit {pair}'}], 'candidate_ids': [shot['shot_id']]})
                self.plan['rows'].append({'index': index, 'parts': [] if empty else [
                    {'shot_id': shot['shot_id'], 'audio_duration_us': 2_000_000}]})
                self.plan['reviews'][str(index)] = [copy.deepcopy(reviewed)]
        self.identity()

    def assert_four_group_repair(self, empty):
        self.four_groups(empty)
        frozen = copy.deepcopy(self.plan['reviews'])
        with patch.object(editor, '_call', return_value={'rows': copy.deepcopy(self.plan['rows'])}) as edit, \
                patch.object(editor, '_repair_selected_window', return_value=False), \
                patch.object(sharing, 'review_group', side_effect=lambda m, c, s, d: self.group(c)) as review:
            editor._edit_plan(self.manifest, self.analysis, self.plan, self.root)
        self.assertTrue(self.plan['ok'], self.plan)
        edit.assert_called_once()
        self.assertEqual(review.call_count, 4)
        self.assertEqual([r['indices'] for r in self.plan['continuity_repairs']], [[1, 2], [3, 4], [5, 6], [7, 8]])
        self.assertEqual(self.plan['reviews'], frozen)
        result = editor.match_manifest(self.manifest, {**self.analysis, 'ai_editor_plan': self.plan})
        self.assertTrue(result['ok'], result)
        self.assertEqual([s['audio_duration_us'] for s in result['segments']], [2_000_000] * 8)

    def test_four_groups_do_not_spend_editor_retry_rounds(self):
        self.assert_four_group_repair(empty=False)

    def test_four_groups_from_empty_model_rows_all_complete(self):
        self.assert_four_group_repair(empty=True)

    def test_uncertain_fourth_group_keeps_first_three_but_blocks_final_plan(self):
        self.four_groups()
        def joint(manifest, context, shot, directory):
            if context['members'][0]['index'] == 7:
                raise ValueError('CONTINUITY_JOINT_REVIEW_UNCERTAIN')
            return self.group(context)
        with patch.object(editor, '_call', return_value={'rows': copy.deepcopy(self.plan['rows'])}) as edit, \
                patch.object(editor, '_repair_selected_window', return_value=False), \
                patch.object(sharing, 'review_group', side_effect=joint) as review:
            with self.assertRaises(WindowFailure):
                editor._edit_plan(self.manifest, self.analysis, self.plan, self.root)
        self.assertFalse(self.plan['ok'])
        self.assertEqual(edit.call_count, 3)
        self.assertEqual(review.call_count, 4)
        self.assertEqual([r['indices'] for r in self.plan['continuity_repairs']], [[1, 2], [3, 4], [5, 6]])

    def test_repair_reporting_progress_without_changing_rows_cannot_loop(self):
        with patch.object(editor, '_call', return_value={'rows': copy.deepcopy(self.plan['rows'])}) as edit, \
                patch.object(editor, '_repair_selected_window', return_value=False), \
                patch.object(sharing, 'repair', return_value=True) as repair:
            with self.assertRaises(WindowFailure):
                editor._edit_plan(self.manifest, self.analysis, self.plan, self.root)
        self.assertFalse(self.plan['ok'])
        self.assertEqual(edit.call_count, 3)
        self.assertEqual(repair.call_count, 3)

    def test_joint_rejections_never_grant_shared_time(self):
        for key in ('same_product', 'same_demonstration', 'same_selling_point', 'semantic_continuity',
                    'complete_action', 'result_preserved'):
            with self.subTest(key=key):
                group = self.group()
                group['review'][key] = False
                with self.assertRaisesRegex(ValueError, 'CONTINUITY_JOINT_REVIEW_UNCERTAIN'):
                    continuity.validate(group)

    def test_nonadjacent_or_different_point_or_outside_evidence_rejected(self):
        mutations = [lambda g: g['input']['members'][1].update(index=3),
                     lambda g: g['review']['members'][0].update(supported_point_ids=['invented']),
                     lambda g: g['review']['members'][0]['evidence_intervals_us'][0].update(end=3_000_000),
                     lambda g: g['input']['members'][1].update(start_us=2_100_000)]
        for mutation in mutations:
            group = self.group()
            mutation(group)
            with self.assertRaises(ValueError):
                continuity.validate(group)

    def test_retrieval_does_not_combine_unrelated_source_hook_or_unfitting_action(self):
        self.plan['reviews']['2'] = self.reviews[2:]
        self.assertEqual(list(sharing.proposals(self.manifest, self.analysis, self.plan, 1, None)), [])
        self.plan['reviews']['2'] = [self.original_review]
        self.plan['sentences'][1]['kind'] = 'hook'
        self.assertEqual(list(sharing.proposals(self.manifest, self.analysis, self.plan, 1, None)), [])
        self.plan['sentences'][1]['kind'] = 'selling_point'
        self.original_review['evidence_intervals_us'][0]['end'] = 10_000_000
        self.assertEqual(list(sharing.proposals(self.manifest, self.analysis, self.plan, 1, None)), [])

    def test_recovery_reuses_joint_and_narrow_observations_without_call(self):
        self.install()
        folder = self.root / 'ai_editor'
        folder.mkdir()
        (folder / 'plan.json').write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(editor, '_editor_model', return_value='fixture'), \
                patch.object(editor, '_call', side_effect=AssertionError('API forbidden')), \
                patch.object(editor, '_review_pool', side_effect=AssertionError('paid rereview forbidden')):
            plan = editor.prepare(self.manifest, self.analysis, self.root)['ai_editor_plan']
        self.assertTrue(plan['ok'], plan)
        self.assertEqual(plan['reviews'], self.plan['reviews'])
        self.assertEqual(plan['continuity_reviews'], self.plan['continuity_reviews'])

    def test_joint_review_budget_persists_and_failed_review_does_not_change_rows(self):
        frozen = copy.deepcopy(self.plan['rows'])
        with patch.object(sharing, 'review_group', side_effect=ValueError('CONTINUITY_JOINT_REVIEW_UNCERTAIN')) as review:
            for _ in range(3):
                self.assertFalse(sharing.repair(self.manifest, self.analysis, self.plan, self.root,
                    WindowFailure(1, 'long', 'TOO_LONG'), None))
        review.assert_called_once()
        self.assertEqual(self.plan['rows'], frozen)

    def test_changed_timing_script_product_source_and_missing_member_block(self):
        self.install()
        for field, value in [('product', 'different'), ('tail_pad_s', .12)]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'CONTINUITY_'):
                editor._assemble({**self.manifest, field: value}, self.analysis, self.plan, narration_timing=None)
        modified = copy.deepcopy(self.plan)
        modified['rows'][1].pop('continuity_key')
        with self.assertRaisesRegex(ValueError, 'CONTINUITY_INPUT_CHANGED'):
            editor._assemble(self.manifest, self.analysis, modified, narration_timing=None)
        self.shot_path = Path(self.shot['video'])
        self.shot_path.write_bytes(b'changed source')
        with self.assertRaisesRegex(ValueError, 'CONTINUITY_INPUT_CHANGED'):
            editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None)

    def test_members_cannot_be_detached_from_group_or_observation(self):
        result = self.install()
        segments = copy.deepcopy(result['segments'])
        self.assertEqual(contract.sequence_errors(segments), [])
        self.assertIn('CONTINUITY_GROUP_INCOMPLETE', contract.sequence_errors(segments[:1]))
        segments[0]['ai_selection']['evidence_intervals_us'] = [{'start': 0, 'end': 1_000_000}]
        self.assertIn('CONTINUITY_MEMBER_OBSERVATION_CHANGED', contract.validate_segment_receipt(segments[0])['reasons'])
        for row in self.plan['rows']:
            row.pop('continuity_key')
        with self.assertRaisesRegex(ValueError, 'AI_EDITOR_UNREVIEWED_SELECTION'):
            editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None)
        orphan = copy.deepcopy(result['segments'][0])
        orphan.pop('continuity_group')
        orphan['ai_selection'].pop('continuity_group')
        self.assertIn('CONTINUITY_GROUP_MISSING', contract.validate_segment_receipt(orphan)['reasons'])

    def test_final_solver_keeps_continuity_and_transition_free_sentence_boundary(self):
        result = self.install()
        engine, clips = self.clips(result)
        for clip in clips:
            solved = timing.solve_window(engine._timing_request(clip, 2_000_000))
            self.assertTrue(solved.ok, solved)
            self.assertEqual((solved.source_start_us, solved.source_end_us),
                             (round(clip.source_start * 1e6), round(clip.source_end * 1e6)))
        clips[1].transition = {'type': 'dissolve'}
        with patch('orchestrator.application.engine.stability.recommend_transition', side_effect=AssertionError('No seam effect')):
            self.assertIsNone(engine._transition_for(clips[0], clips[1], cut_inside_span=False))
        self.assertEqual(qc.runtime_errors(clips, result['segments']), [])

    def test_runtime_drift_blocks_both_writers_in_formal_and_preview(self):
        result = self.install()
        engine, clips = self.clips(result)
        for mode in ('formal', 'preview'):
            for backend in ('headless', 'legacy'):
                engine.m.update(delivery_mode=mode, draft_backend=backend)
                bad = copy.deepcopy(clips)
                bad[1].source_start += .01
                with patch.object(headless_production, 'write_draft', side_effect=AssertionError('write forbidden')), \
                        patch.object(engine, '_write_legacy_draft', side_effect=AssertionError('write forbidden')):
                    self.assertEqual(engine.write_draft(bad, None)['failure_code'], 'CONTINUITY_CONFLICT')

    def test_native_plan_preserves_each_audio_and_caption_while_picture_continues(self):
        result = self.install()
        engine, clips = self.clips(result)
        engine.total_us, engine.ffprobe = 4_000_000, 'offline-mocked-probe'
        cues = []
        for i, clip in enumerate(clips):
            audio = self.root / f'audio-{i}.wav'
            audio.write_bytes(b'fixture audio')
            clip.audio_mode, clip.audio = 'file', str(audio)
            cues.append({'text': clip.text, 'start_us': clip.start_us, 'end_us': clip.start_us + 2_000_000})
        with patch.object(headless_production, 'probe_media', return_value={'duration_us': 2_000_000}), \
                patch.object(headless_production, 'layout_subtitle', side_effect=lambda text, *a, **kw: {
                    'text': text, 'font_size_px': 56, 'x': 0, 'y': -.7}):
            plan, _, _ = headless_production.make_plan(engine, clips, None, {}, cues, 'offline', self.shot['video'])
        tracks = {t['name']: t['segments'] for t in plan['tracks']}
        self.assertEqual([(s['source_start_us'], s['source_duration_us'], s['start_us'], s['speed'])
                          for s in tracks['Video_BRoll']], [(0, 2_000_000, 0, 1), (2_000_000, 2_000_000, 2_000_000, 1)])
        self.assertEqual([s['source'] for s in tracks['Narration']], [str(Path(c.audio).resolve()) for c in clips])
        self.assertEqual([s['source_start_us'] for s in tracks['Narration']], [0, 0])
        self.assertEqual([s['text'] for s in tracks['Subtitles']], ['first', 'second'])
        self.assertEqual([s['start_us'] for s in tracks['Subtitles']], [0, 2_000_000])

    def test_unrelated_neighbor_keeps_its_selection_and_audio_budget(self):
        self.manifest['segments'].append({'text': 'unrelated', 'audio_duration_us': 2_000_000})
        self.analysis['shots'].append(self.shots[2])
        self.plan['sentences'].append({'index': 3, 'text': 'unrelated', 'kind': 'selling_point',
            'selling_points': [{'id': 'p1', 'text': 'unrelated'}], 'candidate_ids': ['other']})
        neighbor = {'index': 3, 'parts': [{'shot_id': 'other', 'audio_duration_us': 2_000_000}]}
        self.plan['rows'].append(copy.deepcopy(neighbor))
        self.plan['reviews']['3'] = [self.reviews[2]]
        group = self.group()
        sharing.install(self.plan, contract.digest(group['input'])[:24], group)
        result = editor._assemble(self.manifest, self.analysis, self.plan, narration_timing=None)
        self.assertTrue(result['ok'])
        self.assertEqual(self.plan['rows'][2], neighbor)
        self.assertEqual(result['segments'][2]['shot_id'], 'other')
        self.assertEqual(result['segments'][2]['audio_duration_us'], 2_000_000)

    def test_actual_editable_document_readback_rejects_source_cut_speed_and_gap(self):
        _, clips = self.clips(self.install())
        draft = {'materials': {'videos': [{'id': 'v', 'path': clips[0].video}], 'speeds': [{'id': 'sp', 'type': 'speed', 'speed': 1}]},
                 'tracks': [{'type': 'video', 'segments': [{'material_id': 'v', 'speed': 1, 'extra_material_refs': ['sp'],
                    'source_timerange': {'start': round(c.source_start * 1e6), 'duration': 2_000_000},
                    'target_timerange': {'start': c.start_us, 'duration': 2_000_000}} for c in clips]}]}
        folder = self.root / 'offline-editable-draft'
        folder.mkdir()
        def check(value):
            (folder / 'draft_info.json').write_text(json.dumps(value), encoding='utf-8')
            return qc.draft_errors(folder, clips)
        self.assertEqual(check(draft), [])
        mutations = [lambda d: d['tracks'][0]['segments'][1]['source_timerange'].update(start=2_100_000),
                     lambda d: d['tracks'][0]['segments'][1]['target_timerange'].update(start=2_100_000),
                     lambda d: d['tracks'][0]['segments'][0].update(speed=1.1),
                     lambda d: d['materials']['speeds'][0].update(speed=1.1),
                     lambda d: d['materials']['videos'][0].update(path=self.shots[1]['video'])]
        for mutate in mutations:
            bad = copy.deepcopy(draft)
            mutate(bad)
            self.assertTrue(check(bad))

    def test_independent_qc_keeps_group_together_without_selector_verdicts(self):
        result = self.install()
        claims = [{'index': i, 'claim': s['text'], 'product': 'synthetic',
                   'sentence_kind': 'selling_point', 'ai_segment': s}
                  for i, s in enumerate(result['segments'], 1)]
        batches = ai_editor_qc.claim_batches(claims, 1)
        self.assertEqual([[c['index'] for c in b] for b in batches], [[1, 2]])
        prompt = ai_editor_qc.prompt(claims, 16)
        self.assertIn('同次演示', prompt)
        self.assertNotIn('Same specific benefit', prompt)
        self.assertNotIn('Continuous stages fit the narration', prompt)
        with self.assertRaisesRegex(ValueError, 'CONTINUITY_QC_GROUP_INCOMPLETE'):
            ai_editor_qc.claim_batches(claims[:1], 2)
        with self.assertRaisesRegex(ValueError, 'CONTINUITY_QC_GROUP_INCOMPLETE'):
            ai_editor_qc.prompt(claims[1:], 8)

    def test_normalized_group_copy_keeps_clock_and_original_evidence_binding(self):
        from orchestrator.media import source_normalization as normalization
        result = self.install()
        _, clips = self.clips(result)
        prepared = self.root / 'upright-working.mp4'
        prepared.write_bytes(b'encoded upright pixels differ from original source')
        folder = self.root / 'normalized-editable-draft'
        folder.mkdir()
        owned = folder / 'video.mp4'
        owned.write_bytes(prepared.read_bytes())
        source = result['segments'][0]['continuity_group']['input']['source_identity']
        receipt = {'schema': normalization.VERSION, 'source': source,
            'prepared': normalization.identity(prepared), 'prepared_sha256': normalization.file_digest(prepared),
            'lossless': False, 'quality': {'mode': normalization.QUALITY_MODE}, 'rotation_degrees': 90,
            'frame_clock': {'count': 120}, 'time_mapping': {'offset_us': 0, 'rate_num': 1, 'rate_den': 1}}
        draft = {'materials': {'videos': [{'id': 'v', 'path': 'video.mp4'}]},
                 'tracks': [{'type': 'video', 'segments': [{'material_id': 'v', 'speed': 1,
                    'source_timerange': {'start': round(c.source_start * 1e6), 'duration': 2_000_000},
                    'target_timerange': {'start': c.start_us, 'duration': 2_000_000}} for c in clips]}]}
        (folder / 'draft_info.json').write_text(json.dumps(draft), encoding='utf-8')
        overrides = {str(Path(clips[0].video).resolve()): str(prepared)}
        self.assertEqual(qc.runtime_errors(clips, result['segments']), [])
        self.assertEqual(qc.draft_errors(folder, clips, source_overrides=overrides), [])
        self.assertTrue(ai_editor_qc.source_matches_receipt(str(owned), source, [receipt]))
        self.assertIn('CONTINUITY_DRAFT_SOURCE_CHANGED', qc.draft_errors(folder, clips))
        shifted = copy.deepcopy(receipt)
        shifted['time_mapping']['offset_us'] = 1
        self.assertFalse(ai_editor_qc.source_matches_receipt(str(owned), source, [shifted]))
        owned.write_bytes(b'unrelated replacement')
        self.assertIn('CONTINUITY_DRAFT_SOURCE_CHANGED', qc.draft_errors(folder, clips, source_overrides=overrides))
        self.assertFalse(ai_editor_qc.source_matches_receipt(str(owned), source, [receipt]))


class ReviewRegressionTests(unittest.TestCase):
    setUp = baseline.ProductionPlaybackTests.setUp

    def test_actual_writer_window_overrides_stale_solved_end(self):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        for mode in ('formal', 'preview'):
            for backend in ('headless', 'legacy'):
                engine.m = {'delivery_mode': mode, 'draft_backend': backend}
                for stale_end in (2.95, 1):
                    clip = Clip('synthetic', 0, 1, video_speed=3, solved_window={'source_end': stale_end},
                                playback_observation=process(0, round(stale_end * 1e6)))
                    with patch.object(headless_production, 'write_draft', side_effect=AssertionError('write forbidden')), \
                            patch.object(engine, '_write_legacy_draft', side_effect=AssertionError('write forbidden')):
                        blocked = engine.write_draft([clip], None)
                    self.assertEqual(blocked['failure_code'], 'PLAYBACK_POLICY_CONFLICT')
                    self.assertIn('PLAYBACK_SPEED_CONFLICT', [c['code'] for c in blocked['conflicts']])
        clip = Clip('synthetic', 0, 1, video_speed=3, solved_window={'source_end': 2.999999},
                    playback_observation=process(0, 3_000_000))
        self.assertIsNone(engine._playback_gate([clip]))

    def test_single_candidate_full_window_near_sentence_duration_is_feasible_and_refilled(self):
        review = {**self.reviews[0], 'evidence_intervals_us': [{'start': 0, 'end': 1_990_000}]}
        sentence = self.plan['sentences'][0]
        sentence['audio_durations_us'] = [2_000_000]
        self.assertTrue(editor._cached_reviews_feasible(self.manifest, sentence,
            {'long': self.shots[0]}, [review], {}))
        manifest = {**self.manifest, 'segments': self.manifest['segments'][:1]}
        with patch.object(editor, '_review_pool', return_value=([review], {'feasible': True})):
            result = editor.refill_indices(manifest, self.analysis, {'segments': [], 'pending_items': []}, [1], self.root)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['segments'][0]['video_speed'], 1)
        self.assertEqual(result['segments'][0]['ai_selection']['evidence_intervals_us'], review['evidence_intervals_us'])

    def test_valid_narrow_review_recovery_does_not_call_provider_even_when_broad_invalid(self):
        self.plan['reviews']['1'] = self.reviews[:1]
        self.plan['window_reviews'] = {'shorter': {**copy.deepcopy(self.reviews[0]), 'sentence_index': 1,
            'review_window_us': {'start': 0, 'end': 2_000_000},
            'evidence_intervals_us': [{'start': 0, 'end': 1_000_000}]}}
        self.plan['rows'][0]['parts'][0].update(window_review_key='shorter', source_start_us=0)
        folder = self.root / 'ai_editor'
        folder.mkdir()
        (folder / 'plan.json').write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(editor, '_editor_model', return_value='fixture'), \
                patch.object(editor, '_call', side_effect=ValueError('AI_EDITOR_CALL_FAILED:401')) as call:
            recovered = editor.prepare(self.manifest, self.analysis, self.root)['ai_editor_plan']
        self.assertTrue(recovered['ok'], recovered)
        call.assert_not_called()
        self.assertEqual(recovered['reviews']['1'], self.plan['reviews']['1'])

    def test_unselected_narrow_review_on_same_shot_is_recovered_without_paid_call(self):
        self.plan['reviews']['1'] = self.reviews[:1]
        self.plan['window_reviews'] = {'saved': {**copy.deepcopy(self.reviews[0]), 'sentence_index': 1,
            'review_window_us': {'start': 0, 'end': 2_000_000},
            'evidence_intervals_us': [{'start': 0, 'end': 1_000_000}]}}
        folder = self.root / 'ai_editor'
        folder.mkdir()
        (folder / 'plan.json').write_text(json.dumps(self.plan), encoding='utf-8')
        with patch.object(editor, '_editor_model', return_value='fixture'), \
                patch.object(editor, '_call', side_effect=AssertionError('API forbidden')):
            recovered = editor.prepare(self.manifest, self.analysis, self.root)['ai_editor_plan']
        self.assertTrue(recovered['ok'], recovered)
        self.assertEqual(recovered['rows'][0]['parts'][0]['window_review_key'], 'saved')


if __name__ == '__main__':
    unittest.main()
