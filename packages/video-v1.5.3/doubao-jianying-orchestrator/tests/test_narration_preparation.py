"""Real PCM/media and entrypoint checks for the ten-frame paper narration gate."""
import concurrent.futures
import json
import math
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.audio import narration_preparation as prep, voice_tts
from orchestrator.policy.production_parameters import silence_edges, gap_errors
from orchestrator import cli, engine, task_log
from orchestrator.application import recovery_audio
from orchestrator.runtime.file_safety import content_identity
from orchestrator.runtime.platform_env import find_ffmpeg


def make_wave(path, *, head=.12, tail=.14, speech=.6, amplitude=6000, middle_gap=0., late_voice=0., stereo_opposed=False):
    def tone(duration):
        t = np.arange(round(duration * 48000)) / 48000
        return np.round(amplitude * np.sin(2 * math.pi * 440 * t)).astype('<i2')
    mono = np.concatenate([np.zeros(round(head*48000),dtype='<i2'), tone(speech),
                           np.zeros(round(middle_gap*48000),dtype='<i2'), tone(late_voice),
                           np.zeros(round(tail*48000),dtype='<i2')])
    pcm = np.column_stack([mono, -mono if stereo_opposed else mono]).astype('<i2')
    with wave.open(str(path),'wb') as stream:
        stream.setparams((2,2,48000,0,'NONE','not compressed')); stream.writeframes(pcm.tobytes())
    return pcm


