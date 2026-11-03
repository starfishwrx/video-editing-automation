from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import test_weipin_experiment as fixture
from orchestrator import ai_editor_contract as identity
from orchestrator.application import display_optimization as app
from orchestrator.selection import purpose_selection
from orchestrator.selection.shot_usability import assess,intersect_observations
from orchestrator.vision import display_compare


def quality(end=2_000_000):
    return {'view_kind':'stable_display','subject_visibility':'clear','relation':'visible_feature',
        'demonstration_complete':False,'information_point':'thin edge is clear',
        'clear_intervals_us':[{'start':0,'end':end,'observed':'unobscured thin edge'}],'limitations':'sampled'}


def observation(end=2_000_000):
    from test_shot_value import value
    return {'verdict':'matched','same_product':True,'supports_requirement':True,'confidence':.95,
        'selling_point_value':{'observation':value(end=end)},
        'observation':'thin edge clearly presented','reason':'visible feature','time_sensitive':False,
        'window_us':{'start':0,'end':end},'evidence_intervals_us':[{'start':0,'end':end}],
        'phases':[{'start':0,'end':end,'kind':'display','can_speed':False,'reason':'clear view'}],
        'reviewed_window_us':{'start':0,'end':end},'supported_ids':['c1'],'shot_usability':quality(end)}


def request(end=2_000_000):
    return {'index':1,'text':'thin','segment_us':end,'display_goal':'visible_feature',
        'require_action':False,'require_result':False,'checks':[{'id':'c1','type':'display','question':'thin edge'}]}


class ShotUsabilityTests(unittest.TestCase):
    def test_high_confidence_handling_is_not_a_usable_display(self):
        row=observation();row['confidence']=.99;row['shot_usability'].update(
            view_kind='handling_or_setup',relation='inferred_only',subject_visibility='partial')
        result=purpose_selection.fit(row,{'source_start':0,'source_end':2},request())
        self.assertFalse(result['ok']);self.assertIn('HANDLING_WITHOUT_INFORMATION',result['usability']['reasons'])

    def test_brief_clear_pose_cannot_hide_long_obscured_manipulation(self):
        q=quality();q['clear_intervals_us'][0]['end']=700_000
        self.assertEqual(assess(q,request(),{'start':0,'end':2_000_000})['status'],'REJECTED')

    def test_overlapping_intervals_do_not_double_count_clear_time(self):
        q=quality();q['clear_intervals_us']=[{'start':0,'end':600_000,'observed':'pose'}]*3
        result=assess(q,request(),{'start':0,'end':2_000_000})
        self.assertEqual(result['clear_fraction'],.3);self.assertEqual(result['status'],'REJECTED')

    def test_general_product_view_is_allowed_for_intro_but_not_a_specific_feature(self):
        q=quality();q['relation']='general_product'
        self.assertEqual(assess(q,{**request(),'display_goal':'general_product'},{'start':0,'end':2_000_000})['status'],'USABLE_FOR_COMPARISON')
        self.assertEqual(assess(q,request(),{'start':0,'end':2_000_000})['status'],'REJECTED')

    def test_incomplete_demo_and_outside_intervals_are_rejected(self):
        q=quality();q['view_kind']='complete_demonstration'
        self.assertIn('DEMONSTRATION_INCOMPLETE',assess(q,request(),{'start':0,'end':2_000_000})['reasons'])
        q['clear_intervals_us'][0]['end']=3_000_000
        with self.assertRaisesRegex(ValueError,'OUTSIDE_WINDOW'):assess(q,request(),{'start':0,'end':2_000_000})

    def test_padding_a_short_display_requires_new_actual_window_review(self):
        row=observation(1_000_000);row['reviewed_window_us']['end']=3_000_000
        result=purpose_selection.fit(row,{'source_start':0,'source_end':3},request())
        self.assertEqual(result['reason'],'DISPLAY_CHANGED_WINDOW_REQUIRES_REVIEW')

    def test_general_readability_cannot_expand_brief_specific_feature_evidence(self):
        blind=quality();target=quality();target['clear_intervals_us'][0]['end']=300_000
        merged=intersect_observations(blind,target,request(),{'start':0,'end':2_000_000})
        result=assess(merged,request(),{'start':0,'end':2_000_000})
        self.assertEqual(result['status'],'REJECTED');self.assertEqual(result['clear_fraction'],.15)
        self.assertEqual(merged['blind_observation'],blind);self.assertEqual(merged['semantic_observation'],target)

    def test_clear_information_precedes_model_confidence_in_ranking(self):
        a={'source':'a','observation':observation(),'plan':{'ok':True,'source_start_us':0,'source_end_us':2_000_000,
            'tier':'ORIGINAL','maximum_speed':1,'usability':{'clear_fraction':.5,'subject_visibility':'partial','longest_clear_us':1_000_000}}}
        b=deepcopy(a);b['source']='b';b['observation']['confidence']=.7;b['plan']['usability'].update(clear_fraction=1,subject_visibility='clear')
        self.assertIs(purpose_selection.choose([a,b],[]),b)

    def test_actual_picture_comparison_can_reject_all_without_promoting_a_fallback(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);cs=[]
            for i in range(2):
                source=root/f'{i}.mp4';source.write_bytes(b'source');sheet=root/f'{i}.jpg';Image.new('RGB',(100,200)).save(sheet)
                cs.append({'source':str(source),'observation':{**observation(),'source_identity':identity.source_identity(source),'sheet':str(sheet)},
                    'plan':{'ok':True,'source_start_us':0,'source_end_us':2_000_000,'tier':'ORIGINAL','maximum_speed':1}})
            row={'candidates':[{'id':f'C{i}','usable':False,'rank':None,'reason':'setup only'} for i in range(2)]}
            with patch.object(display_compare.ai_editor,'_call',return_value=row),\
                    patch.object(display_compare.ai_editor,'_review_media',side_effect=[{'sheet':c['observation']['sheet']} for c in cs]):
                display_compare.compare({'product':'fixture'},request(),cs,root/'compare')
            self.assertIsNone(purpose_selection.choose(cs,[]))


class DisplayOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.base=fixture.BoundExperimentTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
        b=self.base;self.request={**request(),'text':b.timeline.sentences[0].text}
        self.shots=[{'shot_id':'new','video':str(b.files['raw-new']),'source_start':0,'source_end':2}]
        self.output=b.root/'new-preview';self.facts={'duration_us':10_000_000,'width':108,'height':192,'fps_num':60,'fps_den':1}

    def run_case(self,change_source=False):
        row=observation();row['source_identity']=identity.source_identity(self.shots[0]['video'])
        row['selling_point_value']['observation']['claims'][0]['claim_text']=self.request['text']
        def review(*args,**kwargs):
            if change_source:Path(self.shots[0]['video']).write_bytes(b'replaced after visual review')
            return row
        def encode(ffmpeg,args,path,**kwargs):Path(path).write_bytes(b'normalized own source')
        with patch.object(app.ai_editor,'_review_media',return_value={}),patch.object(app.vision,'review',side_effect=review),\
                patch.object(app,'run_h264_encode',side_effect=encode),patch.object(app,'probe_media',return_value=self.facts):
            return app.refine(self.base.manifest,self.base.timeline,'s1',self.request,self.shots,self.base.origins,self.output,render=False)

    def test_real_assembly_keeps_audio_captions_and_every_other_sentence(self):
        result=self.run_case();self.assertTrue(all(result['preserved'].values()))
        self.assertEqual(result['status'],'PLANNED_OUTPUT_REVIEW_PENDING')
        self.assertFalse(result['formal_delivery_allowed'])

    def test_unknown_source_is_rejected_before_any_paid_request(self):
        self.shots[0]['video']=str(self.base.files['proxy-new'])
        with patch.object(app.vision,'review') as call:
            with self.assertRaisesRegex(ValueError,'CANDIDATE_SCOPE'):self.run_case()
        call.assert_not_called()

    def test_existing_output_cannot_be_overwritten(self):
        self.run_case();before=(self.output/'timeline.json').read_bytes()
        with self.assertRaises(FileExistsError):self.run_case()
        self.assertEqual((self.output/'timeline.json').read_bytes(),before)

    def test_source_replaced_after_review_cannot_use_old_observation(self):
        with self.assertRaisesRegex(ValueError,'REVIEW_SOURCE_CHANGED'):self.run_case(change_source=True)
        self.assertEqual(json.loads((self.output/'result.json').read_text('utf-8'))['status'],'BLOCKED')

    def test_readable_information_intervals_stay_realtime_when_other_process_accelerates(self):
        row=observation(6_000_000);row['phases'][0]['can_speed']=True
        row['shot_usability']['clear_intervals_us']=[{'start':i*1_000_000,'end':i*1_000_000+700_000,'observed':'clear shape'} for i in range(5)]
        row['selling_point_value']['observation']['claims'][0]['evidence_intervals_us']=[
            {'start':s['start'],'end':s['end']} for s in row['shot_usability']['clear_intervals_us']]
        result=purpose_selection.fit(row,{'source_start':0,'source_end':6},request(4_500_000))
        self.assertTrue(result['ok']);self.assertGreater(result['maximum_speed'],1)
        self.assertTrue(all(p['speed']==1 for p in result['parts'] if p['locked']))


if __name__=='__main__':unittest.main()
