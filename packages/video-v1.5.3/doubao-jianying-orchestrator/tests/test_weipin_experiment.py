from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from orchestrator import ai_editor_contract as identity
from orchestrator.selection.experiment_contract import contract_for,validate_observation,validate_group,candidate_groups
from orchestrator.selection.temporal_groups import replace_group
from orchestrator.media.temporal_samples import validate as validate_samples,guard_outputs
from orchestrator.vision import experiment_review,purpose_review
from orchestrator.application import experiment_production as app,purpose_production
from test_temporal_video_groups import timeline,clip

PHASES=['pour','absorb','press','lift','result']


def observation():
    return {'verdict':'complete','same_product':True,'continuous_specimen':True,'no_splice':True,
        'observed':'one real continuous pour, absorption, tissue press, lift and readable result',
        'phases':[{'kind':kind,'first_frame':a,'last_frame':b,'observed':'visible '+kind}
            for kind,a,b in [('pour',10,18),('absorb',15,22),('press',23,26),('lift',27,28),('result',29,35)]]}


def frames():return [{'frame_id':i,'source_frame':i*6,'pts_us':i*100000}for i in range(100)]


class ExperimentContractTests(unittest.TestCase):
    def test_weipin_policy_loads_existing_business_rules(self):
        text=purpose_review.policy({'category':'weipin'})
        self.assertIn('允许使用对应同一次实验的多个素材文件',text)
        self.assertIn('不闷热句不并入吸水实验组',text)
        self.assertIn('倒液不能替代透气',text)
        self.assertNotIn('absorb_rewet',purpose_review.policy({'category':'zhipin'}))

    def test_complete_chain_is_legal_but_partial_is_not_complete(self):
        self.assertEqual(validate_observation(observation(),frames(),PHASES)['verdict'],'complete')
        row=observation();row['phases']=row['phases'][2:]
        with self.assertRaisesRegex(ValueError,'COMPLETE_WITHOUT'):validate_observation(row,frames(),PHASES)
        row['verdict']='incomplete'
        self.assertEqual(validate_observation(row,frames(),PHASES)['verdict'],'incomplete')

    def test_same_product_does_not_mean_same_specimen_or_unspliced_experiment(self):
        for key in ('same_product','continuous_specimen','no_splice'):
            row=observation();row[key]=False
            with self.assertRaisesRegex(ValueError,'COMPLETE_WITHOUT'):validate_observation(row,frames(),PHASES)

    def test_result_before_lift_and_absorption_after_press_are_rejected(self):
        for index,key,value in [(3,'first_frame',0),(1,'last_frame',99),(4,'first_frame',25)]:
            row=observation();row['phases'][index][key]=value
            with self.assertRaisesRegex(ValueError,'ORDER'):validate_observation(row,frames(),PHASES)

    def test_unknown_duplicate_bool_or_outside_frame_cannot_be_evidence(self):
        for value in (-1,100,True):
            row=observation();row['phases'][0]['first_frame']=value
            with self.assertRaisesRegex(ValueError,'FRAME_ID'):validate_observation(row,frames(),PHASES)
        row=observation();row['phases'].append(deepcopy(row['phases'][0]))
        with self.assertRaisesRegex(ValueError,'PHASE_INVALID'):validate_observation(row,frames(),PHASES)

    def test_absorption_group_cannot_borrow_breathability_sentence(self):
        t=timeline();rules=contract_for({'category':'weipin'})
        roles=[{'sentence_id':s.sentence_id,'text':s.text,'role':r}for s,r in zip(t.sentences,['breathability','absorption','rewet'])]
        self.assertEqual(candidate_groups(t,roles,rules),[('s2','s3')])
        with self.assertRaisesRegex(ValueError,'TOPIC_CONFLICT'):validate_group(t,('s1','s2','s3'),roles,rules)

    def test_role_cache_cannot_drop_or_relabel_sentence_identity(self):
        t=timeline();rules=contract_for({'category':'weipin'})
        roles=[{'sentence_id':s.sentence_id,'text':s.text,'role':'absorption'}for s in t.sentences]
        roles[0]['text']='changed'
        with self.assertRaisesRegex(ValueError,'ROLE_IDENTITY'):candidate_groups(t,roles,rules)

    def test_two_originals_and_gaps_cannot_form_continuous_experiment(self):
        t=timeline();a=replace(t.assets[0],asset_id='other',path='other-original.mp4')
        first=clip();first.update(source_end_us=8_000_000,target_duration_us=2_000_000,protected_intervals=[])
        second=deepcopy(first);second['asset_id']='other'
        with self.assertRaisesRegex(ValueError,'PAIR_EVIDENCE_REQUIRED'):
            replace_group(t,('s1','s2'),[first,second],group_id='g',assets=(a,))
        second=deepcopy(first);second.update(source_start_us=9_000_000,source_end_us=11_000_000)
        with self.assertRaisesRegex(ValueError,'SOURCE_GAP'):
            replace_group(t,('s1','s2'),[first,second],group_id='g')
        second.update(source_start_us=8_000_000,source_end_us=10_000_000)
        self.assertEqual(replace_group(t,('s1','s2'),[first,second],group_id='g').audios,t.audios)


class BoundExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        files={n:self.root/(n+'.mp4')for n in ('raw-old','proxy-old','raw-new','proxy-new','numbered')}
        for p in files.values():p.write_bytes(b'fixture-'+p.name.encode())
        self.files=files
        self.timeline=timeline();self.timeline=replace(self.timeline,assets=(replace(self.timeline.assets[0],path=str(files['proxy-old'])),self.timeline.assets[1]))
        self.manifest={'task_id':self.timeline.task_id,'category':'weipin','product':'fixture',
            'segments':[{'text':s.text}for s in self.timeline.sentences],
            'material_sources':[str(files['raw-old']),str(files['raw-new'])]}
        self.samples={'schema':'numbered-temporal-samples-v2-owned-normalization',
            'original':identity.source_identity(files['raw-new']),'source':identity.source_identity(files['proxy-new']),
            'video':identity.source_identity(files['numbered']),'frames':frames(),'source_end_us':10_000_000}
        self.save_samples()
        self.evidence={'protocol':experiment_review.PROTOCOL,'task_id':self.timeline.task_id,
            'rules':identity.rules_identity(self.manifest),'original':self.samples['original'],
            'context':{'product':self.manifest['product'],'category':'weipin'},
            'samples':self.samples,'observation':observation()}
        self.roles=[{'sentence_id':s.sentence_id,'text':s.text,'role':r}for s,r in zip(self.timeline.sentences,['absorption','rewet','promotion'])]
        self.origins=[{'asset_id':'v','original':identity.source_identity(files['raw-old']),
            'proxy':identity.source_identity(files['proxy-old']),'clock':'same_source_clock'}]

    def save_samples(self):
        (self.root/'samples.json').write_text(json.dumps(self.samples),encoding='utf-8')

    def apply(self,**kw):
        facts={'duration_us':10_000_000,'width':108,'height':192,'fps_num':60,'fps_den':1}
        with patch.object(app,'probe_media',return_value=facts):
            return app.apply(self.manifest,self.timeline,('s1','s2'),self.roles,self.evidence,
                             group_id='g',prior_origins=kw.pop('prior_origins',self.origins),**kw)

    def test_complete_bound_experiment_preserves_fixed_audio_sentences_and_pending_status(self):
        new=self.apply()
        self.assertEqual((new.audios,new.subtitles,new.sentences),(self.timeline.audios,self.timeline.subtitles,self.timeline.sentences))
        self.assertTrue(new.unresolved);self.assertEqual(new.videos[0].speed_num,new.videos[0].speed_den)
        self.assertEqual(new.videos[0].protected_intervals[-1].min_output_frames,15)

    def test_original_a_cannot_be_attached_to_proxy_from_b(self):
        self.evidence['original']=identity.source_identity(self.files['raw-old'])
        with self.assertRaisesRegex(ValueError,'ORIGINAL_PROXY_MISMATCH'):self.apply()

    def test_missing_prior_origin_is_not_assumed_unrelated(self):
        with self.assertRaisesRegex(ValueError,'ORIGIN_UNKNOWN'):self.apply(prior_origins=[])

    def test_other_proxy_cannot_replay_original_used_outside_group(self):
        self.samples['original']=identity.source_identity(self.files['raw-old']);self.save_samples()
        self.evidence['original']=self.samples['original']
        for p in self.evidence['observation']['phases']:
            p['first_frame']+=55;p['last_frame']+=55
        with self.assertRaisesRegex(ValueError,'SOURCE_REPLAY'):self.apply()

    def test_partial_chain_never_reaches_media_probe(self):
        self.evidence['observation']['verdict']='incomplete';self.evidence['observation']['phases']=[]
        with patch.object(app,'probe_media')as probe:
            with self.assertRaisesRegex(ValueError,'INCOMPLETE_NEEDS_MATCHING'):
                app.apply(self.manifest,self.timeline,('s1','s2'),self.roles,self.evidence,
                          group_id='g',prior_origins=self.origins)
            probe.assert_not_called()

    def test_old_rules_and_changed_script_cannot_reuse_evidence(self):
        old=self.evidence['rules'];self.evidence['rules']={}
        with self.assertRaisesRegex(ValueError,'RULE_OR_TASK_CHANGED'):self.apply()
        self.evidence['rules']=old;self.manifest['segments'][0]['text']='different'
        with self.assertRaisesRegex(ValueError,'SCRIPT_CHANGED'):self.apply()

    def test_changed_product_cannot_reuse_same_product_observation(self):
        self.manifest['product']='another SKU'
        with self.assertRaisesRegex(ValueError,'RULE_OR_TASK_CHANGED'):self.apply()

    def test_too_short_result_and_too_short_sentence_budget_are_rejected(self):
        row=self.evidence['observation'];row['phases'][-1]['last_frame']=row['phases'][-1]['first_frame']
        with self.assertRaisesRegex(ValueError,'RESULT_WINDOW_TOO_SHORT'):self.apply()
        self.evidence['observation']=observation()
        for p in self.evidence['observation']['phases']:p['last_frame']+=45 if p['kind']=='result'else 0
        with self.assertRaisesRegex(ValueError,'DURATION_CONFLICT'):self.apply()

    def test_changed_frame_map_is_not_accepted_as_owned_provenance(self):
        self.samples['frames'][0]['pts_us']=123
        with self.assertRaisesRegex(ValueError,'PROVENANCE_CHANGED'):validate_samples(self.samples)

    def test_output_cannot_overwrite_original_proxy_sample_or_metadata(self):
        for p in list(self.files.values())+[self.root/'samples.json']:
            if p in (self.files['raw-old'],self.files['proxy-old']):continue
            before=p.read_bytes()
            with patch.object(experiment_review.temporal_video,'review')as call:
                with self.assertRaisesRegex(ValueError,'COLLIDES'):
                    experiment_review.observe(self.manifest,self.files['raw-new'],self.samples,p)
                call.assert_not_called()
            self.assertEqual(p.read_bytes(),before)

    def test_old_unbound_sample_schema_is_rejected(self):
        self.samples['schema']='numbered-temporal-samples-v1'
        with self.assertRaisesRegex(ValueError,'UNBOUND_OLD_MEDIA'):validate_samples(self.samples)


