from copy import deepcopy
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from orchestrator.selection import opening_hook as hook
from orchestrator.selection.shot_value import assess


def request(index=1,**changes):
    return {'index':index,'text':'谁还用擦脸泛红的杂牌啊','has_product_selling_point':False,
        'display_goal':'general_product','require_action':False,'require_result':False,
        'checks':[{'id':'c1','claim_text':'擦脸泛红','question':'same product','type':'display'}],**changes}


def packaging(**changes):
    return {'claims':[],'hook_packaging':{'packaging_visible':True,'packaging_primary':True,'same_product':True,
        'identifiers_observed':'visible brand and pink carton',
        'evidence_intervals_us':[{'start':0,'end':1_000_000}],**changes}}


class OpeningHookTests(unittest.TestCase):
    def test_packaging_recall_priority_is_only_a_multilingual_description_hint(self):
        primary=['手持包装正面展示','粉色洗脸巾盒展示','Front-facing retail packaging', 'A carton presentation',
                 'Several packages presented front-facing','Several cartons presented front-facing',
                 '白色背景前的整提包装正面居中展示','Centered retail packaging against a white background']
        incidental=['揉搓动作，包装只在背景角落','Packaging is in the background']
        negative=['揉搓纸巾，未见包装','Only rubbing, no packaging visible']
        self.assertTrue(all(hook.packaging_recall_priority(s)==0 for s in primary))
        self.assertTrue(all(hook.packaging_recall_priority(s)==1 for s in incidental))
        self.assertTrue(all(hook.packaging_recall_priority(s)==3 for s in negative))
        self.assertEqual(hook.packaging_recall_priority('揉搓后展开演示'),2)

    def test_only_first_three_no_point_clauses_receive_packaging_goal(self):
        rows=hook.normalize([request(i)for i in range(1,5)])
        self.assertTrue(all(hook.is_packaging_hook(r)for r in rows[:3]))
        self.assertNotIn('visual_purpose',rows[3])
        self.assertEqual(rows[0]['text'],request()['text'])
        self.assertEqual(rows[0]['checks'][0]['claim_text'],request()['text'])

    def test_classified_mid_script_hook_uses_product_without_downgrading_claims(self):
        rows = hook.normalize([request(8, is_generic_hook=True), request(9)])
        self.assertTrue(hook.is_packaging_hook(rows[0]))
        self.assertNotIn("visual_purpose", rows[1])
        hook.validate_completed(rows)

    def test_explicit_points_in_opening_are_not_downgraded(self):
        for text,kind in [('200抽','label'),('不易掉絮','action_result'),('悬挂式设计','display')]:
            r=request(text=text,has_product_selling_point=True,display_goal='visible_feature',
                checks=[{'id':'c1','type':kind,'claim_text':text}])
            self.assertNotIn('visual_purpose',hook.normalize([r])[0])

    def test_missing_classifier_and_contradictory_action_cannot_silently_bypass_rule(self):
        missing=request();del missing['has_product_selling_point']
        for r in [missing,request(require_action=True),request(display_goal='visible_feature'),
                request(checks=[{'id':'c1','type':'label','claim_text':'泛红'}])]:
            with self.subTest(row=r),self.assertRaises(ValueError):hook.normalize([r])

    def test_packaging_accompanies_hook_without_certifying_pain_or_competitor_claim(self):
        r=hook.normalize([request()])[0]
        a=assess(packaging(),r,{'start':0,'end':2_000_000})
        self.assertTrue(a['selection_eligible'])
        self.assertEqual(a['coverage_scope'],'packaging_accompaniment_only')
        self.assertEqual(a['claim_truth_supported_ids'],[])
        self.assertEqual(a['fully_supported_ids'],[])
        self.assertEqual(a['uncovered_spoken_claims'],[r['text']])

    def test_body_only_or_wrong_product_cannot_replace_packaging(self):
        r=hook.normalize([request()])[0]
        for changes in [{'packaging_visible':False},{'packaging_primary':False},{'same_product':False},{'evidence_intervals_us':[]}]:
            self.assertFalse(assess(packaging(**changes),r,{'start':0,'end':2_000_000})['selection_eligible'])

    def test_outside_window_or_fleeting_packaging_does_not_pass(self):
        r=hook.normalize([request()])[0]
        with self.assertRaisesRegex(ValueError,'OUTSIDE_WINDOW'):
            assess(packaging(evidence_intervals_us=[{'start':0,'end':3_000_000}]),r,{'start':0,'end':2_000_000})
        a=assess(packaging(evidence_intervals_us=[{'start':0,'end':200_000}]),r,{'start':0,'end':2_000_000})
        self.assertFalse(a['selection_eligible'])

    def test_forged_hook_marker_and_replaced_original_claim_are_rejected(self):
        for r in [request(4,visual_purpose=hook.PURPOSE),request(has_product_selling_point=True,visual_purpose=hook.PURPOSE)]:
            with self.assertRaises(ValueError):assess(packaging(),r,{'start':0,'end':2_000_000})
        r=hook.normalize([request()])[0];r['checks'][0]['claim_text']='柔软'
        with self.assertRaisesRegex(ValueError,'REQUEST_CLAIM_SUBSTITUTED'):
            assess(packaging(),r,{'start':0,'end':2_000_000})

    def test_legacy_or_unnormalized_completed_classification_is_rejected(self):
        r=request()
        with self.assertRaisesRegex(ValueError,'NORMALIZATION_REQUIRED'):hook.validate_completed([r])
        del r['has_product_selling_point']
        with self.assertRaisesRegex(ValueError,'CLASSIFICATION_REQUIRED'):hook.validate_completed([r])
        hook.validate_completed(hook.normalize([request()]))

    def test_normalization_is_idempotent_and_keeps_original_checks(self):
        once=hook.normalize([request()]);twice=hook.normalize(once)
        self.assertEqual(once,twice)
        self.assertEqual(once[0]['hook_original_checks'],request()['checks'])


if __name__=='__main__':unittest.main()
