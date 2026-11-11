"""Actual window behavior: useful onset/landing survive every placement path."""
import copy
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import ai_editor as editor, ai_editor_contract as contract, shot_analyzer
from orchestrator.policy import presentation, playback
from orchestrator.quality import ai_editor_qc
from orchestrator.selection import timing_contract as timing


def phase(**changes):
    raw = dict(schema=presentation.SCHEMA, kind="dynamic", start_us=100_000,
               reveal_us=700_000, readable_end_us=1_100_000, end_us=2_200_000,
               action="商品从侧向转向镜头", result="正面包装到位并可辨认",
               change=dict(kind='orientation', actor='product', before='商品侧面朝向镜头', after='商品正面朝向镜头', product_state_changed=True),
               start_censored=False, end_censored=False)
    raw.update(changes)
    return presentation.normalize(raw, {"start": 0, "end": 8_000_000},
                                  [100_000, 500_000, 900_000, 1_100_000, 2_000_000])


class PresentationWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.video = Path(self.tmp.name) / "source.mp4"
        self.video.write_bytes(b"test source identity")
        self.shot = dict(shot_id="display", video=str(self.video), status="ready_for_matching",
                         source_start=0, source_end=8, material_duration_us=8_000_000)
        self.review = dict(shot_id="display", verdict="matched", same_product=True,
                           basis="product_display", observation="商品转正后展示包装", reason="同商品展示",
                           confidence=.9, supported_point_ids=[], time_sensitive=False,
                           evidence_intervals_us=[{"start": 700_000, "end": 1_100_000}],
                           source_identity=contract.source_identity(self.video), presentation_observation=phase())
        self.manifest = dict(selection_mode="ai_editor", product="fixture", head_trim_s=.3,
                             tail_trim_s=.2, tail_pad_s=0,
                             segments=[dict(text="来看看这个商品", audio_duration_us=1_300_000)])
        self.sentence = dict(index=1, text="来看看这个商品", kind="hook", selling_points=[], scene_ids=[])

    def plan(self):
        return dict(schema=contract.PLAN_SCHEMA, policy_version=contract.POLICY_VERSION,
                    identity={"editorial_rules": contract.rules_identity(self.manifest),
                              "model": "fixture", "manifest_sha256": contract.digest(self.manifest),
                              "timing_sha256": contract.digest(None),
                              "analysis_sha256": contract.digest({"shots": [self.shot]})},
                    sentences=[self.sentence], reviews={}, display_reviews=[self.review],
                    rows=[dict(index=1, parts=[dict(shot_id="display", audio_duration_us=1_300_000)])])

    def test_early_change_overrides_midpoint_and_generic_head_trim(self):
        observed = editor._observed_shot(self.shot, self.review)
        solved = shot_analyzer.candidate_window(observed, audio_duration_us=1_300_000, manifest=self.manifest)
        self.assertTrue(solved.ok, solved)
        self.assertEqual(solved.source_start_us, 100_000)
        self.assertEqual(solved.source_end_us, 1_400_000)
        self.assertEqual(presentation.cut_errors(phase(), solved.source_start_us, solved.source_end_us), [])

    def test_no_static_fallback_when_action_does_not_fit(self):
        observed = editor._observed_shot(self.shot, self.review)
        self.assertFalse(shot_analyzer.candidate_window(observed, audio_duration_us=600_000,
                                                      manifest=self.manifest).ok)
        self.assertFalse(shot_analyzer.candidate_window(observed, audio_duration_us=3_000_000,
                                                      manifest=self.manifest).ok)

    def test_dynamic_failure_reaches_repairs_before_a_feasible_static_substitute(self):
        from orchestrator.selection.ai_editor_recovery import WindowFailure
        analysis,plan=self.short_display_pair()
        static_video=Path(self.tmp.name)/'static.mp4'
        static_video.write_bytes(b'static product')
        static_shot={**self.shot,'shot_id':'static','video':str(static_video)}
        static={**plan['display_reviews'][0],'shot_id':'static',
                'source_identity':contract.source_identity(static_video)}
        static.pop('presentation_observation')
        analysis['shots'].append(static_shot)
        plan['display_reviews'].append(static)
        self.assertTrue(list(editor._short_complete_rows(self.manifest,analysis,plan,1,None)))
        before=copy.deepcopy(plan['rows'])
        with self.assertRaises(WindowFailure):
            editor._revalidate_rows(self.manifest,analysis,plan,narration_timing=None)
        self.assertEqual(plan['rows'],before)

    def test_broad_evidence_failure_can_review_another_complete_action(self):
        from orchestrator.selection.ai_editor_recovery import WindowFailure
        plan = self.plan()
        failure = WindowFailure(1, 'display', timing.REASON_EVIDENCE_OUT_OF_RANGE)
        with mock.patch.object(editor, '_place_absorb_experiment'), \
                mock.patch.object(editor.experiment_groups, 'repair'), \
                mock.patch.object(editor, '_place_empty_matched_evidence'), \
                mock.patch.object(editor, '_prefer_reviewed_actions'), \
                mock.patch.object(editor, '_revalidate_rows', side_effect=[failure, {'ok': True}]), \
                mock.patch.object(editor, '_retrieve_empty_selection', return_value=False), \
                mock.patch.object(editor, '_extend_previous_action', return_value=False), \
                mock.patch.object(editor, '_compose_reviewed_short_displays', return_value=False), \
                mock.patch.object(editor, '_try_other_presentation_window', return_value=True) as alternate:
            self.assertTrue(editor._repair_reviewed_rows(self.manifest, {'shots': [self.shot]},
                plan, Path(self.tmp.name), None, set())['ok'])
        alternate.assert_called_once()

    def test_plain_display_is_replaced_by_available_complete_action_without_new_calls(self):
        plan=self.plan()
        static_video=Path(self.tmp.name)/'static.mp4'
        static_video.write_bytes(b'static product')
        static_shot={**self.shot,'shot_id':'static','video':str(static_video)}
        static={**self.review,'shot_id':'static','source_identity':contract.source_identity(static_video)}
        static.pop('presentation_observation')
        plan['display_reviews'].append(static)
        plan['rows'][0]['parts'][0]['shot_id']='static'
        plan['window_reviews']={'line_fit':{**self.review,'sentence_index':1,
            'review_window_us':{'start':100_000,'end':1_400_000},
            'presentation_observation':phase(end_us=1_400_000),
            'sentence_fit':True,'sentence_fit_claim':self.sentence['text'],
            'sentence_observation':'本句为泛促销，实际完整转正展示同商品'}}
        analysis={'shots':[self.shot,static_shot]}
        self.assertTrue(editor._assemble(self.manifest,analysis,plan,narration_timing=None)['ok'])
        with mock.patch.object(editor,'_call',side_effect=AssertionError('existing complete pixels suffice')):
            editor._prefer_reviewed_actions(self.manifest,analysis,plan,Path(self.tmp.name),None)
        actual=editor._assemble(self.manifest,analysis,plan,narration_timing=None)
        self.assertEqual(actual['segments'][0]['shot_id'],'display')
        self.assertEqual(actual['segments'][0]['source_start'],.1)
        self.assertTrue(actual['segments'][0]['ai_selection']['sentence_fit']['ok'])
        plan['window_reviews']['line_fit']['sentence_fit']=False
        with self.assertRaisesRegex(ValueError,'AI_EDITOR_SENTENCE_FIT_UNOBSERVED'):
            editor._assemble(self.manifest,analysis,plan,narration_timing=None)
        plan['window_reviews']['line_fit']['sentence_fit']=True
        plan['rows'][0]['parts'][0]['shot_id']='static'
        plan['_frozen_segments']={1:copy.deepcopy(actual['segments'][0])}
        with mock.patch.object(editor,'_try_other_presentation_window',side_effect=AssertionError('frozen')):
            editor._prefer_reviewed_actions(self.manifest,analysis,plan,Path(self.tmp.name),None)
        self.assertEqual(plan['rows'][0]['parts'][0]['shot_id'],'static')
        plan.pop('_frozen_segments')
        for text in ('到手五包', '到手5包', '这次买正装送正装', '一整年的量',
                     '看看双包', '看看三大包', '看看两盒', '看看半斤', '看看三种香型', '看看加大号', '荧光检测'):
            plan['sentences'][0]['text']=text
            with mock.patch.object(editor,'_try_other_presentation_window',side_effect=AssertionError('spoken fact')):
                editor._prefer_reviewed_actions(self.manifest,analysis,plan,Path(self.tmp.name),None)
            self.assertEqual(plan['rows'][0]['parts'][0]['shot_id'],'static')
        plan['sentences'][0]['text']=self.sentence['text']='看看这个商品'
        from orchestrator.selection import shot_notes
        with mock.patch.object(shot_notes,'note_for',return_value={'requested':'keep'}), \
                mock.patch.object(editor,'_try_other_presentation_window',side_effect=AssertionError('note')):
            editor._prefer_reviewed_actions(self.manifest,analysis,plan,Path(self.tmp.name),None)
        self.assertEqual(plan['rows'][0]['parts'][0]['shot_id'],'static')

    def test_phase_does_not_authorize_acceleration(self):
        observed = editor._observed_shot(self.shot, self.review)
        self.assertEqual(playback.speed_range(observation=observed.get("playback_observation")), (1, 1))
        self.assertEqual(observed["speed_range_override"], [1, 1])

    def test_dynamic_requires_observed_product_state_change(self):
        for change in (None, dict(kind='pointing', actor='hand_only', before='整箱已摆好', after='手指向整箱', product_state_changed=True),
                       dict(kind='reveal', actor='occluder', before='整箱已摆好', after='整箱已摆好', product_state_changed=True),
                       dict(kind='orientation', actor='product', before='侧面', after='正面', product_state_changed=False),
                       dict(kind='enter', actor='camera', before='背景', after='商品', product_state_changed=True),
                       dict(kind='enter', actor='hand_only', before='商品', after='手指向商品', product_state_changed=True)):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError,'PRODUCT_CHANGE_UNOBSERVED'):
                phase(change=change)

    def test_legacy_frozen_phase_stays_readable_but_cannot_be_new_observation(self):
        old=phase()
        old['schema']=presentation.LEGACY_SCHEMA
        old.pop('change')
        self.assertEqual(presentation.get(old)['schema'],presentation.LEGACY_SCHEMA)
        with self.assertRaisesRegex(ValueError,'SCHEMA_INVALID'):
            presentation.normalize(old,old['review_window_us'],old['frame_times_us'])
        old_v2=phase()
        old_v2['schema']='reviewed-presentation-phases-v2'
        old_v2['change'].pop('actor')
        self.assertEqual(presentation.get(old_v2)['schema'],old_v2['schema'])
        self.assertEqual(presentation.get(old_v2)['change'],old_v2['change'])
        with self.assertRaisesRegex(ValueError,'SCHEMA_INVALID'):
            presentation.normalize(old_v2,old_v2['review_window_us'],old_v2['frame_times_us'])

    def test_span_and_single_use_identical_phase_bounds(self):
        observed = editor._observed_shot(self.shot, self.review)
        span = shot_analyzer.span_segment_for(observed, manifest=self.manifest)
        self.assertEqual((span.lo_bound_us, span.hi_bound_us), (100_000, 2_200_000))
        self.assertEqual((span.evidence_start_us, span.evidence_end_us), (100_000, 1_100_000))

    def test_observations_in_analysis_do_not_certify_phase(self):
        raw = {**self.shot, "presentation_observation": phase()}
        review = {k: v for k, v in self.review.items() if k != "presentation_observation"}
        self.assertNotIn("presentation_observation", editor._observed_shot(raw, review))

    def test_landing_cut_and_reset_have_distinct_rejections(self):
        self.assertIn("PRESENTATION_ONSET_CUT_AWAY", presentation.cut_errors(phase(), 500_000, 1_500_000))
        self.assertIn("PRESENTATION_LANDING_CUT_AWAY", presentation.cut_errors(phase(), 100_000, 900_000))
        self.assertIn("PRESENTATION_RESET_OR_REPEAT_INCLUDED", presentation.cut_errors(phase(), 100_000, 2_500_000))

    def test_censored_reversed_and_single_frame_phases_are_not_certified(self):
        for updates in ({"start_censored": True}, {"end_censored": True}, {"reveal_us": True},
                        {"readable_end_us": 600_000}, {"end_us": 9_000_000}):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                phase(**updates)
        value = phase()
        with self.assertRaisesRegex(ValueError, "SAMPLES_INSUFFICIENT"):
            presentation.normalize(value, value["review_window_us"], [900_000])

    def test_shared_pool_assembly_binds_actual_phase_and_rejects_tampering(self):
        result = editor._assemble(self.manifest, {"shots": [self.shot]}, self.plan(), narration_timing=None)
        cut = result["segments"][0]
        self.assertTrue(contract.validate_segment_receipt(cut, index=1, manifest=self.manifest)["ok"])
        self.assertEqual(cut["source_start"], .1)
        altered = copy.deepcopy(cut)
        altered["presentation_observation"]["end_us"] = 5_000_000
        self.assertIn("PRESENTATION_OBSERVATION_CHANGED", contract.validate_segment_receipt(
            altered, index=1, manifest=self.manifest)["reasons"])

    def test_placement_cannot_erase_parent_onset_before_model_call(self):
        media = dict(start=.5, end=1.8, before=self.review["source_identity"], times=[.5, 1, 1.5], sheet=Path("fake.jpg"))
        with mock.patch.object(editor, "_review_media", return_value=media), mock.patch.object(editor, "_call") as call:
            with self.assertRaisesRegex(ValueError, "PRESENTATION_CROP_CUTS_ACTION"):
                editor._review_placement_window(self.manifest, self.sentence, self.shot,
                                               Path(self.tmp.name), parent=self.review)
            call.assert_not_called()

    def test_phase_is_not_specific_selling_point_evidence(self):
        plan = self.plan()
        plan["sentences"][0] = {**self.sentence, "kind": "selling_point", "selling_points": [{"id": "p1", "text": "吸水"}]}
        plan["reviews"] = {"1": [self.review]}
        with self.assertRaisesRegex(ValueError, "POINTS_UNCOVERED"):
            editor._assemble(self.manifest, {"shots": [self.shot]}, plan, narration_timing=None)

    def test_final_qc_rejects_small_shift_that_removes_onset(self):
        cut = editor._assemble(self.manifest, {"shots": [self.shot]}, self.plan(), narration_timing=None)["segments"][0]
        claim = {"ai_segment": cut, "index": 1}
        row = {"source": str(self.video), "source_start": .15, "source_duration": 1.25, "target_duration": 1.3}
        self.assertIn("PRESENTATION_ONSET_CUT_AWAY", ai_editor_qc.validate_cut(claim, [row]))

    def test_qc_samples_observed_reveal_and_readable_landing(self):
        cut = editor._assemble(self.manifest, {"shots": [self.shot]}, self.plan(), narration_timing=None)["segments"][0]
        row = {"source_start": .1, "source_duration": 1.3}
        frames = ai_editor_qc.frame_plan({"ai_segment": cut}, [row])
        self.assertTrue(any(abs(fraction - (.7 - .1) / 1.3) < 1e-9 for _, _, fraction in frames))
        self.assertTrue(any(abs(fraction - (1.06 - .1) / 1.3) < 1e-9 for _, _, fraction in frames))

    def test_group_protects_whole_presentation_instead_of_replaying_per_member(self):
        group = {"input": {"window_us": {"start": 100_000, "end": 2_000_000}, "presentation_observations": [phase()]}}
        self.assertEqual(presentation.group_errors(group), [])
        group["input"]["window_us"]["start"] = 500_000
        self.assertIn("PRESENTATION_ONSET_CUT_AWAY", presentation.group_errors(group))

    def test_shared_display_recall_balances_short_and_long_lines(self):
        sentences = [{"candidate_ids": ["hook", "second", "tail"]},
                     {"candidate_ids": ["cta", "second", "tail"]}]
        self.assertEqual(editor._display_candidate_ids(sentences), ["hook", "cta", "second", "tail"])

    def test_inventory_preserves_line_actions_before_common_display_pool(self):
        shots = [{**self.shot, 'shot_id': sid} for sid in ('common', 'turn', 'open')]
        manifest = {**self.manifest, 'segments': [{'text': '看这个'}, {'text': '再看看'}]}
        response = {'sentences': [dict(index=i, kind='hook', selling_points=[], scene_ids=[],
            candidate_ids=[sid]) for i, sid in ((1, 'turn'), (2, 'open'))],
            'display_pool_ids': ['common']}
        with mock.patch.object(editor, '_call', return_value=response):
            rows = editor._inventory(manifest, shots, Path(self.tmp.name))
        self.assertEqual(rows[0]['candidate_ids'], ['turn', 'common', 'open'])
        self.assertEqual(rows[1]['candidate_ids'], ['open', 'common', 'turn'])
        self.assertEqual(editor._display_candidate_ids(rows)[:3], ['turn', 'open', 'common'])

    def test_fallback_keeps_feasible_action_ahead_of_higher_confidence_static(self):
        static = {k: v for k, v in self.review.items() if k != 'presentation_observation'}
        static.update(shot_id='static', confidence=1.)
        plan = self.plan()
        plan['display_reviews'].append(static)
        analysis = {'shots': [self.shot, {**self.shot, 'shot_id': 'static'}]}
        rows = list(editor._short_complete_rows(self.manifest, analysis, plan, 1, None))
        self.assertEqual(rows[0]['parts'][0]['shot_id'], 'display')

    def test_shared_history_does_not_replace_verified_action_with_unused_static(self):
        video = Path(self.tmp.name)/'unused.mp4'
        video.write_bytes(b'another source')
        static_shot = {**self.shot, 'shot_id': 'static', 'video': str(video)}
        static = {k:v for k,v in self.review.items() if k!='presentation_observation'}
        static.update(shot_id='static', source_identity=contract.source_identity(video), confidence=1.)
        plan=self.plan()
        plan['display_reviews'].append(static)
        analysis={'shots':[self.shot,static_shot]}
        with mock.patch.object(editor, '_prior_use_by_shot', return_value={'display':(4,4),'static':(0,0)}):
            menu=editor._build_edit_inventory(self.manifest, analysis, plan, None)
        self.assertEqual(menu[0]['candidates'][0]['shot_id'], 'display')
        with mock.patch.object(editor, '_assemble', side_effect=AssertionError('must keep the complete action')):
            editor._mix_repeated_displays(self.manifest, analysis, plan, None, menu)
        self.assertEqual(plan['rows'][0]['parts'][0]['shot_id'], 'display')

    def test_all_used_line_priorities_keep_first_seven_and_one_fresh_review_slot(self):
        shots=[{**self.shot,'shot_id':str(i)} for i in range(9)]
        priorities=[s['shot_id'] for s in shots[:8]]
        response={'sentences':[dict(index=1,kind='hook',selling_points=[],scene_ids=[],candidate_ids=priorities)],
                  'display_pool_ids':priorities}
        with mock.patch.object(editor, '_call', return_value=response), mock.patch.object(editor,
                '_prior_use_by_shot',return_value={str(i):(int(i<8),0) for i in range(9)}):
            row=editor._inventory(self.manifest,shots,Path(self.tmp.name))[0]
        self.assertEqual(row['candidate_ids'][:7],priorities[:7])
        self.assertEqual(row['candidate_ids'][7],'8')
        self.assertIn('7',row['candidate_ids'][8:])

    def short_display_pair(self):
        video=Path(self.tmp.name)/'second.mp4'
        video.write_bytes(b'second observed product action')
        other={**self.shot,'shot_id':'second','video':str(video)}
        action=phase(reveal_us=300_000,readable_end_us=500_000,end_us=900_000)
        first={**self.review,'presentation_observation':action,
               'evidence_intervals_us':[{'start':300_000,'end':500_000}]}
        second={**first,'shot_id':'second','source_identity':contract.source_identity(video)}
        plan=self.plan()
        plan['display_reviews']=[first,second]
        return {'shots':[self.shot,other]},plan

    def test_two_complete_short_actions_fill_one_longer_line_without_paid_review(self):
        analysis,plan=self.short_display_pair()
        self.assertEqual(list(editor._short_complete_rows(self.manifest,analysis,plan,1,None)),[])
        with mock.patch.object(editor,'_call',side_effect=AssertionError('no new review')):
            self.assertTrue(editor._compose_reviewed_short_displays(self.manifest,analysis,plan,Path(self.tmp.name),1,None))
        result=editor._assemble(self.manifest,analysis,plan,narration_timing=None,verify_sources=True)
        parts=result['segments'][0]['span_parts']
        self.assertEqual(len(parts),2)
        self.assertEqual(sum(round(p['segment_s']*1e6) for p in parts),1_300_000)
        self.assertTrue(all(p['video_speed']==1 for p in parts))
        self.assertTrue(contract.validate_segment_receipt(result['segments'][0],index=1,manifest=self.manifest)['ok'])

    def test_short_action_composition_cannot_borrow_occupied_or_unobserved_source(self):
        analysis,plan=self.short_display_pair()
        plan['rows'].append(dict(index=2,parts=[dict(shot_id='second')]))
        before=copy.deepcopy(plan['rows'])
        self.assertFalse(editor._compose_reviewed_short_displays(self.manifest,analysis,plan,Path(self.tmp.name),1,None))
        self.assertEqual(plan['rows'],before)
        plan['rows'].pop()
        plan['display_reviews'][1].pop('presentation_observation')
        self.assertFalse(editor._compose_reviewed_short_displays(self.manifest,analysis,plan,Path(self.tmp.name),1,None))

    def test_short_source_uses_fresh_tight_review_of_another_matched_action(self):
        analysis,plan=self.short_display_pair()
        other=plan['display_reviews'][1]
        other['presentation_observation']=phase(readable_end_us=1_200_000,end_us=2_400_000)
        other['evidence_intervals_us']=[{'start':300_000,'end':7_000_000}]
        media=dict(start=.1,end=1.48,before=other['source_identity'],times=[.1,.5,.9,1.3],sheet=Path('pixels.jpg'))
        response=dict(verdict='suitable',same_product=True,observation='同商品转正并可辨认',reason='完整紧凑动作',
                      confidence=.9,evidence_intervals_us=[{'start':300_000,'end':1_480_000}],
                      presentation_observation=phase(readable_end_us=1_200_000,end_us=1_480_000))
        with mock.patch.object(editor,'_review_media',return_value=media),mock.patch.object(editor,'_call',return_value=response):
            self.assertTrue(editor._try_other_presentation_window(self.manifest,analysis,plan,Path(self.tmp.name),1,None))
        actual=editor._assemble(self.manifest,analysis,plan,narration_timing=None,verify_sources=True)
        self.assertEqual(actual['segments'][0]['shot_id'],'second')
        self.assertEqual(actual['segments'][0]['source_start'],.1)
        self.assertGreater(actual['segments'][0]['video_speed'],1)
        self.assertLessEqual(actual['segments'][0]['video_speed'],2)
        self.assertEqual(other['evidence_intervals_us'],[{'start':300_000,'end':7_000_000}])
        self.assertEqual(len(plan['window_repair_attempts']),1)

    def test_positive_crop_that_is_still_too_short_does_not_replace_row(self):
        plan=self.plan()
        plan['window_reviews']={'too_short':{**self.review,'sentence_index':1,
            'review_window_us':{'start':100_000,'end':1_480_000},
            'presentation_observation':phase(end_us=1_350_000)}}
        before=copy.deepcopy(plan['rows'])
        with mock.patch.object(editor,'_review_proposed_window',return_value='too_short'):
            self.assertFalse(editor._try_other_presentation_window(self.manifest,
                {'shots':[self.shot]},plan,Path(self.tmp.name),1,None))
        self.assertEqual(plan['rows'],before)
        self.assertIn('too_short',plan['window_reviews'])

    def test_matched_static_display_crop_gets_only_reviewed_placement_speed_and_sentence_fit(self):
        manifest={**self.manifest,'segments':[dict(text=self.sentence['text'],audio_duration_us=600_000)]}
        plan=self.plan()
        plan['display_reviews'][0].pop('presentation_observation')
        plan['display_reviews'][0]['review_window_us']={'start':0,'end':8_000_000}
        media=dict(start=.3,end=1.5,before=self.review['source_identity'],times=[.3,.6,.9,1.2,1.46],sheet=Path('static.jpg'))
        raw=dict(verdict='suitable',same_product=True,observation='同商品已稳定可辨认',reason='本句泛促销可用',
            confidence=.9,evidence_intervals_us=[{'start':300_000,'end':1_500_000}],
            sentence_fit=True,sentence_observation='原句没有规格性能，实窗展示同商品')
        with mock.patch.object(editor,'_review_media',return_value=media),mock.patch.object(editor,'_call',return_value=raw), \
                mock.patch.object(editor,'_review',side_effect=AssertionError('matched parent needs placement review')):
            key=editor._review_proposed_window(manifest,plan,Path(self.tmp.name),self.sentence,self.shot,(300_000,1_500_000))
        self.assertIsNotNone(key)
        plan['rows'][0]['parts']=[dict(shot_id='display',audio_duration_us=600_000,source_start_us=300_000,window_review_key=key)]
        actual=editor._assemble(manifest,{'shots':[self.shot]},plan,narration_timing=None)['segments'][0]
        self.assertEqual(actual['video_speed'],2)
        self.assertTrue(actual['ai_selection']['sentence_fit']['ok'])
        self.assertEqual(actual['ai_selection']['supported_point_ids'],[])

    def test_half_second_cta_does_not_force_whole_library_review(self):
        shots = {f"s{i}": {**self.shot, "shot_id": f"s{i}"} for i in range(24)}
        sentence = {**self.sentence, "index": 0, "kind": "product_intro", "candidate_ids": list(shots)}
        def run(ids, shots, directory, review, **kwargs):
            return iter((sid, review(sid)) for sid in ids)
        with mock.patch.object(editor, "_iter_review_round", side_effect=run), \
                mock.patch.object(editor, "_review", side_effect=lambda m, s, shot, d, **kwargs: {
                    **self.review, "shot_id": shot["shot_id"], "presentation_observation": None}), \
                mock.patch.object(editor, "_cached_reviews_feasible", return_value=False):
            rows, coverage = editor._review_pool(self.manifest, sentence, shots, Path(self.tmp.name), {})
        self.assertEqual(len(rows), 8)
        self.assertEqual(coverage["reason"], "KNOWN_WINDOWS_INFEASIBLE")
        self.assertFalse(coverage["exhausted"])

    def test_same_pixel_phase_fill_is_reused_without_repeating_semantic_review(self):
        parent = {k: v for k, v in self.review.items() if k != 'presentation_observation'}
        parent.update(frame_times_us=[0, 500_000, 1_000_000], sheet='fixture.jpg',
                      review_window_us={'start': 0, 'end': 8_000_000})
        other = copy.deepcopy(parent)
        plan = {'display_reviews': [parent], 'reviews': {'1': [other]}}
        with mock.patch.object(editor, '_presentation_review', return_value={
                'presentation_status': 'dynamic', 'presentation_observation': phase()}) as reviewed:
            editor._prepare_presentations({'shots': [self.shot]}, plan, Path(self.tmp.name))
        self.assertEqual(reviewed.call_count, 1)
        self.assertEqual(other['presentation_observation'], phase())
        self.assertEqual(other['evidence_intervals_us'], self.review['evidence_intervals_us'])

    def test_local_action_pixels_replace_overlong_phase_without_trusting_old_hint(self):
        shot = {**self.shot, 'action_segments': [
            {'start': .4, 'end': 1.4, 'action': 'open', 'result': 'revealed'},
            {'start': 4., 'end': 7., 'action': 'take out', 'result': 'package front'}]}
        parent = {**self.review, 'review_window_us': {'start': 0, 'end': 8_000_000},
                  'sheet': 'fixture.jpg', 'frame_times_us': [0, 1_000_000, 4_000_000, 7_000_000]}
        plan = {'display_reviews': [parent], 'reviews': {}}
        with mock.patch.object(editor, '_presentation_review', return_value={
                'presentation_status': 'uncertain', 'local_action_review': True}) as review:
            editor._prepare_presentations({'shots': [shot]}, plan, Path(self.tmp.name))
        review.assert_called_once()
        self.assertEqual(parent['presentation_observation'], phase())
        self.assertEqual(parent['evidence_intervals_us'], self.review['evidence_intervals_us'])
        focused = editor._presentation_focus(shot, parent['review_window_us'])
        self.assertLess(focused['end'], 4_000_000)  # Later action never shares this look.
        self.assertLessEqual(focused['start'], 400_000)
        self.assertGreater(focused['end'], 1_400_000)  # Landing context remains observable.
        self.assertEqual(editor._presentation_focus(self.shot, parent['review_window_us']), parent['review_window_us'])

    def test_dense_crop_may_refine_readability_without_changing_onset_or_reveal(self):
        media = dict(start=.1, end=1., before=self.review['source_identity'],
                     times=[.1, .5, .7, .9], sheet=Path('fixture.jpg'))
        refined = phase(readable_end_us=900_000, end_us=1_000_000)
        response = dict(verdict='suitable', same_product=True, observation='转正后正面已可辨认',
                        reason='新短窗实际展示足够', confidence=.9,
                        evidence_intervals_us=[{'start': 700_000, 'end': 900_000}],
                        presentation_observation=refined)
        with mock.patch.object(editor, '_review_media', return_value=media), \
                mock.patch.object(editor, '_call', return_value=response):
            review = editor._review_placement_window(self.manifest, self.sentence, self.shot,
                Path(self.tmp.name), parent=self.review)
        self.assertEqual(review['verdict'], 'matched')
        self.assertEqual(review['presentation_observation']['readable_end_us'], 900_000)
        self.assertEqual(review['parent_presentation_observation'], phase())
        self.assertEqual(self.review['presentation_observation'], phase())
        legacy_parent=copy.deepcopy(self.review)
        legacy_parent['presentation_observation']['schema']='reviewed-presentation-phases-v2'
        legacy_parent['presentation_observation']['change'].pop('actor')
        with mock.patch.object(editor, '_review_media', return_value=media), \
                mock.patch.object(editor, '_call', return_value=response):
            upgraded=editor._review_placement_window(self.manifest,self.sentence,self.shot,
                Path(self.tmp.name),parent=legacy_parent)
        self.assertEqual(upgraded['parent_presentation_observation'],legacy_parent['presentation_observation'])
        self.assertEqual(upgraded['presentation_observation']['change']['actor'],'product')
        response['presentation_observation']['change']['kind']='enter'
        with mock.patch.object(editor, '_review_media', return_value=media), \
                mock.patch.object(editor, '_call', return_value=response):
            with self.assertRaisesRegex(ValueError,'PRESENTATION_CROP_CHANGED_ACTION_KIND'):
                editor._review_placement_window(self.manifest,self.sentence,self.shot,
                    Path(self.tmp.name),parent=self.review)

    def test_phase_crop_cannot_pass_without_fresh_readability_observation(self):
        media = dict(start=.1, end=1., before=self.review['source_identity'],
                     times=[.1, .5, .7, .9], sheet=Path('fixture.jpg'))
        response = dict(verdict='suitable', same_product=True, observation='包装清晰', reason='看得见',
                        confidence=.9, evidence_intervals_us=[{'start': 700_000, 'end': 900_000}])
        with mock.patch.object(editor, '_review_media', return_value=media), \
                mock.patch.object(editor, '_call', return_value=response):
            with self.assertRaisesRegex(ValueError, 'PRESENTATION_SCHEMA_INVALID'):
                editor._review_placement_window(self.manifest, self.sentence, self.shot,
                    Path(self.tmp.name), parent=self.review)

    def test_proactive_replacement_requires_fresh_sentence_semantics_not_generic_product_fit(self):
        media=dict(start=.1,end=1.4,before=self.review['source_identity'],times=[.1,.5,.9,1.3],sheet=Path('pixels.jpg'))
        sentence={**self.sentence,'text':'赶紧看看荧光检测','require_sentence_fit':True}
        raw=dict(verdict='suitable',same_product=True,observation='包装转正，未见荧光检测',reason='同商品完整动作',
            confidence=.9,evidence_intervals_us=[{'start':700_000,'end':1_100_000}],
            presentation_observation=phase(end_us=1_400_000))
        for fit in (None,False,True):
            result={**raw,'sentence_fit':fit,'sentence_observation':'根据本窗实际可见事实核对原句'}
            with mock.patch.object(editor,'_review_media',return_value=media),mock.patch.object(editor,'_call',return_value=result):
                review=editor._review_placement_window(self.manifest,sentence,self.shot,Path(self.tmp.name),parent=self.review)
            self.assertEqual(review['verdict'],'matched' if fit is True else 'uncertain')
            self.assertEqual(review['sentence_fit_claim'],sentence['text'])

    def test_fresh_crop_cannot_expand_parent_into_reset(self):
        media = dict(start=.1, end=3., before=self.review['source_identity'],
                     times=[.1, .5, .7, 1., 2., 2.8], sheet=Path('fixture.jpg'))
        raw = phase(end_us=2_800_000)
        response = dict(verdict='suitable', same_product=True, observation='商品正面', reason='展示',
                        confidence=.9, evidence_intervals_us=[{'start': 700_000, 'end': 1_100_000}],
                        presentation_observation=raw)
        with mock.patch.object(editor, '_review_media', return_value=media), \
                mock.patch.object(editor, '_call', return_value=response):
            with self.assertRaisesRegex(ValueError, 'PRESENTATION_CROP_WIDENED_PARENT'):
                editor._review_placement_window(self.manifest, self.sentence, self.shot,
                    Path(self.tmp.name), parent=self.review)

    def test_dense_crop_can_require_more_reading_time_inside_known_usable_end(self):
        media = dict(start=.1, end=2., before=self.review['source_identity'],
                     times=[.1, .5, .7, 1., 1.5, 1.9], sheet=Path('fixture.jpg'))
        refined = phase(readable_end_us=1_500_000, end_us=2_000_000)
        response = dict(verdict='suitable', same_product=True, observation='包装正面逐渐清晰',
                        reason='密集帧实际需要更久辨认', confidence=.9,
                        evidence_intervals_us=[{'start': 700_000, 'end': 1_500_000}],
                        presentation_observation=refined)
        with mock.patch.object(editor, '_review_media', return_value=media), \
                mock.patch.object(editor, '_call', return_value=response):
            review = editor._review_placement_window(self.manifest, self.sentence, self.shot,
                Path(self.tmp.name), parent=self.review)
        self.assertEqual(review['presentation_observation']['readable_end_us'], 1_500_000)
        self.assertIn('PRESENTATION_LANDING_CUT_AWAY', presentation.cut_errors(
            review['presentation_observation'], 100_000, 1_200_000))
        self.assertEqual(self.review['presentation_observation'], phase())

    def test_independent_qc_receives_action_goal_without_selector_pass_or_timestamp(self):
        claim = {'index': 1, 'product': 'fixture', 'claim': '现在去看看', 'sentence_kind': 'hook',
                 'ai_segment': {'ai_selection': {'presentation_observation': phase(readable_end_us=1_700_001),
                                                'verdict': 'matched', 'observation': 'UNTRUSTED_PASS_TEXT'}}}
        prompt = ai_editor_qc.prompt([claim], 8)
        self.assertIn('商品从侧向转向镜头', prompt)
        self.assertIn('不是已发生事实', prompt)
        self.assertIn('不能偷换成更弱的动作后通过', prompt)
        self.assertNotIn('UNTRUSTED_PASS_TEXT', prompt)
        self.assertNotIn('1700001', prompt)

    def test_action_qc_does_not_borrow_next_unrelated_shots_result(self):
        claims = [{'index': i, 'ai_segment': {}} for i in range(1, 5)]
        claims[1]['ai_segment']['ai_selection'] = {'presentation_observation': phase()}
        batches = ai_editor_qc.claim_batches(claims, 2)
        self.assertEqual([[c['index'] for c in b] for b in batches], [[1], [2], [3, 4]])
        group = {'input': {'members': [{'index': 2}, {'index': 3}], 'presentation_observations': [phase()]}}
        claims[1]['ai_segment']['continuity_group'] = group
        claims[2]['ai_segment']['continuity_group'] = group
        batches = ai_editor_qc.claim_batches(claims, 4)
        self.assertEqual([[c['index'] for c in b] for b in batches], [[1], [2, 3], [4]])

    def test_extra_display_round_is_saved_before_work_and_resumes_same_ids(self):
        import json
        from orchestrator.selection.ai_editor_recovery import WindowFailure
        plan = self.plan()
        plan['sentences'][0]['candidate_ids'] = ['display']
        plan['display_reviews'] = []
        root = Path(self.tmp.name)
        with mock.patch.object(editor, '_edit_plan', side_effect=WindowFailure(1, 'display', 'SOURCE_TOO_SHORT')), \
                mock.patch.object(editor, '_review_pool', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                editor._edit_with_review_fallback(self.manifest, {'shots': [self.shot]}, plan, root, None)
        saved = json.loads((root / 'plan.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['display_extra_round']['candidate_ids'], ['display'])
        self.assertEqual(saved['display_extra_round']['status'], 'reviewing')
        with mock.patch.object(editor, '_review_pool', return_value=([self.review], {})) as review, \
                mock.patch.object(editor, '_prepare_presentations'):
            editor._resume_display_round(self.manifest, {'shots': [self.shot]}, saved, root, None)
        self.assertEqual(review.call_args.args[1]['candidate_ids'], ['display'])
        self.assertEqual(saved['display_extra_round']['status'], 'done')
        self.assertEqual(len(saved['display_reviews']), 1)

    def test_interruption_before_dense_review_does_not_restore_coarse_row_as_complete(self):
        import json
        root = Path(self.tmp.name)
        shared = {**self.sentence, 'index': 0, 'kind': 'product_intro', 'candidate_ids': ['display']}
        state = {'status': 'reviewing', 'candidate_ids': ['display'], 'sentence': shared}
        checkpoint = {'candidate_ids': ['display'], 'presentation_policy_version': presentation.VERSION,
                      'selection_policy_version': contract.POLICY_VERSION, 'model': 'fixture',
                      'reviews': [{**self.review, 'dense_required': True}]}
        for stage in ('initial', 'complete'):
            checkpoint['review_phase'] = stage
            (root / 'review_checkpoint_0.json').write_text(json.dumps(checkpoint), encoding='utf-8')
            plan = {'display_reviews': [], 'display_extra_round': copy.deepcopy(state)}
            with mock.patch.object(editor, '_editor_model', return_value='fixture'), \
                    mock.patch.object(editor, '_review_pool', return_value=([self.review], {})) as review, \
                    mock.patch.object(editor, '_prepare_presentations'):
                editor._resume_display_round(self.manifest, {'shots': [self.shot]}, plan, root, None)
            self.assertEqual(review.call_args.kwargs['existing_reviews'], [])

    def test_tight_reviewed_phase_uses_exact_ratio_without_adding_handles(self):
        args = dict(source_in_us=2_150_000, source_out_us=4_374_000,
                    audio_duration_us=1_132_208, tail_pad_us=0, head_guard_us=0, tail_guard_us=0,
                    evidence_start_us=2_150_000, evidence_end_us=4_374_000, speed_min=1, speed_max=2)
        legacy = timing.solve_window(timing.WindowRequest(**args))
        self.assertFalse(legacy.ok)
        result = timing.solve_window(timing.WindowRequest(**args, exact_source_timing=True))
        self.assertTrue(result.ok, result)
        self.assertEqual((result.source_start_us, result.source_end_us), (2_150_000, 4_374_000))
        self.assertEqual(round(result.to_dict()['speed'] * 1_132_208), 2_224_000)
        self.assertLessEqual(result.speed, 2)
        args['speed_max'] = 1
        self.assertFalse(timing.solve_window(timing.WindowRequest(**args, exact_source_timing=True)).ok)

    def test_writer_request_keeps_reviewed_onset_and_exact_timing(self):
        from orchestrator.application.engine import OrchestrationEngine
        from orchestrator.domain.clips import Clip
        cut = editor._assemble(self.manifest, {'shots': [self.shot]}, self.plan(), narration_timing=None)['segments'][0]
        engine = object.__new__(OrchestrationEngine)
        engine.m, engine._raw_segments = self.manifest, [cut]
        clip = Clip(video=str(self.video), source_start=.1, source_end=1.4, duration=1.3,
                    video_duration=8, raw_index=0, visual_role='product_display',
                    speed_range_override=[1, 1], evidence_intervals=cut['evidence_intervals'])
        req = engine._timing_request(clip, 1_300_000)
        self.assertEqual((req.head_guard_us, req.tail_guard_us), (0, 0))
        self.assertTrue(req.exact_source_timing)
        self.assertEqual(timing.solve_window(req).source_start_us, 100_000)

    def test_continuation_rounding_is_clamped_only_within_one_source_frame(self):
        sentence = {**self.sentence, 'action_continuation': True}
        media = dict(start=2.174917, end=2.662313, before=self.review['source_identity'],
                     times=[2.174917, 2.3, 2.5, 2.62], sheet=Path('fixture.jpg'))
        raw = {k: v for k, v in self.review.items() if k not in {'presentation_observation', 'source_identity'}}
        raw['evidence_intervals_us'] = [{'start': 2_174_000, 'end': 2_662_000}]
        with mock.patch.object(editor, '_review_media', return_value=media), \
                mock.patch.object(editor, '_source_frame_us', return_value=16_667), \
                mock.patch.object(editor, '_call', return_value=copy.deepcopy(raw)):
            review = editor._review(self.manifest, sentence, self.shot, Path(self.tmp.name), dense=True)
        self.assertEqual(review['evidence_intervals_us'][0]['start'], 2_174_917)
        self.assertTrue(review['evidence_endpoint_clamps'])
        raw['evidence_intervals_us'][0]['start'] = 2_100_000
        with mock.patch.object(editor, '_review_media', return_value=media), \
                mock.patch.object(editor, '_source_frame_us', return_value=16_667), \
                mock.patch.object(editor, '_call', return_value=raw):
            with self.assertRaisesRegex(ValueError, 'AI_EDITOR_EVIDENCE_INVALID'):
                editor._review(self.manifest, sentence, self.shot, Path(self.tmp.name), dense=True)

    def test_json_interval_pair_forms_are_normalized_without_dropping_evidence(self):
        expected = [{'start': 2_174_917, 'end': 2_662_313}]
        self.assertEqual(editor._evidence_intervals([2_174_917, 2_662_313]), expected)
        self.assertEqual(editor._evidence_intervals([[2_174_917, 2_662_313]]), expected)
        mixed = [[2_174_917, 2_662_313], {'start': 3_000_000, 'end': 4_000_000}]
        self.assertEqual(editor._evidence_intervals(mixed), expected + [mixed[1]])
        invalid = [[True, 2_662_313], [0, 1, 2]]
        self.assertEqual(editor._evidence_intervals(invalid), invalid)

    def test_continuation_respects_direct_review_and_group_usable_end(self):
        plan = self.plan()
        self.assertEqual(editor._presentation_limit(plan, 1, 'display'), 2_200_000)
        plan['display_reviews'][0].pop('presentation_observation')
        plan['rows'][0]['continuity_key'] = 'group'
        plan['continuity_reviews'] = {'group': {'input': {'shot_id': 'display',
                                                        'presentation_observations': [phase()]}}}
        self.assertEqual(editor._presentation_limit(plan, 1, 'display'), 2_200_000)
        plan['rows'][0]['parts'][0]['window_review_key'] = 'narrow'
        plan['window_reviews'] = {'narrow': {'presentation_observation': phase(end_us=1_800_000),
                                            'parent_presentation_observation': phase()}}
        self.assertEqual(editor._presentation_limit(plan, 1, 'display'), 1_800_000)


if __name__ == "__main__":
    unittest.main()
