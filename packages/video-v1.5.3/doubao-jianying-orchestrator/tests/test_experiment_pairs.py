from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import json
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from orchestrator import ai_editor_contract as identity
from orchestrator.selection.experiment_contract import adjacency_hint,rank_pairs,validate_pair
from orchestrator.application import experiment_production as app
from orchestrator.vision.experiment_pair_review import PROTOCOL,input_binding
import test_weipin_experiment as fixture


def observation():
    return {'verdict':'matched','comparisons':{k:{'status':'consistent','observed':'actual A/B correspondence'}
        for k in('product','setup','material_state','verification_correspondence')},
        'phases':[{'part':part,'kind':kind,'first_frame':a,'last_frame':b,'observed':'visible '+kind}
            for part,kind,a,b in [('A','pour',3,9),('A','absorb',6,14),('B','press',3,6),('B','lift',9,11),('B','result',14,20)]],
        'limitations':'sampled pairing evidence, not film acceptance'}


class PairHintTests(unittest.TestCase):
    def test_select_recalls_adjacent_task_file_missing_semantic_verification_tag(self):
        from orchestrator.application import purpose_production as production
        from orchestrator.vision import purpose_review
        with fixture.tempfile.TemporaryDirectory()as raw:
            paths=[str((Path(raw)/f'IMG_{n:04d}.MOV').resolve())for n in(31,32,90)]
            manifest={'task_id':'fixture','category':'weipin','material_sources':paths}
            request={'index':1,'text':'吸收','topic':'absorption','require_action':True,'require_result':True,'checks':[]}
            analysis={'shots':[{'shot_id':'pour','video':paths[0],'source_start':0,'source_end':5}]}
            narration={'rows':[{'index':1,'audio_duration_us':1_000_000}]}
            index={'scans':{'pour':{'description':'pouring only'}}}
            with patch.object(production,'_binding'),patch.object(production,'_audio_binding',return_value=[]), \
                 patch.object(purpose_review,'requirements',return_value=[request]), \
                 patch.object(production.purpose_inventory,'recall',return_value={1:[{'shot_id':'pour'}]}):
                result=production.select(manifest,analysis,narration,index,Path(raw)/'selection')
            pairs=result['experiment_pair_candidates']
            self.assertEqual([(p['first'],p['verification'])for p in pairs],[(paths[0],paths[1])])
            self.assertFalse(pairs[0]['hint']['acceptance_evidence'])
            self.assertIsNone(result['rows'][0]['selected'])

    def test_adjacent31and32_are_only_retrieval_hints(self):
        for a,b in [('material31.mp4','material32.mp4'),('31.MOV','32.MOV'),
                    ('2026_10_21_16_46_IMG_3431.MOV','2026_10_21_16_49_IMG_3432.MOV')]:
            hint=adjacency_hint(a,b);self.assertTrue(hint['adjacent']);self.assertFalse(hint['acceptance_evidence'])

    def test_ranking_keeps_nonadjacent_candidates_and_prefers_adjacent(self):
        rows=rank_pairs(['IMG_0031.MOV'],['IMG_0100.MOV','IMG_0033.MOV','IMG_0032.MOV'])
        self.assertEqual([r['verification']for r in rows],['IMG_0032.MOV','IMG_0033.MOV','IMG_0100.MOV'])
        self.assertFalse(adjacency_hint('dirA/31.mp4','dirB/32.mp4')['adjacent'])

    def test_adjacent_files_with_conflicting_experiments_cannot_pass(self):
        row=observation();row['comparisons']['setup']['status']='conflict'
        self.assertTrue(adjacency_hint('31.mp4','32.mp4')['adjacent'])
        with self.assertRaisesRegex(ValueError,'MATCH_WITHOUT_EVIDENCE'):validate_pair(row,{'A':set(range(21)),'B':set(range(21))})

    def test_inferred_frame_not_shown_cannot_be_evidence(self):
        row=observation();row['phases'][0]['first_frame']=4
        with self.assertRaisesRegex(ValueError,'FRAME_NOT_SHOWN'):
            validate_pair(row,{'A':{0,3,6,9,11,14,17,20},'B':{0,3,6,9,11,14,17,20}})

    def test_missing_verification_or_uncertain_relationship_is_not_matched(self):
        row=observation();row['phases'].pop()
        with self.assertRaisesRegex(ValueError,'MATCH_WITHOUT_EVIDENCE'):validate_pair(row,{'A':set(range(21)),'B':set(range(21))})
        row=observation();row['comparisons']['material_state']['status']='uncertain'
        with self.assertRaisesRegex(ValueError,'MATCH_WITHOUT_EVIDENCE'):validate_pair(row,{'A':set(range(21)),'B':set(range(21))})


