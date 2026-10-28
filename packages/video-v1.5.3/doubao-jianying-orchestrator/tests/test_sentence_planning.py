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

    def test_model_plans_unpunctuated_claims_without_rewriting(self):
        script = '纸张厚实柔软亲肤使劲揉搓也不掉絮不含荧光剂湿水不易破'
        plan = ['纸张厚实柔软亲肤', '使劲揉搓也不掉絮', '不含荧光剂', '湿水不易破']
        result = sentence_planner.plan({'full_script': script}, Path('.'),
                                       call=lambda prompt, path: {'segments': plan})
        self.assertEqual(result['segments'], [{'text': x} for x in plan])
        self.assertEqual(timing_contract.split_sentences(result), result['segments'])

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
            path.write_text(json.dumps({'full_script': '原文内容', 'task_id': 'sentence-test',
                                        'temporary_material_analysis': True, 'selection_mode': 'rule_then_ai'}), encoding='utf8')
            args = argparse.Namespace(input=str(path), report_dir=str(root / 'report'),
                                      trigger_phrase='', delivery_mode='formal')
            with mock.patch.object(cli, '_manifest_sources', return_value=[]), \
                 mock.patch.object(cli, 'environment_report', return_value={'ready': True}), \
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
            path.write_text(json.dumps({'full_script': '原文内容', 'task_id': 'sentence-test',
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
            with mock.patch.object(cli, '_temporary_match', return_value=({'ok': True}, None)) as match:
                self.assertEqual(cli.cmd_recover(args), 0)
            self.assertEqual(match.call_args.kwargs['narration_timing'], timing)

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
