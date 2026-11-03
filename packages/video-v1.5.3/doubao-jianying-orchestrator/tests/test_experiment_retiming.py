from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from fractions import Fraction
from unittest.mock import patch
import json
import unittest

import test_experiment_pairs as fixture
from orchestrator import ai_editor_contract as identity
from orchestrator.application import experiment_production as app
from orchestrator.selection.experiment_retiming import validate_motion,windows,fit_group
from orchestrator.vision import experiment_motion_review as motion
from tools import weipin_experiment as cli
from orchestrator.domain.timeline import VideoEvent,TimelineError
from orchestrator.media.timeline_render import _frame_plan


def observation(part):
    phases=([('pour',0,0,False),('pour',1,36,True),('pour',37,37,False),('absorb',38,40,False)]
        if part=='A'else [('press',0,0,False),('press',1,29,True),('press',30,30,False),('lift',31,33,False),('result',34,40,False)])
    return {'verdict':'observed','effective_window_frame_ids':[0,40],
        'segments':[{'kind':k,'first_frame':a,'last_frame':b,'can_speed':s,'observed':'actual visible phase'}for k,a,b,s in phases],
        'limitations':'sampled boundaries'}


class ExperimentRetimingTests(unittest.TestCase):
    def setUp(self):
        self.pair=fixture.PairApplicationTests();self.pair.setUp();self.addCleanup(self.pair.doCleanups)
        self.base=self.pair.base;self.evidence=self.pair.evidence
        for m in self.evidence['members']:
            m.update(window_frame_ids=[0,40],visible_frame_ids=[0,6,11,17,23,29,34,40])
        # Bind a valid pair observation to this larger physical window.
        for p in self.evidence['observation']['phases']:
            p['first_frame']=min([0,6,11,17,23,29,34,40],key=lambda n:abs(n-p['first_frame']))
            p['last_frame']=min([0,6,11,17,23,29,34,40],key=lambda n:abs(n-p['last_frame']))
        self.evidence['observation']['phases'][-1].update(first_frame=29,last_frame=40)
        self.pair.bind()
        self.motion={'protocol':motion.PROTOCOL,'task_id':self.base.manifest['task_id'],
            'rules':identity.rules_identity(self.base.manifest),
            'pair_input_sha256':identity.digest(motion.input_binding(self.base.manifest,self.evidence['members'],self.pair.sheet)),
            'observations':[observation('A'),observation('B')]}

    def clips(self):return windows(self.evidence['members'],self.motion['observations'],15)

    def test_shared_budget_prefers_two_and_uses_three_only_when_needed(self):
        for target,maximum in [(6_000_000,2),(4_000_000,3)]:
            plans=fit_group(self.clips(),target)
            self.assertEqual(sum(p['target_duration_us']for p in plans),target)
            parts=[r for p in plans for r in p['retiming']['parts']]
            self.assertLessEqual(max(r['speed']for r in parts),maximum)
            self.assertTrue(all(r['speed']==1 for r in parts if r['locked']))
        self.assertGreater(max(r['speed']for p in plans for r in p['retiming']['parts']),2)

    def test_no_source_frame_is_cut_or_repeated_to_fit(self):
        for original,plan in zip(self.clips(),fit_group(self.clips(),4_000_000)):
            parts=plan['retiming']['parts']
            self.assertEqual(parts[0]['source_start_us'],original['source_start_us'])
            self.assertEqual(parts[-1]['source_end_us'],original['source_end_us'])
            self.assertTrue(all(a['source_end_us']==b['source_start_us']for a,b in zip(parts,parts[1:])))

    def test_too_short_target_stops_at_three_instead_of_hiding_required_actions(self):
        with self.assertRaisesRegex(ValueError,'3X_CAP'):fit_group(self.clips(),2_500_000)

    def test_complete_press_and_result_cannot_be_marked_speedable(self):
        b=self.motion['observations'][1];b['segments'][0]['can_speed']=True
        with self.assertRaisesRegex(ValueError,'CONTACT_OR_COMPLETION'):self.clips()
        b['segments'][0]['can_speed']=False;b['segments'][-1]['can_speed']=True
        with self.assertRaisesRegex(ValueError,'MUST_STAY_REALTIME'):self.clips()

    def test_missing_frame_range_or_phase_is_not_a_complete_motion_plan(self):
        self.motion['observations'][0]['segments'][1]['first_frame']=2
        with self.assertRaisesRegex(ValueError,'COVERAGE'):self.clips()

    def test_too_short_result_is_rejected_before_assembly(self):
        b=self.motion['observations'][1];b['segments'][-2]['last_frame']=38;b['segments'][-1]['first_frame']=39
        with self.assertRaisesRegex(ValueError,'RESULT_WINDOW_TOO_SHORT'):self.clips()

    def test_real_apply_keeps_audio_and_sentences_and_speeds_only_unlocked_parts(self):
        facts={'duration_us':10_000_000,'width':108,'height':192,'fps_num':60,'fps_den':1}
        with patch.object(app,'probe_media',return_value=facts):
            new=app.apply(self.base.manifest,self.base.timeline,('s1','s2'),self.base.roles,self.evidence,
                motion=self.motion,group_id='motion',prior_origins=self.base.origins)
        self.assertEqual((new.sentences,new.audios,new.subtitles),(self.base.timeline.sentences,self.base.timeline.audios,self.base.timeline.subtitles))
        moving=[v for v in new.videos if v.event_id.startswith('motion-')]
        self.assertEqual(sum(v.duration_us for v in moving),4_000_000)
        self.assertTrue(any(v.speed_num>2*v.speed_den for v in moving))
        self.assertTrue(all(v.speed_num<=3*v.speed_den for v in moving))
        self.assertTrue(all(v.speed_num==v.speed_den for v in moving if v.protected_intervals))

    def test_motion_cannot_be_reused_on_other_pair_or_window(self):
        self.motion['pair_input_sha256']='old-window'
        with self.assertRaisesRegex(ValueError,'MOTION_INPUT_CHANGED'):
            motion.validate(self.base.manifest,self.evidence,self.motion)

    def test_motion_review_rejects_non_task_source_before_encoding_or_api(self):
        manifest={**self.base.manifest,'material_sources':[]}
        with patch.object(motion,'run_h264_encode')as encode,patch.object(motion.temporal_video,'review')as call:
            with self.assertRaisesRegex(ValueError,'SOURCE_OUTSIDE_TASK'):
                motion.review(manifest,self.evidence,self.base.root/'unsafe.json')
        encode.assert_not_called();call.assert_not_called()

    def test_cli_output_guard_accepts_normal_roles_output(self):
        root=self.base.root;m=root/'task.json';t=root/'timeline.json';o=root/'roles.json'
        m.write_text(json.dumps(self.base.manifest),encoding='utf-8');self.base.timeline.save(t)
        with patch('orchestrator.vision.experiment_review.classify_sentences',return_value=self.base.roles):
            self.assertEqual(cli.main(['roles','--manifest',str(m),'--timeline',str(t),'--output',str(o)]),0)
        self.assertTrue(o.is_file())

    def test_actual_output_frames_cover_process_tail_without_over_three_boundary_skip(self):
        duration=1_394_343;speed=Fraction(4_100_000,duration)
        event=VideoEvent('quantized','v',('s1',),400_000,duration,2_100_000,4_100_000,
            speed.numerator,speed.denominator,evidence=({'bounded_process_speed_cap':3},))
        metadata={'stream':{'avg_frame_rate':'60/1','time_base':'1/60000'},
            'frames':[{'pts_us':round(i*1_000_000/60)}for i in range(600)],'source_end_us':10_000_000}
        mapping=_frame_plan(self.base.timeline,event,metadata)
        self.assertEqual(len(mapping),41)
        source_frames=[r['source_frame']for r in mapping]+[372]
        self.assertLessEqual(max(b-a for a,b in zip(source_frames,source_frames[1:])),6)
        too_short=replace(event,duration_us=1_300_000,speed_num=41,speed_den=13)
        with self.assertRaisesRegex(TimelineError,'EXCEEDS_PROCESS_SPEED_CAP'):
            _frame_plan(self.base.timeline,too_short,metadata)


if __name__=='__main__':unittest.main()
