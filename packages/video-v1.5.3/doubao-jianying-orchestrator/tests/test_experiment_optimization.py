from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import json
import unittest

import test_experiment_retiming as fixture
from orchestrator import ai_editor_contract as identity
from orchestrator.application import experiment_optimization as program
from orchestrator.application import experiment_production
from orchestrator.domain.timeline import EditTimeline
from tools.weipin_experiment import main


class ExperimentOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.ExperimentRetimingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.base = self.fixture.base
        self.pair = self.fixture.evidence
        self.roles = {'task_id': self.base.manifest['task_id'],
            'rules': identity.rules_identity(self.base.manifest), 'sentences': deepcopy(self.base.roles)}
        self.spec = program.phase_spec_template(self.base.manifest, self.pair, self.fixture.motion)
        self.spec['review_note'] = 'Reviewed fixture phase boundaries.'
        self.directory = self.base.root / 'optimized'
        self.facts = {'duration_us': 10_000_000, 'width': 108, 'height': 192, 'fps_num': 60, 'fps_den': 1}

    def run_program(self, **kwargs):
        options = {'phase_spec': self.spec, 'render': False, **kwargs}
        with patch.object(experiment_production, 'probe_media', return_value=self.facts):
            return program.optimize(self.base.manifest, self.base.timeline, self.roles, self.pair,
                self.base.origins, self.directory, **options)

    def test_reviewed_recipe_reproduces_fixed_audio_edit_and_change_table(self):
        result = self.run_program()
        timeline = EditTimeline.load(self.directory / 'timeline.json')
        self.assertEqual((timeline.sentences, timeline.audios, timeline.subtitles),
            (self.base.timeline.sentences, self.base.timeline.audios, self.base.timeline.subtitles))
        self.assertTrue(all(result['preserved'].values()))
        self.assertEqual(result['duration_us'], 4_000_000)
        self.assertEqual(result['method'], 'reviewed_phase_specification')
        self.assertFalse(result['formal_delivery_allowed'])
        self.assertEqual(len(json.loads((self.directory / 'changes.json').read_text('utf-8'))), 2)
        self.assertTrue((self.directory / 'REPORT.md').is_file())

    def test_bound_motion_is_also_supported_without_relabeling_it_as_manual_review(self):
        result = self.run_program(phase_spec=None, motion=self.fixture.motion)
        self.assertEqual(result['status'], 'PLANNED_OUTPUT_REVIEW_PENDING')
        self.assertNotEqual(result['method'], 'reviewed_phase_specification')

    def test_swapped_recipe_sources_are_rejected_before_rendering(self):
        members = self.spec['members']
        members[0]['source'], members[1]['source'] = members[1]['source'], members[0]['source']
        with patch.object(program, 'render_timeline') as render:
            with self.assertRaisesRegex(ValueError, 'SOURCE_CHANGED'):
                self.run_program(render=True)
        render.assert_not_called()
        self.assertEqual(json.loads((self.directory / 'result.json').read_text('utf-8'))['status'], 'BLOCKED')

    def test_missing_review_or_unprotected_contact_is_not_silently_accepted(self):
        self.spec['review_note'] = ''
        with self.assertRaisesRegex(ValueError, 'REVIEW_REQUIRED'):
            self.run_program()
        self.spec['review_note'] = 'reviewed'
        self.spec['members'][1]['observation']['segments'][0]['can_speed'] = True
        self.directory = self.base.root / 'different-output'
        with self.assertRaisesRegex(ValueError, 'CONTACT_OR_COMPLETION'):
            self.run_program()

    def test_existing_output_is_preserved(self):
        self.run_program()
        before = (self.directory / 'result.json').read_bytes()
        with self.assertRaises(FileExistsError):
            self.run_program()
        self.assertEqual((self.directory / 'result.json').read_bytes(), before)

    def test_same_path_replaced_media_cannot_reuse_old_phase_spec(self):
        before = deepcopy(self.spec)
        samples = self.pair['members'][1]['samples']
        for key in ('original', 'source', 'video'):
            path = Path(samples[key]['path'])
            path.write_bytes(b'replaced physical media at same path')
            samples[key] = identity.source_identity(path)
        with self.assertRaisesRegex(ValueError, 'REVIEW_INPUT_CHANGED'):
            program.bind_phase_spec(self.base.manifest, self.pair, before)

    def test_same_source_changed_sampling_map_or_window_requires_new_phase_review(self):
        self.pair['members'][1]['samples']['frames'][1]['source_frame'] += 1
        with self.assertRaisesRegex(ValueError, 'REVIEW_INPUT_CHANGED'):
            program.bind_phase_spec(self.base.manifest, self.pair, self.spec)
        self.pair['members'][1]['samples']['frames'][1]['source_frame'] -= 1
        self.pair['members'][1]['window_frame_ids'][1] = 39
        with self.assertRaisesRegex(ValueError, 'REVIEW_INPUT_CHANGED'):
            program.bind_phase_spec(self.base.manifest, self.pair, self.spec)

    def test_generated_template_cannot_be_used_without_recording_a_review(self):
        template = program.phase_spec_template(self.base.manifest, self.pair)
        with self.assertRaisesRegex(ValueError, 'REVIEW_REQUIRED'):
            program.bind_phase_spec(self.base.manifest, self.pair, template)

    def test_phase_lag_is_recorded_instead_of_claiming_sentence_level_acceptance(self):
        self.roles['sentences'][0]['role'] = 'rewet'
        result = self.run_program()
        self.assertTrue(any(r['sentence_id'] == 's1' and r['lag_us'] > 0 for r in result['phase_alignment']))
        self.assertFalse(result['formal_delivery_allowed'])

    def test_render_failure_leaves_an_explicit_blocked_result(self):
        with patch.object(program, 'render_timeline', side_effect=RuntimeError('TEST_DECODE_FAILED')):
            with self.assertRaisesRegex(RuntimeError, 'TEST_DECODE_FAILED'):
                self.run_program(render=True)
        result = json.loads((self.directory / 'result.json').read_text('utf-8'))
        self.assertEqual(result['status'], 'BLOCKED')
        self.assertFalse(result['formal_delivery_allowed'])

    def test_normal_cli_executes_recipe_without_the_private_trial_script(self):
        root = self.base.root
        files = {'manifest': self.base.manifest, 'roles': self.roles, 'evidence': self.pair,
            'prior-origins': self.base.origins, 'phase-spec': self.spec}
        args = ['optimize', '--timeline', str(root / 'fixed-timeline.json'), '--report-dir', str(self.directory), '--no-render']
        self.base.timeline.save(root / 'fixed-timeline.json')
        for flag, value in files.items():
            path = root / (flag + '.json')
            path.write_text(json.dumps(value), encoding='utf-8')
            args.extend(['--' + flag, str(path)])
        with patch.object(experiment_production, 'probe_media', return_value=self.facts):
            self.assertEqual(main(args), 0)
        self.assertTrue((self.directory / 'timeline.json').is_file())


if __name__ == '__main__':
    unittest.main()
