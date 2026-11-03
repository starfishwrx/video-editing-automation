from copy import deepcopy
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from orchestrator import ai_editor_contract as identity
from orchestrator.selection import purpose_retrieval as selection
from orchestrator.vision import purpose_inventory as vision


class PurposeRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.source=self.root/'product.mov';self.source.write_bytes(b'original')
        self.shots=[{'shot_id':'opaque-id','video':str(self.source),'source_start':0,'source_end':5}]
        self.manifest={'task_id':'fixture','product':'fixture','material_sources':[str(self.source)]}
        self.index={'task_id':'fixture','context':{k:self.manifest.get(k) for k in
            ('task_id','product','category','full_script','voice')},'sources':[str(self.source)],'scans':{
            'opaque-id':{'description':'从背面照光，正面看到透光变化；没有显示温度测量。',
                'source_identity':identity.source_identity(self.source),'hints':[]}}}
        self.request={'index':1,'text':'不闷热不粘腻','checks':[{'id':'c1','claim_text':'不闷热'},
            {'id':'c2','claim_text':'不粘腻'}],'require_action':False,'require_result':False,'segment_us':2_000_000}
        self.candidate={'shot_id':'opaque-id','claim_id':'c1','claim_text':'不闷热',
            'mode':'feature_presentation','description_excerpt':'从背面照光，正面看到透光变化',
            'information_link':'薄透结构的相关表达，未测量温度','start_us':1_000_000,'end_us':3_000_000}
        self.data={'sentences':[{'index':1,'candidates':[self.candidate]}]}

    def validate(self):
        return selection.validate(self.data,[self.request],selection.catalog(self.shots,self.index['scans']))

    def test_one_of_two_original_claims_can_be_recalled_without_claiming_certification(self):
        result=self.validate()
        self.assertEqual(result[1][0]['claim_id'],'c1')
        self.assertNotIn('plan',result[1][0])

    def test_window_is_selected_from_information_location_not_shot_start(self):
        windows=selection.windows(self.validate()[1],self.shots)
        self.assertEqual((windows[0]['source_start'],windows[0]['source_end']),(1,3))

    def test_changed_claim_unknown_id_and_invented_description_are_blocked(self):
        for key,value in [('claim_text','柔软'),('shot_id','invented'),('description_excerpt','实际高温不闷热')]:
            with self.subTest(key=key):
                old=self.candidate[key];self.candidate[key]=value
                with self.assertRaises(ValueError):self.validate()
                self.candidate[key]=old

    def test_duplicate_shortlist_and_missing_sentence_cannot_slip_through(self):
        self.data['sentences'][0]['candidates']*=2
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):self.validate()
        self.data['sentences']=[]
        with self.assertRaisesRegex(ValueError,'COVERAGE'):self.validate()

    def test_source_bounds_and_fixed_display_duration_are_enforced(self):
        self.candidate['end_us']=2_500_000
        self.assertEqual(self.validate()[1][0]['end_us'],3_000_000)
        self.candidate['start_us']=4_000_000
        with self.assertRaisesRegex(ValueError,'OUT_OF_RANGE'):self.validate()

    def test_empty_recall_is_valid_and_does_not_fill_from_generic_pool(self):
        self.data['sentences'][0]['candidates']=[]
        self.assertEqual(self.validate(),{1:[]})

    def test_one_invalid_hint_is_dropped_without_losing_valid_candidates(self):
        bad={**self.candidate,'shot_id':'invented'}
        self.data['sentences'][0]['candidates'].append(bad)
        rejected=[]
        result=selection.validate(self.data,[self.request],selection.catalog(self.shots,self.index['scans']),rejected=rejected)
        self.assertEqual(result[1],[self.candidate])
        self.assertEqual(rejected[0]['candidate'],bad)

    def test_many_introductions_reserve_different_candidates_without_displacing_feature(self):
        entries=[{'shot_id':f'id-{i}','description':f'同商品展示{i}','start_us':0,'end_us':5_000_000}for i in range(24)]
        generic=[{**self.request,'index':i,'display_goal':'general_product'}for i in range(1,8)]
        feature={**self.request,'index':8,'display_goal':'visible_feature'}
        rankings={r['index']:[{**self.candidate,'shot_id':e['shot_id']}for e in entries[:6]]for r in [*generic,feature]}
        result=selection.allocate([*generic,feature],rankings,entries)
        first_waves=[c['shot_id']for cs in result.values()for c in cs[:3]]
        self.assertEqual(len(first_waves),len(set(first_waves)))
        self.assertEqual(result[8],rankings[8])
        self.assertTrue(all(len(cs)<=6 for cs in result.values()))

    def test_empty_feature_recall_cannot_be_refilled_with_generic_reservoir(self):
        request={**self.request,'display_goal':'visible_feature'}
        entries=selection.catalog(self.shots,self.index['scans'])
        self.assertEqual(selection.allocate([request],{1:[]},entries),{1:[]})

    def test_catalog_order_is_independent_of_input_file_order_and_excludes_failed_scans(self):
        other={**self.shots[0],'shot_id':'other'}
        scans={**self.index['scans'],'other':{'description':'普通抚摸产品'}}
        self.assertEqual(selection.catalog([other,*self.shots],scans),selection.catalog([*self.shots,other],scans))
        scans['other']['error']='missing observation'
        self.assertEqual(len(selection.catalog([other,*self.shots],scans)),1)

    def test_modified_source_or_different_task_stops_before_paid_query(self):
        for kind in ('source','task'):
            with self.subTest(kind=kind),patch.object(vision.ai_editor,'_call') as call:
                index=deepcopy(self.index)
                if kind=='source':index['scans']['opaque-id']['source_identity']={}
                else:index['task_id']='other-task'
                with self.assertRaises(ValueError):vision.recall(self.manifest,[self.request],self.shots,index,self.root/'recall')
                call.assert_not_called()

    def test_call_uses_current_target_and_returns_hints_without_eligibility(self):
        with patch.object(vision.ai_editor,'_call',return_value=self.data) as call:
            result=vision.recall(self.manifest,[self.request],self.shots,self.index,self.root/'recall')
        self.assertEqual(result,self.validate())
        self.assertIn('不闷热不粘腻',call.call_args.args[0])
        self.assertNotIn('selection_eligible',result[1][0])


if __name__=='__main__':unittest.main()
