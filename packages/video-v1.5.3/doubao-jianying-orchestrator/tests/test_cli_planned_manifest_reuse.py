"""Frozen semantic plans remain reusable with a fresh report destination."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import cli, sentence_planner


class PlannedManifestReuseTests(unittest.TestCase):
    def test_build_reuses_frozen_plan_in_new_report_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / 'new-report'
            segments = [{'text': '纸张厚实亲肤，湿水不破。'}, {'text': '立即下单'}]
            manifest = {'task_id': 'reuse-plan', 'segments': segments,
                        'sentence_plan_version': 'semantic-v1',
                        'temporary_material_analysis': True,
                        'selection_mode': 'rule_then_ai'}
            path = root / 'manifest.json'
            path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf8')
            timing = {'rows': [{'key': 's1', 'text': segments[0]['text'],
                                'audio_duration_us': 1234567}]}
            args = argparse.Namespace(input=str(path), report_dir=str(report),
                                      trigger_phrase='', delivery_mode='formal')
            with mock.patch.object(cli, '_manifest_sources', return_value=[]), \
                 mock.patch.object(cli, 'environment_report', return_value={'ready': True}), \
                 mock.patch.object(sentence_planner, 'plan') as plan, \
                 mock.patch.object(cli, '_lock_voice_once', return_value=({}, None)), \
                 mock.patch.object(cli, '_pre_synthesize_narration', return_value=timing) as tts, \
                 mock.patch.object(cli, '_temporary_match', return_value=(manifest, {
                     'status': 'TEST_MATCH_REACHED', 'safe_to_continue': False})) as match:
                self.assertEqual(cli.cmd_build(args), 1)
            plan.assert_not_called()
            tts.assert_called_once()
            match.assert_called_once()
            self.assertEqual(match.call_args.kwargs['narration_timing'], timing)
            saved = json.loads((report / 'sentence_planning' / 'planned_manifest.json').read_text(encoding='utf8'))
            self.assertEqual(saved['segments'], segments)
            self.assertEqual(saved['sentence_plan_version'], 'semantic-v1')
            self.assertEqual(saved['narration_timing_file'], str(report / 'narration_timing.json'))
            result = json.loads((report / 'last_result.json').read_text(encoding='utf8'))
            self.assertEqual(result['status'], 'TEST_MATCH_REACHED')