class PairApplicationTests(unittest.TestCase):
    def setUp(self):
        self.base=fixture.BoundExperimentTests();self.base.setUp();self.addCleanup(self.base.doCleanups)
        b=self.base;root=b.root/'second';root.mkdir()
        files={n:root/(n+'.mp4')for n in('original','proxy','numbered')}
        for p in files.values():p.write_bytes(b'second-media')
        self.second=deepcopy(b.samples)
        self.second.update(original=identity.source_identity(files['original']),source=identity.source_identity(files['proxy']),video=identity.source_identity(files['numbered']))
        (root/'samples.json').write_text(json.dumps(self.second),encoding='utf-8')
        b.manifest['material_sources'].append(str(files['original']))
        self.sheet=b.root/'pair.jpg';self.sheet.write_bytes(b'fixture image')
        ids=[0,3,6,9,11,14,17,20]
        self.evidence={'protocol':PROTOCOL,'task_id':b.timeline.task_id,'rules':identity.rules_identity(b.manifest),
            'context':{'product':b.manifest['product'],'category':'weipin'},'sheet':identity.source_identity(self.sheet),
            'members':[{'part':part,'samples':samples,'window_frame_ids':[0,20],'visible_frame_ids':ids}
                       for part,samples in [('A',b.samples),('B',self.second)]],
            'observation':observation(),'retrieval_hint':{'adjacent':False}}
        self.bind()

    def bind(self):
        path=self.base.root/'pair.binding.json'
        path.write_text(json.dumps(input_binding(self.base.manifest,self.evidence['members'],self.sheet)),encoding='utf-8')
        self.evidence['input_binding']={'file':identity.source_identity(path),'sha256':identity.file_sha256(path)}

    def apply(self):
        b=self.base
        with patch.object(app,'probe_media',return_value={'duration_us':10_000_000,'width':108,'height':192,'fps_num':60,'fps_den':1}):
            return app.apply(b.manifest,b.timeline,('s1','s2'),b.roles,self.evidence,group_id='pair',prior_origins=b.origins)

    def test_confirmed_pair_can_cross_files_without_changing_voice_or_sentence_times(self):
        new=self.apply();old=self.base.timeline
        self.assertNotEqual(new.videos[0].asset_id,new.videos[1].asset_id)
        self.assertEqual((new.sentences,new.audios,new.subtitles),(old.sentences,old.audios,old.subtitles))
        self.assertEqual(sum(v.duration_us for v in new.videos[:2]),4_000_000)
        self.assertTrue(new.unresolved)

    def test_pairing_match_and_duration_fit_are_separate_checks(self):
        ids=[0,6,11,17,23,29,34,40]
        for m in self.evidence['members']:m.update(window_frame_ids=[0,40],visible_frame_ids=ids)
        row=self.evidence['observation'];row['phases']=[{'part':part,'kind':kind,'first_frame':a,'last_frame':b,'observed':'actual'}
            for part,kind,a,b in [('A','pour',6,17),('A','absorb',11,29),('B','press',6,11),('B','lift',17,23),('B','result',29,40)]]
        self.bind()
        with self.assertRaisesRegex(ValueError,'DURATION_CONFLICT'):self.apply()
        self.assertEqual(row['verdict'],'matched')

    def test_different_experiment_is_rejected_even_with_adjacency_hint(self):
        self.evidence['retrieval_hint']['adjacent']=True
        self.evidence['observation']['verdict']='mismatch'
        self.evidence['observation']['comparisons']['setup']['status']='conflict'
        with self.assertRaisesRegex(ValueError,'PAIR_NOT_CONFIRMED'):self.apply()

    def test_changed_product_sheet_or_frame_window_cannot_reuse_pairing(self):
        old=self.evidence['context']['product'];self.evidence['context']['product']='other SKU'
        with self.assertRaisesRegex(ValueError,'CONTEXT_CHANGED'):self.apply()
        self.evidence['context']['product']=old;self.sheet.write_bytes(b'changed image')
        with self.assertRaisesRegex(ValueError,'SHEET_CHANGED'):self.apply()

    def test_unshown_window_changes_and_unknown_prior_origins_are_rejected(self):
        self.evidence['members'][0]['visible_frame_ids']=[0,1,2]
        with self.assertRaisesRegex(ValueError,'SHOWN_FRAMES_CHANGED'):self.apply()
        self.evidence['members'][0]['visible_frame_ids']=[0,3,6,9,11,14,17,20]
        self.base.origins=[]
        with self.assertRaisesRegex(ValueError,'ORIGIN_UNKNOWN'):self.apply()

    def test_swapped_members_cannot_reuse_original_review(self):
        a,b=self.evidence['members']
        a['samples'],b['samples']=b['samples'],a['samples']
        with self.assertRaisesRegex(ValueError,'REVIEW_INPUT_CHANGED'):self.apply()

    def test_consistently_shifted_window_and_visible_frames_need_new_review(self):
        self.evidence['members'][0].update(window_frame_ids=[3,23],visible_frame_ids=[3,6,9,12,14,17,20,23])
        with self.assertRaisesRegex(ValueError,'REVIEW_INPUT_CHANGED'):self.apply()


if __name__=='__main__':unittest.main()