class NarrationBoundaryUnitTests(unittest.TestCase):
    def test_detector_does_not_merge_internal_pause_with_last_30ms_of_speech(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'a.wav';p.write_bytes(b'x')
            proc=SimpleNamespace(returncode=0,stdout='out_time_us=1000000',
                stderr='silence_start: 0.8\nsilence_end: 0.97')
            with patch('orchestrator.policy.production_parameters.subprocess.run',return_value=proc):
                self.assertEqual(silence_edges(str(p),start_s=0,duration_s=1,ffmpeg='ffmpeg')['trailing_s'],0)
            proc.stderr='silence_start: 0.003\nsilence_end: 0.2'
            with patch('orchestrator.policy.production_parameters.subprocess.run',return_value=proc):
                self.assertEqual(silence_edges(str(p),start_s=0,duration_s=1,ffmpeg='ffmpeg')['leading_s'],0)

    def test_five_frames_are_accepted_but_longer_gap_and_timeline_holes_block(self):
        rows=[dict(sentence=1,start_s=0,end_s=1,trailing_s=1/12,leading_s=0),
              dict(sentence=2,start_s=1,end_s=2,trailing_s=0,leading_s=1/12)]
        self.assertFalse(gap_errors(rows,fps=30,max_frames=5))
        rows[1]['start_s']+=.001
        self.assertTrue(gap_errors(rows,fps=30,max_frames=5))

    def test_failure_contracts_are_explicit_and_do_not_retry_whole_task(self):
        for status,kind,action in [('NARRATION_GAP_EXCEEDED','timing','replan_narration'),
                                    ('NARRATION_AUDIO_UNVERIFIED','timing','replan_narration'),
                                    ('NARRATION_GAP_UNVERIFIED','environment','fix_environment'),
                                    ('NARRATION_PREPARATION_FAILED','environment','fix_environment')]:
            with self.subTest(status=status):
                r=task_log.result_contract({'status':status},task_id='task',run_id='run',task_log='log')
                self.assertEqual((r['terminal_state'],r['failure_class'],r['next_action']),('BLOCKED',kind,action))
                self.assertFalse(r['retryable']);self.assertFalse(r['safe_to_continue'])

    def test_paper_gate_rejects_empty_missing_silent_and_real_gap(self):
        with tempfile.TemporaryDirectory() as td:
            e=engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
            e.m={'category':'zhipin'};e.report_dir=Path(td);e.ffmpeg='fake'
            self.assertEqual(e._narration_gap_gate([])['status'],'NARRATION_GAP_UNVERIFIED')
            c=engine.Clip('v',0,1,text='voice',audio_mode='tts')
            self.assertEqual(e._narration_gap_gate([c])['status'],'NARRATION_GAP_UNVERIFIED')
            c.tts_path='voice.wav';c.raw_index=0
            d=engine.Clip('v',0,1,audio_mode='tts',tts_path='other.wav',raw_index=1,start_us=1000000)
            with patch('orchestrator.policy.production_parameters.silence_edges',return_value={
                    'leading_s':.1,'trailing_s':.1,'all_silent':False}):
                self.assertIsNone(e._narration_gap_gate([c,d]))
            with patch('orchestrator.policy.production_parameters.silence_edges',return_value={
                    'leading_s':.2,'trailing_s':.2,'all_silent':False}):
                self.assertEqual(e._narration_gap_gate([c,d])['status'],'NARRATION_GAP_EXCEEDED')
            with patch('orchestrator.policy.production_parameters.silence_edges',return_value={'all_silent':True}):
                self.assertEqual(e._narration_gap_gate([c])['status'],'NARRATION_AUDIO_UNVERIFIED')
            e.m={'category':'weipin'};self.assertIsNone(e._narration_gap_gate([c,d]))

    def test_derivative_identity_distinguishes_exact_tempo_threshold_and_source_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'raw.ogg';p.write_bytes(b'first')
            a=voice_tts._post_name(p,1.201,25,True)
            self.assertNotEqual(a,voice_tts._post_name(p,1.204,25,True))
            with patch.object(voice_tts,'VOICE_SILENCE_THRESHOLD_DB',-40):
                self.assertNotEqual(a,voice_tts._post_name(p,1.201,25,True))
            p.write_bytes(b'other');self.assertNotEqual(a,voice_tts._post_name(p,1.201,25,True))

    def test_invalid_frame_clock_cannot_bypass_gate(self):
        for fps in [0,-1,float('nan'),float('inf')]:
            with self.subTest(fps=fps),self.assertRaises(ValueError):gap_errors([],fps=fps,max_frames=5)

    def test_zero_start_rendering_requires_explicit_duration_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'a.wav';p.write_bytes(b'x')
            proc=SimpleNamespace(returncode=0,stdout='out_time_us=1000000',
                stderr='silence_start: invalid param\nsilence_end: 0.2 | silence_duration: 0.2')
            with patch('orchestrator.policy.production_parameters.subprocess.run',return_value=proc):
                self.assertEqual(silence_edges(str(p),start_s=0,duration_s=1,ffmpeg='ffmpeg')['leading_s'],.2)
            proc.stderr='silence_start: invalid param'
            with patch('orchestrator.policy.production_parameters.subprocess.run',return_value=proc),self.assertRaises(ValueError):
                silence_edges(str(p),start_s=0,duration_s=1,ffmpeg='ffmpeg')

    def test_paper_batch_defers_all_edge_trimming_until_after_tempo_and_gain(self):
        def quiet(coro):coro.close();return {'s1':'retained.ogg'}
        with patch.object(voice_tts,'_quiet_async_run',side_effect=quiet), \
                patch.object(voice_tts,'_postprocess_all',return_value={'s1':'tempo.ogg'}) as post, \
                patch.object(prep,'prepare',return_value={'path':'final.wav'}) as final:
            _,paths=voice_tts.generate_batch([{'key':'s1','text':'原句'}],Path('unused'),
                voice_spec='真人播客女',tempo=1.2,final_narration={})
        self.assertFalse(post.call_args.kwargs['edge_trim'])
        self.assertTrue(final.call_args.kwargs['edge_trim'])
        self.assertEqual(post.call_args.kwargs['tempo'],1.2)
        self.assertEqual(paths,{'s1':'final.wav'})


class NarrationMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg,cls.ffprobe=find_ffmpeg()[:2]
        if not cls.ffmpeg or not cls.ffprobe:
            raise unittest.SkipTest('FFmpeg/ffprobe required for real narration media checks')

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def prepare(self,source,**kwargs):
        return prep.prepare(source,self.root/'final',ffmpeg=str(self.ffmpeg),ffprobe=str(self.ffprobe),**kwargs)

    def test_edge_repair_is_lossless_keeps_all_active_samples_and_source_unchanged(self):
        src=self.root/'source.wav';pcm=make_wave(src,stereo_opposed=True);before=content_identity(src)
        r=self.prepare(src,loudness={'enabled':False})
        with wave.open(r['path'],'rb') as stream:final=stream.readframes(stream.getnframes())
        lo,hi=r['trim_start_samples'],len(pcm)-r['trim_end_samples']
        self.assertEqual(final,pcm[lo:hi].tobytes())
        self.assertLess(np.max(np.abs(pcm[:lo].astype(np.int32))),328)
        self.assertLess(np.max(np.abs(pcm[hi:].astype(np.int32))),328)
        self.assertEqual(content_identity(src),before)
        self.assertLessEqual(r['after']['leading_s'],.026)
        self.assertLessEqual(r['after']['trailing_s'],.026)
        self.assertEqual(r['repair_attempts'],1)

    def test_internal_pause_and_last_voiced_tail_are_retained(self):
        src=self.root/'tail.wav';make_wave(src,head=.03,tail=0,speech=.5,middle_gap=.15,late_voice=.03)
        r=self.prepare(src,loudness={'enabled':False})
        self.assertEqual(r['trim_end_samples'],0)
        self.assertGreater(r['after']['duration_us'],670000)
        edges=silence_edges(r['path'],start_s=0,duration_s=r['duration_us']/1e6,ffmpeg=str(self.ffmpeg))
        self.assertLess(edges['trailing_s'],.001)

    def test_all_silence_overtrim_and_truncated_pcm_are_blocked(self):
        src=self.root/'silent.wav';make_wave(src,amplitude=0)
        with self.assertRaisesRegex(prep.NarrationError,'ALL_SILENT'):self.prepare(src,loudness={'enabled':False})
        src=self.root/'long.wav';make_wave(src,head=.9,tail=.9,speech=.2)
        with self.assertRaisesRegex(prep.NarrationError,'REPAIR_UNSAFE'):self.prepare(src,loudness={'enabled':False})
        src=self.root/'truncated.wav';make_wave(src);src.write_bytes(src.read_bytes()[:-100])
        with self.assertRaisesRegex(prep.NarrationError,'TRUNCATED'):prep.pcm_edges(src)

    def test_disabled_trim_is_respected_and_preflight_still_blocks_real_gap(self):
        src=self.root/'pad.wav';make_wave(src)
        r=self.prepare(src,loudness={'enabled':False},edge_trim=False)
        self.assertEqual(r['trim_start_samples']+r['trim_end_samples'],0)
        rows=[{'index':i,'tts_path':r['path'],'audio_duration_us':r['duration_us'],
               'timing_certainty':'audio'}for i in [1,2]]
        with self.assertRaises(prep.NarrationError) as error:
            prep.preflight({'rows':rows},self.root,fps=30,max_frames=5,ffmpeg=str(self.ffmpeg))
        self.assertEqual(error.exception.status,'NARRATION_GAP_EXCEEDED')

    def test_cache_is_idempotent_and_repairs_corrupt_derived_assets(self):
        src=self.root/'source.wav';make_wave(src)
        r=self.prepare(src,loudness={'enabled':False});p=Path(r['path']);before=content_identity(p)
        self.assertEqual(self.prepare(src,loudness={'enabled':False})['output'],r['output'])
        self.assertEqual(self.prepare(p,loudness={'enabled':False})['output'],r['output'])
        self.assertEqual(content_identity(p),before)
        p.write_bytes(b'corrupt')
        with self.assertRaises(prep.NarrationError):prep.prepared(p,prep.settings({'enabled':False}))
        fixed=self.prepare(src,loudness={'enabled':False});self.assertEqual(fixed['output']['sha256'],before['sha256'])
        self.assertNotEqual(self.prepare(src,loudness={'enabled':False},edge_trim=False)['path'],r['path'])

    def test_parallel_same_asset_publishes_one_valid_cache(self):
        src=self.root/'source.wav';make_wave(src)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            rows=list(pool.map(lambda _:self.prepare(src,loudness={'enabled':False}),range(2)))
        self.assertEqual(rows[0]['output'],rows[1]['output'])
        self.assertIsNotNone(prep.prepared(rows[0]['path'],prep.settings({'enabled':False})))

    def test_final_gain_and_duration_are_reused_by_writer_without_second_normalization(self):
        src=self.root/'source.wav';make_wave(src)
        r=self.prepare(src)
        e=engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
        e.m={'category':'zhipin'};e.report_dir=self.root;e.ffmpeg=self.ffmpeg;e.ffprobe=self.ffprobe
        c=engine.Clip('v',0,r['duration_us']/1e6,text='voice',tts_path=r['path'])
        with patch.object(engine.audio_loudness,'normalize_narration',side_effect=AssertionError('double normalization')):
            self.assertEqual(e._normalize_narrations([c]),{r['path']:r['path']})
        self.assertEqual(content_identity(r['path']),r['output'])

    def test_pre_synthesis_measures_final_audio_and_checks_it_before_matching(self):
        src=self.root/'source.wav';make_wave(src)
        r=self.prepare(src)
        manifest={'category':'zhipin','voice':'真人播客女1.2倍速','voice_tempo':1.2,
                  'segments':[{'text':'第一句'},{'text':'第二句'}]}
        with patch.object(cli.voice_tts,'generate_batch',return_value=(None,{'s1':r['path'],'s2':r['path']})) as gen:
            result=cli._pre_synthesize_narration(manifest,self.root,None)
        self.assertIn('final_narration',gen.call_args.kwargs)
        self.assertEqual([x['audio_duration_us'] for x in result['rows']],[r['duration_us']]*2)
        self.assertEqual(json.loads((self.root/'narration_preflight_report.json').read_text())['status'],'PASS')

    def test_unavailable_paper_tts_blocks_instead_of_estimated_selection(self):
        with patch.object(cli.voice_tts,'generate_batch',side_effect=RuntimeError('unavailable')):
            with self.assertRaisesRegex(prep.NarrationError,'PREPARATION_FAILED'):
                cli._pre_synthesize_narration({'category':'zhipin','script':'第一句。'},self.root,None)

    def test_recovery_binds_exact_frozen_audio_and_never_calls_tts_or_edge_repair(self):
        src=self.root/'frozen.wav';make_wave(src);identity=content_identity(src)
        rows=[{'key':'s1','index':1,'text':'原句','tts_path':str(src),'audio_duration_us':860000,'timing_certainty':'audio'}]
        timing=self.root/'timing.json';timing.write_text(json.dumps({'rows':rows}),encoding='utf-8')
        manifest={'category':'zhipin','narration_timing_file':str(timing),'segments':[{'text':'原句'}]}
        with patch.object(voice_tts,'generate_batch',side_effect=AssertionError('new TTS')), \
                patch.object(prep,'prepare',side_effect=AssertionError('changed frozen length')):
            bound=recovery_audio.bind(manifest)
        self.assertEqual(bound['segments'][0]['audio'],str(src.resolve()))
        self.assertEqual(bound['segments'][0]['audio_duration_us'],860000)
        self.assertEqual(content_identity(src),identity)

    def test_retained_tts_preflight_and_writer_use_same_fixed_length_final_file(self):
        src=self.root/'retained.wav';make_wave(src,head=.03,tail=.03);before=content_identity(src)
        manifest={'category':'zhipin','retained_tts_audio':[str(src)],
                  'segments':[{'text':'原句','audio':str(src),'audio_mode':'file'}]}
        with patch.object(voice_tts,'generate_batch',side_effect=AssertionError('new synthesis')):
            timing=cli._pre_synthesize_narration(manifest,self.root,None)
        final=timing['rows'][0]['tts_path']
        record=manifest['_paper_fixed_narrations'][str(src.resolve())]
        self.assertEqual(record['trim_start_samples']+record['trim_end_samples'],0)
        self.assertEqual(record['duration_us'],660000)
        e=engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
        e.m=manifest;e.report_dir=self.root;e.ffmpeg=self.ffmpeg;e.ffprobe=self.ffprobe
        c=engine.Clip('v',0,.66,audio_mode='file',audio=str(src),text='原句')
        with patch.object(engine.audio_loudness,'normalize_narration',side_effect=AssertionError('different audio')):
            self.assertEqual(e._normalize_narrations([c])[str(src)],final)
        self.assertEqual(before,content_identity(src))

    def test_probe_environment_failure_is_not_reported_as_replan_narration(self):
        src=self.root/'valid.wav';make_wave(src)
        timing={'rows':[{'index':1,'tts_path':str(src),'audio_duration_us':860000,'timing_certainty':'audio'}]}
        with self.assertRaises(prep.NarrationError) as error:
            prep.preflight(timing,self.root,fps=30,max_frames=5,ffmpeg=str(self.root/'missing-ffmpeg'))
        result=task_log.result_contract(error.exception.result(),task_id='t',run_id='r',task_log='log')
        self.assertEqual(result['status'],'NARRATION_GAP_UNVERIFIED')
        self.assertEqual(result['next_action'],'fix_environment')

    def test_writer_rejects_audio_changed_after_final_gate(self):
        src=self.root/'source.wav';make_wave(src)
        e=engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
        e._paper_audio_overrides={str(src):str(src)}
        e._paper_audio_signatures={str(src):content_identity(src)}
        src.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'CHANGED_AFTER_GAP_GATE'):e._normalize_narrations([])

    def test_ffprobe_failure_chain_stops_before_selection_with_environment_action(self):
        src=self.root/'source.wav';make_wave(src)
        manifest={'category':'zhipin','segments':[{'text':'原句','audio':str(src),'audio_mode':'file'}]}
        def unavailable(_):raise FileNotFoundError('ffprobe unavailable')
        with patch.object(cli,'_audio_probe',return_value=unavailable):
            with self.assertRaises(prep.NarrationError) as error:
                cli._pre_synthesize_narration(manifest,self.root,None)
        r=task_log.result_contract(error.exception.result(),task_id='t',run_id='r',task_log='log')
        self.assertEqual((r['status'],r['failure_class'],r['next_action']),
                         ('NARRATION_GAP_UNVERIFIED','environment','fix_environment'))
        self.assertFalse(r['safe_to_continue'])

    def test_retained_prepared_audio_reuses_saved_edge_policy_without_processing(self):
        src=self.root/'source.wav';make_wave(src)
        record=self.prepare(src)
        final=record['path'];identity=content_identity(final)
        manifest={'category':'zhipin','retained_tts_audio':[final],
                  'segments':[{'text':'原句','audio':final,'audio_mode':'file'}]}
        with patch.object(engine.audio_loudness,'normalize_narration',side_effect=AssertionError('repeat gain')):
            timing=cli._pre_synthesize_narration(manifest,self.root,None)
        self.assertEqual(timing['rows'][0]['tts_path'],final)
        self.assertEqual(content_identity(final),identity)
        e=engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
        e.m=manifest;e.report_dir=self.root;e.ffmpeg=self.ffmpeg;e.ffprobe=self.ffprobe
        c=engine.Clip('v',0,record['duration_us']/1e6,text='原句',audio_mode='file',audio=final)
        with patch.object(engine.audio_loudness,'normalize_narration',side_effect=AssertionError('repeat gain')):
            self.assertEqual(e._normalize_narrations([c]),{final:final})

    def test_retained_audio_with_gain_disabled_preserves_original_bytes_and_length(self):
        src=self.root/'source.wav';make_wave(src,head=.03,tail=.03)
        identity=content_identity(src)
        m={'category':'zhipin','narration_loudness':{'enabled':False},'retained_tts_audio':[str(src)],
           'segments':[{'text':'原句','audio':str(src),'audio_mode':'file'}]}
        with patch.object(prep,'prepare',side_effect=AssertionError('processed frozen audio')):
            timing=cli._pre_synthesize_narration(m,self.root,None)
        self.assertEqual(timing['rows'][0]['tts_path'],str(src))
        self.assertEqual(timing['rows'][0]['audio_duration_us'],660000)
        self.assertEqual(content_identity(src),identity)

    def test_engine_uses_frozen_audio_for_all_parent_slices_without_tts(self):
        src=self.root/'frozen.wav';make_wave(src,head=.03,tail=.03)
        timing=self.root/'timing.json';timing.write_text(json.dumps({'rows':[
            {'key':'s1','index':1,'text':'原句','tts_path':str(src),
             'audio_duration_us':660000,'timing_certainty':'audio'}]}),encoding='utf-8')
        e=engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
        e.logger=None;e.m={'category':'zhipin','narration_timing_file':str(timing),'segments':[{'text':'原句'}]}
        clips=[engine.Clip('v',0,.33,text='原句',raw_index=i,parent_index=0)for i in range(2)]
        with patch.object(voice_tts,'generate_batch',side_effect=AssertionError('recomputed frozen TTS')):
            e.synthesize(clips,None)
        self.assertEqual([c.audio for c in clips],[str(src.resolve())]*2)
        self.assertTrue(all(c.audio_mode=='file' and c.tts_path is None for c in clips))

    def test_direct_engine_tts_environment_failure_has_explicit_status(self):
        e=engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
        e.m={'category':'zhipin'};e.report_dir=self.root;e.profile=SimpleNamespace(tts_concurrency=1)
        with patch.object(voice_tts,'generate_batch',side_effect=RuntimeError('ffmpeg missing')):
            with self.assertRaises(prep.NarrationError) as error:
                e.synthesize([engine.Clip('v',0,1,text='原句')],None)
        self.assertEqual(error.exception.status,'NARRATION_PREPARATION_FAILED')

    def test_silent_subslice_of_voiced_sentence_is_not_an_all_silent_sentence(self):
        src=self.root/'split.wav';make_wave(src,head=.1,tail=0,speech=.5)
        e=engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
        e.m={'category':'zhipin'};e.report_dir=self.root;e.ffmpeg=self.ffmpeg
        clips=[engine.Clip('v',0,.1,text='原句',raw_index=0,parent_index=0,audio_mode='file',audio=str(src)),
               engine.Clip('v',0,.5,text='原句',raw_index=1,parent_index=0,audio_mode='file',audio=str(src),start_us=100000)]
        for clip in clips:
            clip.multi_shot=True
            clip.audio_clip_start_us=0
        self.assertIsNone(e._narration_gap_gate(clips))
        report=json.loads((self.root/'narration_gap_report.json').read_text())
        self.assertTrue(report['rows'][0]['all_silent'])
        self.assertFalse(report['rows'][1]['all_silent'])
        self.assertEqual([row['sentence'] for row in report['rows']],[0,0])

    def _split_boundary(self,side):
        a=self.root/'a.wav';b=self.root/'b.wav'
        make_wave(a,head=0,tail=.40 if side=='tail' else 0,speech=.4)
        make_wave(b,head=.40 if side=='head' else 0,tail=0,speech=.4)
        e=engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
        e.m={'category':'zhipin'};e.report_dir=self.root;e.ffmpeg=self.ffmpeg
        if side=='tail':
            cuts=[(a,0,0,.50,0),(a,0,.50,.30,.50),(b,1,.80,.4,0)]
        else:
            cuts=[(a,0,0,.4,0),(b,1,.4,.30,0),(b,1,.70,.50,.30)]
        clips=[]
        for index,(path,parent,start,duration,offset) in enumerate(cuts):
            c=engine.Clip('v',0,duration,text='A' if parent==0 else 'B',audio_mode='file',audio=str(path),
                          raw_index=index,parent_index=parent,start_us=round(start*1e6))
            c.multi_shot=True
            c.audio_clip_start_us=round((start-offset)*1e6)
            clips.append(c)
        result=e._narration_gap_gate(clips)
        self.assertEqual(result['status'],'NARRATION_GAP_EXCEEDED')
        self.assertGreater(result['narration_gap']['issues'][0]['gap_frames'],10)
        self.assertEqual(result['narration_gap']['max_gap_frames'],10)

    def test_tail_silence_across_multiple_sentence_slices_still_blocks(self):
        self._split_boundary('tail')

    def test_head_silence_across_multiple_sentence_slices_still_blocks(self):
        self._split_boundary('head')


if __name__=='__main__':unittest.main()
