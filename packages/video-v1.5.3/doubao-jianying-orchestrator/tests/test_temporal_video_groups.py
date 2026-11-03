from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.domain.timeline import Asset, AudioEvent, Canvas, EditTimeline, Sentence, VideoEvent
from orchestrator.selection.temporal_groups import replace_group
from orchestrator.selection.purpose_selection import fit
from orchestrator.vision import temporal_video
from orchestrator.vision.vision_client import VisionCallResult


def timeline():
    return EditTimeline('fixture', 1, Canvas(), 6_000_000,
        (Asset('v', 'source.mp4', 'video', 20_000_000, 108, 192, 60),
         Asset('a', 'voice.wav', 'audio', 6_000_000)),
        tuple(Sentence(f's{i}', 'text', (i-1)*2_000_000, 2_000_000) for i in range(1,4)),
        tuple(VideoEvent(f'v{i}', 'v', (f's{i}',), (i-1)*2_000_000, 2_000_000,
                         (i-1)*2_000_000, 2_000_000) for i in range(1,4)),
        (AudioEvent('a', 'a', 'narration', 0, 6_000_000),), delivery_mode='preview')


def clip(**kw):
    return dict(asset_id='v', source_start_us=6_000_000, source_end_us=10_000_000,
        target_duration_us=4_000_000, protected_intervals=[{'start':6_000_000,'end':10_000_000}], **kw)


class TemporalGroupTests(unittest.TestCase):
    def test_action_can_span_sentences_without_changing_narration_or_other_picture(self):
        old=timeline(); new=replace_group(old, ('s1','s2'), [clip()], group_id='g')
        self.assertEqual(new.videos[0].sentence_ids, ('s1','s2'))
        self.assertEqual(new.videos[-1], old.videos[-1])
        self.assertEqual((new.sentences,new.audios,new.subtitles), (old.sentences,old.audios,old.subtitles))
        self.assertTrue(new.unresolved)
        self.assertEqual(new.revision,2)

    def test_cannot_freeze_speed_or_truncate_protected_action_to_fit(self):
        c=clip();c['source_end_us']=11_000_000;c['protected_intervals'][0]['end']=11_000_000
        with self.assertRaisesRegex(ValueError,'DURATION_FIT_CONFLICT'):
            replace_group(timeline(),('s1','s2'),[c],group_id='g')

    def test_cannot_use_more_than_three_times_speed_or_underfill_group(self):
        c=clip();c.update(source_end_us=20_000_000,protected_intervals=[])
        with self.assertRaisesRegex(ValueError,'DURATION_FIT_CONFLICT'):
            replace_group(timeline(),('s1','s2'),[c],group_id='g')
        c=clip();c.update(source_end_us=8_000_000,target_duration_us=2_000_000,
                         protected_intervals=[])
        with self.assertRaisesRegex(ValueError,'TARGET_COVERAGE'):
            replace_group(timeline(),('s1','s2'),[c],group_id='g')

    def test_rejects_nonadjacent_or_reordered_sentences_and_formal_mode(self):
        for ids in [('s1','s3'),('s2','s1'),('s1','s1')]:
            with self.assertRaises(ValueError):replace_group(timeline(),ids,[clip()],group_id='g')
        with self.assertRaisesRegex(ValueError,'PREVIEW_ONLY'):
            replace_group(replace(timeline(),delivery_mode='formal'),('s1','s2'),[clip()],group_id='g')

    def test_cannot_replay_retained_source_window_or_cover_only_part_of_event(self):
        c=clip();c.update(source_start_us=4_000_000,source_end_us=8_000_000,protected_intervals=[])
        with self.assertRaisesRegex(ValueError,'SOURCE_REPLAY'):
            replace_group(timeline(),('s1','s2'),[c],group_id='g')
        old=timeline(); cross=replace(old.videos[0],sentence_ids=('s1','s2'),duration_us=4_000_000,source_duration_us=4_000_000)
        old=replace(old,videos=(cross,old.videos[-1]))
        with self.assertRaisesRegex(ValueError,'CUTS_EXISTING_EVENT'):
            replace_group(old,('s2',),[clip()],group_id='g')

    def test_different_proxies_of_same_original_cannot_replay_source(self):
        old=timeline();old=replace(old,assets=(replace(old.assets[0],origin_asset_id='raw'),old.assets[1],
            replace(old.assets[0],asset_id='raw',path='raw.mp4')))
        c=clip();c.update(asset_id='proxy',source_start_us=4_000_000,source_end_us=8_000_000,protected_intervals=[])
        proxy=replace(old.assets[0],asset_id='proxy',path='another-proxy.mp4')
        with self.assertRaisesRegex(ValueError,'SOURCE_REPLAY'):
            replace_group(old,('s1','s2'),[c],group_id='g',assets=(proxy,))

    def test_replacing_picture_never_removes_unresolved_audio_or_other_issues(self):
        old=replace(timeline(),unresolved=({'sentence_id':'s1','reason':'NARRATION_AUDIO_MISSING'},))
        new=replace_group(old,('s1','s2'),[clip()],group_id='g')
        self.assertIn(old.unresolved[0],new.unresolved)

    def test_only_known_replaced_black_diagnostic_becomes_history_not_content_pass(self):
        old=timeline();black=replace(old.videos[0],evidence=({'diagnostic_placeholder':True},))
        issue={'sentence_id':'s1','reason':'NO_CONFIRMED_FIT_WITHIN_SEARCH_BUDGET; black is diagnostic only'}
        other={'sentence_id':'s2','reason':'INDEPENDENT_QC_FAILED'}
        old=replace(old,videos=(black,)+old.videos[1:],unresolved=(issue,other))
        new=replace_group(old,('s1','s2'),[clip()],group_id='g')
        self.assertNotIn(issue,new.unresolved);self.assertIn(other,new.unresolved)
        self.assertIn(issue,new.videos[0].evidence[-1]['replaced_visual_diagnostics'])
        self.assertTrue(any(u.get('reason')=='TEMPORAL_GROUP_OUTPUT_REVIEW_PENDING'for u in new.unresolved))

    def test_result_keeps_its_own_kind_and_minimum_frames(self):
        c=clip();c['protected_intervals']=[{'start':6_000_000,'end':9_500_000,'kind':'action'},
            {'start':9_500_000,'end':10_000_000,'kind':'result','min_output_frames':6}]
        new=replace_group(timeline(),('s1','s2'),[c],group_id='g')
        self.assertEqual(new.videos[0].protected_intervals[-1].kind,'result')
        self.assertEqual(new.videos[0].protected_intervals[-1].min_output_frames,6)

    def test_time_sensitive_process_stays_original_but_explicit_setup_may_speed(self):
        from test_purpose_production import observed
        row=observed();row['time_sensitive']=True
        row['phases'][0].update(kind='setup',can_speed=True)
        # Time-sensitive process + result stay 3s. Only the 1s setup compresses.
        result=fit(row,{'source_start':0,'source_end':5}, {'segment_us':3_500_000})
        self.assertTrue(result['ok']);self.assertEqual(result['protected_us'],3_000_000)
        self.assertEqual(result['maximum_speed'],2)


