from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import test_experiment_retiming as fixture
from orchestrator.application import experiment_production as app
from orchestrator.selection.experiment_events import compile_events,validate_focus,fit_phase_windows
from orchestrator.selection.experiment_retiming import windows,fit_group
from orchestrator.vision import experiment_event_review as reviewer


def facts(part):
    stages=[('pour',0,31),('absorb',32,40)] if part=='A' else [('press',0,30),('lift',31,33),('result',34,40)]
    return {'verdict':'observed','coverage':'complete_pour' if part=='A' else 'complete_verification',
        **({'contact_first_frame':0,'release_first_frame':31,'result_first_frame':34} if part=='B' else {}),
        'effective_window_frame_ids':[0,40],'events':[{'kind':k,'first_frame':a,'last_frame':b,
            'observed':'actual visible event'} for k,a,b in stages],'limitations':'sampled event boundaries'}


class ExperimentEventsTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.ExperimentRetimingTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.pair=self.fixture.evidence;self.base=self.fixture.base

    def test_absorption_middle_can_speed_while_final_state_stays_realtime(self):
        row=compile_events(facts('A'),self.pair['members'][0])
        absorb=[s for s in row['segments'] if s['kind']=='absorb']
        self.assertEqual([s['can_speed'] for s in absorb],[False,True,False])
        self.assertEqual((absorb[-1]['first_frame'],absorb[-1]['last_frame']),(37,40))

    def test_short_absorption_state_is_rejected_using_actual_source_pts(self):
        for start in (38,39,40):
            row=facts('A');row['events'][0]['last_frame']=start-1;row['events'][1]['first_frame']=start
            with self.assertRaisesRegex(ValueError,'ABSORPTION_STATE_TOO_SHORT'):
                compile_events(row,self.pair['members'][0])
        compiled=compile_events(facts('A'),self.pair['members'][0])
        clips=windows(self.pair['members'],[compiled,compile_events(facts('B'),self.pair['members'][1])],15)
        self.assertEqual(clips[0]['protected_intervals'][-1]['min_output_frames'],9)
        compiled['segments'][-1]['first_frame']=39;compiled['segments'][-2]['last_frame']=38
        with self.assertRaisesRegex(ValueError,'ABSORPTION_STATE_TOO_SHORT'):
            windows(self.pair['members'],[compiled,compile_events(facts('B'),self.pair['members'][1])],15)

    def test_press_contact_completion_lift_and_result_remain_protected(self):
        row=compile_events(facts('B'),self.pair['members'][1])
        locked=[s for s in row['segments'] if not s['can_speed']]
        self.assertEqual([(s['kind'],s['first_frame'],s['last_frame']) for s in locked],
            [('press',0,0),('press',29,30),('lift',31,33),('result',34,40)])

    def test_missing_or_overlapping_event_ranges_are_rejected(self):
        for mutate in (lambda r:r['events'].pop(),lambda r:r['events'][1].update(first_frame=31)):
            row=facts('A');mutate(row)
            with self.assertRaisesRegex(ValueError,'EVENTS_'):compile_events(row,self.pair['members'][0])

    def test_absorption_focus_is_explicit_and_only_for_qualitative_absorption(self):
        rows=[{'coverage':'absorption_focus','segments':[]},{}];roles=[{'sentence_id':'s','role':'absorption'}]
        validate_focus([SimpleNamespace(sentence_id='s',text='吸收真的贼牛')],roles,rows)
        for text in ('两秒吸收','瞬间吸干','整杯吸收','倒入后快速吸收','吸收100ml'):
            with self.assertRaisesRegex(ValueError,'INCOMPATIBLE_CLAIM'):
                validate_focus([SimpleNamespace(sentence_id='s',text=text)],roles,rows)

    def test_unknown_coverage_does_not_silently_become_a_complete_pour(self):
        row=facts('A');row['coverage']='made_up'
        with self.assertRaisesRegex(ValueError,'COVERAGE_INVALID'):compile_events(row,self.pair['members'][0])

    def test_phase_budgets_are_separate_and_cannot_borrow_from_rewet(self):
        clips=self.fixture.clips()
        self.assertEqual(len(fit_group(clips,4_000_000)),2)
        with self.assertRaisesRegex(ValueError,'PHASE_DURATION_CONFLICT:part=0'):
            fit_group(clips,4_000_000,phase_boundary_us=500_000)

    def test_strict_phase_plan_starts_press_at_rewet_without_changing_fixed_inputs(self):
        facts_={'duration_us':10_000_000,'width':108,'height':192,'fps_num':60,'fps_den':1}
        roles=deepcopy(self.base.roles);motion=deepcopy(self.fixture.motion)
        b=motion['observations'][1];b['effective_window_frame_ids']=[0,35]
        b['segments'][1]['last_frame']=24;b['segments'][2].update(first_frame=25,last_frame=25)
        b['segments'][3].update(first_frame=26,last_frame=28);b['segments'][4].update(first_frame=29,last_frame=35)
        with patch.object(app,'probe_media',return_value=facts_):
            result=app.apply(self.base.manifest,self.base.timeline,('s1','s2'),roles,self.pair,
                group_id='anchored',prior_origins=self.base.origins,motion=motion,align_phases=True)
        first=min(v.target_start_us for v in result.videos if v.asset_id=='anchored-1')
        self.assertEqual(first,self.base.timeline.sentences[1].start_us)
        self.assertEqual((result.sentences,result.audios,result.subtitles),
            (self.base.timeline.sentences,self.base.timeline.audios,self.base.timeline.subtitles))

    def test_non_task_input_stops_event_review_before_any_media_or_paid_call(self):
        with patch.object(reviewer,'window_video') as encode,patch.object(reviewer.temporal_video,'review') as call:
            with self.assertRaisesRegex(ValueError,'SOURCE_OUTSIDE_TASK'):
                reviewer.review({**self.base.manifest,'material_sources':[]},self.pair,
                    self.base.root/'facts.json',font_path='unused')
        encode.assert_not_called();call.assert_not_called()

    def test_timed_absorption_cannot_be_sped_even_with_complete_pour(self):
        rows=[compile_events(facts('A'),self.pair['members'][0]),{}]
        for stage in ('pour','absorb'):
            for s in rows[0]['segments']:
                if s['kind']!=stage:s['can_speed']=False
            with self.assertRaisesRegex(ValueError,'SPEED_INCOMPATIBLE_CLAIM'):
                validate_focus([SimpleNamespace(sentence_id='s',text='3秒就吸收')],
                    [{'sentence_id':'s','role':'absorption'}],rows)
            rows=[compile_events(facts('A'),self.pair['members'][0]),{}]

    def test_focused_solver_preserves_absorption_and_declares_omitted_pour_prefix(self):
        observations=[compile_events(facts(m['part']),m) for m in self.pair['members']]
        observations[0]['coverage']='absorption_focus'
        plans=fit_phase_windows(self.pair['members'],observations,15,4_300_000,1_800_000)
        selected=plans[0]['source_selection'];self.assertFalse(selected['internal_omissions'])
        self.assertGreater(selected['selected_window_frame_ids'][0],0)
        self.assertEqual(plans[0]['source_end_us'],4_016_667)
        self.assertTrue(all(p['speed']<=2 for p in plans[0]['retiming']['parts']))
        self.assertEqual(sum(p['target_duration_us'] for p in plans),4_300_000)
        self.assertEqual(observations[0]['effective_window_frame_ids'],[0,40])

    def test_focused_solver_finds_shorter_reviewed_window_under_task_cap(self):
        observations=[compile_events(facts(m['part']),m) for m in self.pair['members']]
        observations[0]['coverage']='absorption_focus'
        plans=fit_phase_windows(self.pair['members'],observations,15,5_000_000,1_800_000,max_speed=1.5)
        self.assertEqual(plans[0]['source_selection']['selected_window_frame_ids'],[17,40])
        self.assertTrue(all(p['speed'] <= 1.5 for plan in plans for p in plan['retiming']['parts']))
        self.assertTrue(all(p['speed'] == 1 for plan in plans for p in plan['retiming']['parts'] if p['locked']))

    def test_preparation_is_removed_before_anchoring_actual_tissue_contact(self):
        raw=facts('B');raw['contact_first_frame']=5;raw['events'][0]['first_frame']=5
        raw['events'].insert(0,{'kind':'context','first_frame':0,'last_frame':4,'observed':'pad only; tissue has not contacted'})
        observations=[compile_events(facts('A'),self.pair['members'][0]),compile_events(raw,self.pair['members'][1])]
        plans=fit_phase_windows(self.pair['members'],observations,15,4_800_000,2_300_000)
        self.assertEqual(plans[1]['source_start_us'],500_000)
        self.assertEqual(plans[1]['source_selection']['omitted_preparation_frame_ids'],[0,4])
        raw['contact_first_frame']=0
        with self.assertRaisesRegex(ValueError,'MARKER_REQUIRED'):compile_events(raw,self.pair['members'][1])

    def test_cli_rejects_detail_output_collision_with_manifest_before_request(self):
        from tools.weipin_experiment import main
        import json
        root=self.base.root;manifest=root/'facts-A-detail.json';manifest.write_text(json.dumps(self.base.manifest),encoding='utf-8')
        before=manifest.read_bytes()
        with patch.object(reviewer,'review') as call:
            with self.assertRaisesRegex(ValueError,'COLLIDES'):
                main(['events','--manifest',str(manifest),'--evidence',str(root/'pair.json'),
                    '--output',str(root/'facts.json'),'--font',str(root/'font.ttf')])
        self.assertEqual(manifest.read_bytes(),before);call.assert_not_called()


if __name__=='__main__':unittest.main()
