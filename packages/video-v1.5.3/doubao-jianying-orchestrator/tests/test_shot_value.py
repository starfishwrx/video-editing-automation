from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from orchestrator.selection.shot_value import assess
from orchestrator.selection import purpose_selection


def value(text='thin',end=2_000_000):
    return {'all_spoken_claims_checked':True,'uncovered_spoken_claims':[],
        'claims':[{'check_id':'c1','claim_text':text,'role':'feature_presentation','coverage':'full',
        'adds_claim_information':True,'specific_link':True,'action_or_presentation':'stationary side profile',
        'visible_change_or_feature':'the product edge thickness is shown','claim_link':'side profile conveys thinness',
        'missing_information':'no quantitative thickness measurement',
        'evidence_intervals_us':[{'start':0,'end':end}]}],'limitations':'sampled view'}


class ShotValueTests(unittest.TestCase):
    def setUp(self):
        self.request={'text':'thin','display_goal':'visible_feature','checks':[{'id':'c1','type':'display'}]}
        self.bounds={'start':0,'end':2_000_000}

    def test_a_stationary_feature_can_have_value_without_motion(self):
        self.assertEqual(assess(value(),self.request,self.bounds)['status'],'FULL')

    def test_busy_hand_motion_without_claim_information_is_rejected(self):
        data=value();data['claims'][0].update(role='generic_motion',adds_claim_information=False,specific_link=False)
        self.assertEqual(assess(data,self.request,self.bounds)['status'],'NO_SELLING_POINT_INFORMATION')

    def test_softness_cannot_be_substituted_for_the_original_hot_sticky_claim(self):
        with self.assertRaisesRegex(ValueError,'CLAIM_SUBSTITUTED'):
            assess(value('softness'),{**self.request,'text':'not hot or sticky'},self.bounds)

    def test_compound_clause_partial_support_stays_partial(self):
        request={**self.request,'text':'not hot, not sticky','checks':[{'id':'c1','claim_text':'not hot'},{'id':'c2','claim_text':'not sticky'}]}
        data=value('not hot');missing=deepcopy(data['claims'][0]);missing.update(check_id='c2',claim_text='not sticky',
            role='unrelated',coverage='none',adds_claim_information=False,specific_link=False,evidence_intervals_us=[])
        data['claims'].append(missing)
        result=assess(data,request,self.bounds)
        self.assertEqual(result['status'],'PARTIAL');self.assertEqual(result['uncovered_ids'],['c2'])

    def test_explicit_request_cannot_replace_hot_sticky_with_softness(self):
        request={**self.request,'text':'穿着不闷热，也不粘腻','checks':[{'id':'c1','claim_text':'柔软'}]}
        with self.assertRaisesRegex(ValueError,'REQUEST_CLAIM_SUBSTITUTED'):
            assess(value('柔软'),request,self.bounds)

    def test_unlisted_original_claim_keeps_whole_clause_partial(self):
        request={**self.request,'text':'不闷热不粘腻','checks':[{'id':'c1','claim_text':'不闷热'}]}
        data=value('不闷热');data['uncovered_spoken_claims']=['不粘腻']
        self.assertEqual(assess(data,request,self.bounds)['status'],'PARTIAL')
        del data['all_spoken_claims_checked']
        self.assertTrue(assess(data,request,self.bounds)['selection_eligible'])

    def test_one_relevant_point_is_enough_without_covering_other_points(self):
        request={**self.request,'text':'不闷热不粘腻','checks':[{'id':'c1','claim_text':'不闷热'},{'id':'c2','claim_text':'不粘腻'}]}
        data=value('不闷热');data['claims'][0]['coverage']='partial'
        result=assess(data,request,self.bounds)
        self.assertTrue(result['selection_eligible']);self.assertEqual(result['supported_ids'],['c1'])
        self.assertIn('c2',result['uncovered_ids'])

    def test_one_direct_demonstration_ranks_before_more_indirect_points(self):
        request={**self.request,'text':'thin and soft','checks':[{'id':'c1','claim_text':'thin'},{'id':'c2','claim_text':'soft'}]}
        direct=value('thin');direct['claims'][0]['role']='direct_demonstration'
        indirect=value('thin');second=deepcopy(indirect['claims'][0]);second.update(check_id='c2',claim_text='soft');indirect['claims'].append(second)
        candidates=[]
        for name,data in [('direct',direct),('indirect',indirect)]:
            assessed=assess(data,request,self.bounds)
            candidates.append({'source':name,'observation':{'supported_ids':assessed['supported_ids'],'confidence':.95},
                'plan':{'ok':True,'source_start_us':0,'source_end_us':2_000_000,'selling_point_value':assessed,
                    'tier':'ORIGINAL','maximum_speed':1}})
        self.assertIs(purpose_selection.choose(candidates,[]),candidates[0])

    def test_general_display_is_valid_only_for_general_intro(self):
        data=value();data['claims'][0].update(role='general_product',adds_claim_information=False,specific_link=False)
        self.assertEqual(assess(data,self.request,self.bounds)['status'],'NO_SELLING_POINT_INFORMATION')
        self.assertEqual(assess(data,{**self.request,'display_goal':'general_product'},self.bounds)['status'],'FULL')

    def test_claim_value_evidence_cannot_come_from_outside_output(self):
        data=value();data['claims'][0]['evidence_intervals_us'][0]['end']=3_000_000
        with self.assertRaisesRegex(ValueError,'OUTSIDE_WINDOW'):assess(data,self.request,self.bounds)

    def test_clear_high_confidence_display_does_not_override_empty_action_value(self):
        from test_shot_usability import observation,request
        data=observation();info=value();info['claims'][0].update(role='generic_motion',adds_claim_information=False,specific_link=False)
        data['selling_point_value']={'observation':info}
        data['confidence']=.99
        result=purpose_selection.fit(data,{'source_start':0,'source_end':2},request())
        self.assertEqual(result['reason'],'NO_SELLING_POINT_INFORMATION')