class NativeVideoTests(unittest.TestCase):
    def test_timestamp_outside_actual_video_is_rejected_not_clamped(self):
        row={'description':'visible action','result_visible':True,'limitations':'',
             'milestones':[{'name':'result','start_us':2_600_000,'end_us':5_000_000}],
             'complete_window_us':{'start':800_000,'end':5_000_000}}
        self.assertFalse(temporal_video.validate_milestones(row,3_800_000))
        row['milestones'][0]['end_us']=3_800_000
        row['complete_window_us']['end']=3_800_000
        self.assertTrue(temporal_video.validate_milestones(row,3_800_000))
        row['milestones'][0]['start_us']=True
        self.assertFalse(temporal_video.validate_milestones(row,3_800_000))

    def test_sends_video_and_requested_fps_through_shared_client_without_image_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'input.mp4';p.write_bytes(b'fixture')
            cfg={'base_url':'https://invalid.example/v1','api_key':'fixture','model':'fixture'}
            with patch.object(temporal_video.vision_analyzer,'_vision_transport',return_value='qwen'), \
                 patch.object(temporal_video.vision_analyzer,'_qwen_settings',return_value=cfg), \
                 patch.object(temporal_video.vision_client,'post_chat',return_value=VisionCallResult(True,text='{}')) as call:
                temporal_video.review(p,'facts',Path(d)/'response.json',fps=10)
                body=call.call_args.args[2];content=body['messages'][0]['content']
                self.assertEqual(content[0]['type'],'video_url');self.assertEqual(content[0]['fps'],10)
                self.assertTrue(content[0]['video_url']['url'].startswith('data:video/mp4;base64,'))
                receipt=json.loads((Path(d)/'response.input.json').read_text('utf-8'))
                self.assertEqual(receipt['input_kind'],'video_url')
                self.assertNotIn('api_key',receipt)

    def test_fps_and_failed_video_route_do_not_silently_fallback(self):
        for value in (True,0,11,float('nan')):
            with self.assertRaisesRegex(ValueError,'FPS_INVALID'):
                temporal_video.review('absent.mp4','',Path('output'),fps=value)
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'input.mp4';p.write_bytes(b'fixture')
            with patch.object(temporal_video.vision_analyzer,'_vision_transport',return_value='qwen'), \
                 patch.object(temporal_video.vision_analyzer,'_qwen_settings',return_value={
                     'base_url':'https://invalid.example','api_key':'fixture','model':'fixture'}), \
                 patch.object(temporal_video.vision_client,'post_chat',return_value=VisionCallResult(False,error_code='HTTP_400')):
                with self.assertRaisesRegex(ValueError,'HTTP_400'):
                    temporal_video.review(p,'facts',Path(d)/'out.json')

    def test_output_cannot_overwrite_input_before_or_after_provider_call(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'input.mp4';p.write_bytes(b'fixture')
            with patch.object(temporal_video.vision_analyzer,'_vision_transport',return_value='qwen'), \
                 patch.object(temporal_video.vision_analyzer,'_qwen_settings',return_value={
                     'base_url':'https://invalid.example','api_key':'fixture','model':'fixture'}), \
                 patch.object(temporal_video.vision_client,'post_chat') as call:
                with self.assertRaisesRegex(ValueError,'COLLIDES'):
                    temporal_video.review(p,'facts',p)
                call.assert_not_called();self.assertEqual(p.read_bytes(),b'fixture')


if __name__=='__main__':unittest.main()