class LegacyPurposeGateTests(unittest.TestCase):
    def test_old_index_is_retrieval_only_and_requirements_are_recompiled(self):
        manifest={'task_id':'fixture','category':'weipin','product':'fixture','material_sources':[]}
        narration={'rows':[{'index':1,'text':'吸收','audio_duration_us':1_000_000}]}
        request={'index':1,'text':'吸收','topic':'absorption','require_action':True,'require_result':True,
                 'checks':[{'id':'c1','type':'action_result','question':'complete experiment'}]}
        index={'requests':[{'index':1,'text':'吸收','checks':[]}],'scans':{}}
        with tempfile.TemporaryDirectory()as d,patch.object(purpose_production,'_binding'), \
             patch.object(purpose_production,'_audio_binding',return_value=[]), \
             patch.object(purpose_review,'requirements',return_value=[request])as compile_, \
             patch.object(purpose_production.purpose_inventory,'recall',return_value={1:[]})as recall:
            result=purpose_production.select(manifest,{'shots':[]},narration,index,Path(d))
        compile_.assert_called_once()
        self.assertEqual(recall.call_args.args[1][0]['checks'],request['checks'])
        self.assertEqual(result['requests'][0]['topic'],'absorption')
        self.assertIsNone(result['rows'][0]['selected'])
        self.assertEqual(result['rows'][0]['reason'],'SAME_EXPERIMENT_GROUP_REQUIRED')

    def test_old_weipin_selection_rule_identity_is_rejected(self):
        with patch.object(purpose_production,'_audio_binding',return_value=[]), \
             patch('orchestrator.selection.timing_contract.split_sentences',return_value=[]):
            m={'task_id':'fixture','category':'weipin','voice':'fixture','material_sources':[]}
            a={'task_id':'fixture','sources':[],'context':purpose_production._context(m),'narration':[],'requests':[]}
            with self.assertRaisesRegex(ValueError,'RULE_IDENTITY_CHANGED'):
                purpose_production._binding(m,{'rows':[],'voice':'fixture'},a,rules_required=True)

    def test_selected_absorption_cannot_use_old_per_sentence_materializer(self):
        for topic,error in [('absorption','REQUIRES_GROUP_ASSEMBLY'),('rewet','REQUIRES_GROUP_ASSEMBLY'),('typo','TOPIC_REQUIRED')]:
            selection={'requests':[{'index':1,'text':'吸收','topic':topic}],
                       'rows':[{'index':1,'text':'吸收','selected':{'fake':'only tissue press'}}]}
            with patch.object(purpose_production,'_binding'),patch.object(purpose_production,'probe_media')as probe:
                with self.assertRaisesRegex(ValueError,error):
                    purpose_production.materialize({'category':'weipin'},{'rows':[{'text':'吸收'}]},selection,Path('unused'))
                probe.assert_not_called()


if __name__=='__main__':unittest.main()
