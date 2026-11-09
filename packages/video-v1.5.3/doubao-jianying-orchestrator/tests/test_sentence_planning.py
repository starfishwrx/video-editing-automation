"""Planning boundaries are shared by narration, notes and AI selection."""
from pathlib import Path
import sys
import unittest
import argparse
import json
import tempfile
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import timing_contract, shot_notes, sentence_planner, cli


class SentencePlanningTests(unittest.TestCase):
    def split(self, text, **extra):
        return [s['text'] for s in timing_contract.split_sentences(
            {'full_script': text, 'selection_mode': 'ai_editor', **extra})]

    def test_revision_feedback_reaches_planner_and_preserves_script(self):
        captured = []
        result = sentence_planner.plan({'full_script': '第一句，第二句。', 'revision_feedback': '按原文断句'}, Path('.'),
            call=lambda prompt, path: captured.append(prompt) or {'segments': ['第一句，', '第二句。']})
        self.assertIn('按原文断句', captured[0])
        self.assertEqual(''.join(row['text'] for row in result['segments']), '第一句，第二句。')

    def test_separate_punctuated_selling_points_without_losing_text(self):
        text = '纸张厚实亲肤，4层加厚，随便揉搓不掉絮，湿水不破，一纸多用'
        pieces = self.split(text)
        self.assertEqual(pieces, ['纸张厚实亲肤，', '4层加厚，', '随便揉搓不掉絮，', '湿水不破，', '一纸多用'])
        self.assertEqual(''.join(pieces), text)

    def test_incomplete_prefixes_and_numbers_stay_intact(self):
        text = '来得早的，还送定制纸巾支架，趁划算，快紧来直播间安排上吧'
        self.assertEqual(self.split(text), ['来得早的，还送定制纸巾支架，', '趁划算，快紧来直播间安排上吧'])
        self.assertEqual(self.split('1,000片，10:30开播'), ['1,000片，', '10:30开播'])
        self.assertEqual(self.split('产品（柔软，亲肤）很好用，立即购买'), ['产品（柔软，亲肤）很好用，', '立即购买'])

    def test_unpunctuated_long_text_never_cut_inside_words(self):
        text = '这个真的很适合日常出门随手装在包里面想用的时候拿出来特别方便而且不占地方'
        self.assertEqual(self.split(text), [text])

    def test_explicit_segments_and_legacy_are_unchanged(self):
        segments = [{'text': '纸张厚实，湿水不破', 'audio_duration_us': 123}]
        self.assertEqual(timing_contract.split_sentences({'segments': segments}), segments)
        self.assertEqual(timing_contract.split_sentences({'full_script': '厚实柔软，湿水不破', 'selection_mode': 'legacy'}), [{'text': '厚实柔软，湿水不破'}])

    def test_notes_follow_the_same_sentence_indices(self):
        m = {'full_script': '纸张厚实亲肤，湿水不破', 'selection_mode': 'ai_editor',
             'shot_notes': '湿水不破：展示完整实验'}
        planned = shot_notes.apply(m)
        self.assertEqual(shot_notes.note_for(planned, 2), '展示完整实验')
        self.assertEqual([x['text'] for x in timing_contract.split_sentences(planned)],
                         ['纸张厚实亲肤，', '湿水不破'])

    def test_numbered_raw_notes_follow_original_sentences_after_clause_splitting(self):
        manifest = {'full_script': '纸张厚实亲肤，湿水不破。立即下单', 'selection_mode': 'ai_editor',
                    'shot_notes': '第1句：保留实验\n第2句：展示包装',
                    'shot_notes_index_basis': 'original_sentence'}
        planned = shot_notes.apply(manifest)
        self.assertEqual([shot_notes.note_for(planned, i) for i in (1, 2, 3)],
                         ['保留实验', '保留实验', '展示包装'])
        self.assertEqual(planned['shot_notes_index_basis'], 'original_sentence')
        self.assertEqual(shot_notes.apply(planned), planned)

    def test_existing_binding_and_explicit_segments_do_not_change_numbering(self):
        frozen = {'full_script': '纸张厚实亲肤，湿水不破。立即下单', 'selection_mode': 'ai_editor',
                  'shot_notes': '第2句：保留实验', 'shot_notes_by_index': {'2': '已冻结的备注'}}
        self.assertEqual(shot_notes.apply(frozen)['shot_notes_by_index'], {'2': '已冻结的备注'})
        explicit = {'segments': [{'text': '纸张厚实亲肤，湿水不破'}, {'text': '立即下单'}],
                    'shot_notes': '第2句：展示包装'}
        self.assertEqual(shot_notes.apply(explicit)['segments'][1]['shot_note'], '展示包装')
        old_numbering = {**frozen, 'shot_notes_index_basis': 'split'}
        old_numbering.pop('shot_notes_by_index')
        self.assertEqual(shot_notes.note_for(shot_notes.apply(old_numbering), 2), '保留实验')
        old_numbering.pop('shot_notes_index_basis')
        self.assertEqual(shot_notes.note_for(shot_notes.apply(old_numbering), 2), '保留实验')

    def test_numbered_note_outside_original_sentences_is_reported_unmatched(self):
        manifest = {'full_script': '纸张厚实亲肤，湿水不破', 'selection_mode': 'ai_editor',
                    'shot_notes': '第2句：展示包装', 'shot_notes_index_basis': 'original_sentence'}
        planned = shot_notes.apply(manifest)
        self.assertEqual(planned['shot_notes_by_index'], {})
        self.assertEqual(planned['shot_notes_unmatched'], ['第2句：展示包装'])
        self.assertEqual(shot_notes.apply(planned), planned)

    def test_empty_input_mapping_does_not_hide_unparsed_notes(self):
        planned = shot_notes.apply({'full_script': '纸张厚实亲肤，湿水不破', 'selection_mode': 'ai_editor',
            'shot_notes_by_index': {}, 'shot_notes': '湿水不破：展示实验'})
        self.assertEqual(shot_notes.note_for(planned, 2), '展示实验')
        self.assertEqual(planned['shot_notes_binding_version'], 'v1')

    def test_model_plans_unpunctuated_claims_without_rewriting(self):
        script = '纸张厚实柔软亲肤使劲揉搓也不掉絮不含荧光剂湿水不易破'
        plan = ['纸张厚实柔软亲肤', '使劲揉搓也不掉絮', '不含荧光剂', '湿水不易破']
        result = sentence_planner.plan({'full_script': script}, Path('.'),
                                       call=lambda prompt, path: {'segments': plan})
        self.assertEqual(result['segments'], [{'text': x} for x in plan])
        self.assertEqual(timing_contract.split_sentences(result), result['segments'])

    def test_numbered_notes_preserve_natural_boundaries_in_semantic_plan(self):
        manifest = {'full_script': '纸张厚实。湿水不破', 'selection_mode': 'ai_editor',
            'shot_notes_index_basis': 'original_sentence', 'shot_notes': '第1句：展示包装\n第2句：展示实验'}
        planned = sentence_planner.plan(manifest, Path('.'),
            call=lambda *_args: {'segments': ['纸张厚实。湿水不破']})
        bound = shot_notes.apply(planned)
        self.assertEqual([s['text'] for s in bound['segments']], ['纸张厚实。', '湿水不破'])
        self.assertEqual([s['shot_note'] for s in bound['segments']], ['展示包装', '展示实验'])

        for mark in ('！！', '？！'):
            text = '纸张厚实' + mark + '湿水不破'
            source = {**manifest, 'full_script': text}
            result = sentence_planner.plan(source, Path('.'), call=lambda *_args: {'segments': [text]})
            bound = shot_notes.apply(result)
            self.assertEqual([s['text'] for s in bound['segments']], ['纸张厚实' + mark, '湿水不破'])
            self.assertEqual([s['shot_note'] for s in bound['segments']], ['展示包装', '展示实验'])

    def test_semantic_planning_maps_fragment_notes_and_natural_sentence_numbers(self):
        manifest = {'full_script': '厚实柔软亲肤也不掉絮。湿水不破', 'selection_mode': 'ai_editor',
            'shot_notes_index_basis': 'original_sentence', 'shot_notes': '第1句：展示触感\n湿水不破：展示实验'}
        planned = sentence_planner.plan(manifest, Path('.'),
            call=lambda *_args: {'segments': ['厚实柔软亲肤', '也不掉絮。', '湿水不破']})
        bound = shot_notes.apply(planned)
        self.assertEqual([s['shot_note'] for s in bound['segments']], ['展示触感', '展示触感', '展示实验'])

    def test_frozen_cross_sentence_segment_cannot_rebind_numbered_notes(self):
        manifest = {'full_script': '纸张厚实。湿水不破', 'selection_mode': 'ai_editor',
            'segments': [{'text': '纸张厚实。湿水不破'}], 'sentence_plan_version': 'semantic-v1',
            'shot_notes_index_basis': 'original_sentence', 'shot_notes': '第1句：展示包装\n第2句：展示实验'}
        with self.assertRaisesRegex(ValueError, 'SHOT_NOTES_SEGMENT_CROSSES_ORIGINAL_SENTENCES'):
            shot_notes.apply(manifest)

    def test_fixed_audio_segments_map_numbered_notes_to_original_sentence(self):
        manifest = {'full_script': '纸张厚实亲肤，湿水不破。立即下单', 'selection_mode': 'ai_editor',
            'segments': [{'text': '纸张厚实亲肤，', 'audio': 'a.wav'}, {'text': '湿水不破。', 'audio': 'b.wav'},
                         {'text': '立即下单', 'audio': 'c.wav'}], 'shot_notes_index_basis': 'original_sentence',
            'shot_notes': '第2句：展示包装'}
        bound = shot_notes.apply(manifest)
        self.assertEqual([s.get('shot_note') for s in bound['segments']], [None, None, '展示包装'])
        self.assertEqual([s['audio'] for s in bound['segments']], ['a.wav', 'b.wav', 'c.wav'])

    def test_frozen_unreferenced_sentences_can_share_an_audio_segment(self):
        manifest = {'full_script': '纸张厚实。湿水不破。立即下单', 'selection_mode': 'ai_editor',
            'segments': [{'text': '纸张厚实。', 'audio': 'a.wav'}, {'text': '湿水不破。立即下单', 'audio': 'b.wav'}],
            'shot_notes_index_basis': 'original_sentence', 'shot_notes': '第1句：展示包装'}
        bound = shot_notes.apply(manifest)
        self.assertEqual([s.get('shot_note') for s in bound['segments']], ['展示包装', None])
        self.assertEqual([s['audio'] for s in bound['segments']], ['a.wav', 'b.wav'])

    def test_model_cannot_drop_reorder_or_change_content(self):
        source = '心相印100%原生木浆，不含荧光剂'
        for parts in [['心相印100%木浆，', '不含荧光剂'], ['不含荧光剂', '心相印100%原生木浆，'],
                      ['心相印100%原生木浆', '不含荧光剂']]:
            with self.subTest(parts=parts), self.assertRaisesRegex(ValueError, 'TEXT_CHANGED'):
                sentence_planner.validate(source, {'segments': parts})

    def test_model_cannot_split_brand_number_or_empty_cue(self):
        for text, parts, error in [('心相印纸巾', ['心相', '印纸巾'], 'SPLIT_TOKEN'),
                                   ('每包1,000片', ['每包1,', '000片'], 'SPLIT_TOKEN'),
                                   ('含量100%', ['含量100', '%'], 'SPLIT_TOKEN'),
                                   ('买140片', ['买140', '片'], 'SPLIT_TOKEN'),
                                   ('产品，真好', ['产品', '，', '真好'], 'UNREADABLE')]:
            with self.subTest(parts=parts), self.assertRaisesRegex(ValueError, error):
                sentence_planner.validate(text, {'segments': parts})

    def test_failed_model_plan_never_becomes_a_manifest(self):
        with self.assertRaisesRegex(ValueError, 'INVALID_SEGMENTS'):
            sentence_planner.plan({'full_script': '正文'}, Path('.'),
                                  call=lambda prompt, path: {'segments': []})

    def test_revision_feedback_is_context_not_evidence(self):
        from orchestrator import ai_editor_contract
        prompt = ai_editor_contract.policy_prompt({'category': '纸品', 'revision_feedback': '揉搓动作不完整'})
        self.assertIn('揉搓动作不完整', prompt)
        self.assertIn('时间码属于旧稿', prompt)
        self.assertIn('不能据此虚构画面', prompt)

    def test_source_whitespace_is_preserved_inside_phrase(self):
        result = sentence_planner.validate('这款 Hello Kitty 纸巾\n厚实柔软',
                                           {'segments': ['这款HelloKitty纸巾', '厚实柔软']})
        self.assertEqual(result, [{'text': '这款 Hello Kitty 纸巾'}, {'text': '厚实柔软'}])

    def test_build_stops_before_voice_when_model_changes_script(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / 'manifest.json'
            path.write_text(json.dumps({'full_script': '原文内容', 'task_id': 'sentence-test', 'draft_backend': 'legacy',
                                        'temporary_material_analysis': True, 'selection_mode': 'rule_then_ai'}), encoding='utf8')
            args = argparse.Namespace(input=str(path), report_dir=str(root / 'report'),
                                      trigger_phrase='', delivery_mode='formal')
            with mock.patch.object(cli, '_manifest_sources', return_value=[]), \
                 mock.patch.object(cli, 'environment_report', return_value={'ready': True}), \
                 mock.patch.object(cli, '_production_environment_report', return_value={'ready': True}), \
                 mock.patch.object(sentence_planner, 'plan', side_effect=ValueError('SENTENCE_PLAN_TEXT_CHANGED')), \
                 mock.patch.object(cli, '_lock_voice_once') as voice:
                self.assertEqual(cli.cmd_build(args), 1)
            voice.assert_not_called()
            saved = json.loads((root / 'report' / 'last_result.json').read_text(encoding='utf8'))
            self.assertEqual(saved['status'], 'SENTENCE_PLAN_REVIEW_REQUIRED')

    def test_existing_indexed_notes_are_not_reindexed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / 'manifest.json'
            path.write_text(json.dumps({'full_script': '原文内容', 'task_id': 'sentence-test', 'draft_backend': 'legacy',
                'temporary_material_analysis': True, 'shot_notes_by_index': {'1': '完整动作'}}), encoding='utf8')
            args = argparse.Namespace(input=str(path), report_dir=str(root / 'report'),
                                      trigger_phrase='', delivery_mode='formal')
            with mock.patch.object(cli, '_manifest_sources', return_value=[]), \
                 mock.patch.object(cli, 'environment_report', return_value={'ready': True}), \
                 mock.patch.object(sentence_planner, 'plan') as plan, \
                 mock.patch.object(cli, '_lock_voice_once', return_value=(None, {'status': 'VOICE_SELECTION_REQUIRED'})):
                self.assertEqual(cli.cmd_build(args), 1)
            plan.assert_not_called()

    def test_recovery_uses_frozen_sentences_and_matching_audio_timing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            timing = {'rows': [{'key': 's1', 'text': '湿水不破', 'audio_duration_us': 1234567}]}
            (root / 'timing.json').write_text(json.dumps(timing, ensure_ascii=False), encoding='utf8')
            (root / 'analysis.json').write_text('{}', encoding='utf8')
            m = {'segments': [{'text': '湿水不破'}], 'sentence_plan_version': 'semantic-v1',
                 'narration_timing_file': str(root / 'timing.json'), 'shot_analysis_file': str(root / 'analysis.json')}
            path = root / 'manifest.json'; path.write_text(json.dumps(m, ensure_ascii=False), encoding='utf8')
            args = argparse.Namespace(input=str(path), report_dir=str(root / 'report'), stage='semantic_match')
            with mock.patch.object(cli, 'environment_report', return_value={'ready': True}), \
                 mock.patch.object(cli, '_production_environment_report', return_value={'ready': True}), \
                 mock.patch.object(cli, '_temporary_match', return_value=({'ok': True}, None)) as match:
                self.assertEqual(cli.cmd_recover(args), 0)
            self.assertEqual(match.call_args.kwargs['narration_timing'], timing)

    def test_match_and_semantic_recovery_bind_notes_without_rewriting_original_input(self):
        from orchestrator.selection import ai_editor
        from orchestrator.vision import material_cache
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = {'full_script': '纸张厚实亲肤，湿水不破。立即下单', 'selection_mode': 'ai_editor',
                'shot_notes': '第2句：展示包装', 'shot_notes_index_basis': 'original_sentence',
                'material_sources': ['fixture.mp4'], 'record_id': 'fresh-direct', 'max_source_reuse': 2,
                'clip_selection_policy': {'mode': 'script_driven_semantic_match'}}
            input_path = root / 'manifest.json'
            input_path.write_text(json.dumps(source, ensure_ascii=False), encoding='utf8')
            analysis = root / 'analysis.json'
            analysis.write_text('{"shots": []}', encoding='utf8')
            args = argparse.Namespace(input=str(input_path), analysis=str(analysis), report_dir=str(root / 'match'),
                                      topk=3, require_selection=False)
            prepared = []
            def prepare(manifest, facts, report_dir):
                prepared.append(manifest)
                return facts
            def match(manifest, facts, **kwargs):
                return {'ok': True, 'segments': [], 'manifest': manifest}
            with mock.patch.object(cli, 'environment_report', return_value={}), \
                 mock.patch.object(material_cache, 'refresh_external_analysis', side_effect=lambda a, *x, **k: a), \
                 mock.patch.object(ai_editor, 'snapshot_previous_analysis'), \
                 mock.patch.object(ai_editor, 'prepare', side_effect=prepare), \
                 mock.patch.object(cli.shot_analyzer, 'match_manifest', side_effect=match):
                self.assertEqual(cli.cmd_match_shots(args), 0)
            self.assertEqual(prepared[0]['shot_notes_by_index'], {'3': '展示包装'})
            self.assertEqual(prepared[0]['source_reuse_policy'], 'strict-v1')
            report = json.loads((root / 'match' / 'shot_match_report.json').read_text(encoding='utf8'))
            self.assertEqual(report['input_manifest'], source)

            source['shot_analysis_file'] = str(analysis)
            input_path.write_text(json.dumps(source, ensure_ascii=False), encoding='utf8')
            args = argparse.Namespace(input=str(input_path), report_dir=str(root / 'recover'), stage='semantic_match')
            with mock.patch.object(cli, 'environment_report', return_value={'ready': True}), \
                 mock.patch.object(cli, '_production_environment_report', return_value={'ready': True}), \
                 mock.patch.object(cli, '_temporary_match', side_effect=lambda m, *_a, **_k: (m, None)) as recover:
                self.assertEqual(cli.cmd_recover(args), 0)
            self.assertEqual(recover.call_args.args[0]['shot_notes_by_index'], {'3': '展示包装'})

    def test_invalid_limits_invalidate_old_success_before_match_or_recovery(self):
        from orchestrator.selection import ai_editor
        for stage, field in (('match', 'max_absorb_demo_speed'), ('semantic_match', 'max_source_reuse'),
                             ('match', 'max_source_reuse')):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                report = root / 'report'
                report.mkdir()
                (report / 'shot_match_report.json').write_text('{"ok": true}', encoding='utf8')
                manifest = {'material_sources': ['fixture.mp4'], field: 1.5 if stage == 'match'
                    and field == 'max_source_reuse' else 'invalid', 'record_id': 'fresh-direct',
                    'clip_selection_policy': {'mode': 'script_driven_semantic_match'}}
                input_path = root / 'manifest.json'
                input_path.write_text(json.dumps(manifest), encoding='utf8')
                with mock.patch.object(ai_editor, 'prepare') as prepare:
                    if stage == 'match':
                        args = argparse.Namespace(input=str(input_path), analysis=str(root / 'missing.json'),
                            report_dir=str(report), topk=3, require_selection=False)
                        self.assertEqual(cli.cmd_match_shots(args), 1)
                    else:
                        args = argparse.Namespace(input=str(input_path), report_dir=str(report), stage=stage)
                        self.assertEqual(cli.cmd_recover(args), 1)
                prepare.assert_not_called()
                current = json.loads((report / 'shot_match_report.json').read_text(encoding='utf8'))
                self.assertFalse(current['ok'])
                self.assertEqual(current['input_manifest'], manifest)
                self.assertTrue(list((report / 'match_history').glob('*.json')))

    def test_invalid_semantic_note_binding_is_structured_and_releases_recovery_lock(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            timing = {'rows': [{'key': 's1', 'text': '错误文本', 'audio_duration_us': 1_000_000}]}
            (root / 'timing.json').write_text(json.dumps(timing, ensure_ascii=False), encoding='utf8')
            manifest = {'full_script': '原始文案', 'segments': [{'text': '错误文本'}],
                'sentence_plan_version': 'semantic-v1', 'narration_timing_file': str(root / 'timing.json'),
                'shot_notes_index_basis': 'original_sentence', 'shot_notes': '第1句：展示包装'}
            source = root / 'manifest.json'
            source.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf8')
            args = argparse.Namespace(input=str(source), report_dir=str(root / 'report'), stage='semantic_match')
            with mock.patch.object(cli, '_temporary_match') as match:
                self.assertEqual(cli.cmd_recover(args), 1)
                self.assertEqual(cli.cmd_recover(args), 1)
            match.assert_not_called()
            result = json.loads((root / 'report' / 'last_result.json').read_text(encoding='utf8'))
            self.assertEqual(result['status'], 'SHOT_NOTES_BINDING_INVALID')
            self.assertEqual(result['reason'], 'SHOT_NOTES_SOURCE_SEGMENTS_CHANGED')

    def test_build_reuses_frozen_plan_in_fresh_report_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            report = root / 'new-report'
            segments = [{'text': '纸张厚实亲肤，湿水不破。'}, {'text': '立即下单'}]
            manifest = {'task_id': 'reuse-plan', 'segments': segments, 'draft_backend': 'legacy',
                        'sentence_plan_version': 'semantic-v1',
                        'temporary_material_analysis': True,
                        'selection_mode': 'rule_then_ai'}
            path = root / 'manifest.json'
            path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf8')
            timing = {'rows': [{'key': f's{i}', 'text': segment['text'],
                                'audio_duration_us': 1234567}
                               for i, segment in enumerate(segments, 1)]}
            args = argparse.Namespace(input=str(path), report_dir=str(report),
                                      trigger_phrase='', delivery_mode='formal')
            with mock.patch.object(cli, '_manifest_sources', return_value=[]), \
                 mock.patch.object(cli, 'environment_report', return_value={'ready': True}), \
                 mock.patch.object(cli, '_production_environment_report', return_value={'ready': True}), \
                 mock.patch.object(sentence_planner, 'plan') as planner, \
                 mock.patch.object(cli, '_lock_voice_once', return_value=({}, None)), \
                 mock.patch.object(cli, '_pre_synthesize_narration', return_value=timing), \
                 mock.patch.object(cli, '_temporary_match', return_value=(manifest, {
                     'status': 'TEST_MATCH_REACHED', 'safe_to_continue': False})) as match:
                self.assertEqual(cli.cmd_build(args), 1)
            planner.assert_not_called()
            match.assert_called_once()
            self.assertEqual(match.call_args.kwargs['narration_timing'], timing)
            saved = json.loads((report / 'sentence_planning' / 'planned_manifest.json')
                               .read_text(encoding='utf8'))
            self.assertEqual(saved['segments'], segments)
            self.assertEqual(saved['sentence_plan_version'], 'semantic-v1')
            self.assertEqual(saved['narration_timing_file'], str(report / 'narration_timing.json'))
            result = json.loads((report / 'last_result.json').read_text(encoding='utf8'))
            self.assertEqual(result['status'], 'TEST_MATCH_REACHED')

    def test_recovery_rejects_changed_or_missing_sentence_timing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'timing.json').write_text(json.dumps({'rows': [{'key': 's1', 'text': '另一句', 'audio_duration_us': 10}]}), encoding='utf8')
            m = {'segments': [{'text': '湿水不破'}], 'sentence_plan_version': 'semantic-v1',
                 'narration_timing_file': str(root / 'timing.json')}
            path = root / 'manifest.json'; path.write_text(json.dumps(m, ensure_ascii=False), encoding='utf8')
            args = argparse.Namespace(input=str(path), report_dir=str(root / 'report'), stage='semantic_match')
            with mock.patch.object(cli, '_temporary_match') as match:
                self.assertEqual(cli.cmd_recover(args), 1)
            match.assert_not_called()
            saved = json.loads((root / 'report' / 'last_result.json').read_text(encoding='utf8'))
            self.assertEqual(saved['status'], 'RECOVERY_SENTENCE_TIMING_REQUIRED')


if __name__ == '__main__':
    unittest.main()
