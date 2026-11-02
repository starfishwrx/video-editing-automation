"""Default routing, fixed cuts and failure boundaries; no model or UI calls."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import shutil
import wave
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.application.engine import OrchestrationEngine
from orchestrator.application import headless_production
from orchestrator.audio import final_bgm
from orchestrator.audio.bgm_request import normalize
from orchestrator.domain.clips import Clip
from orchestrator.runtime import draft_documents


class DefaultHeadlessTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffprobe'), 'FFprobe required for real audio measurement')
    def test_narration_duration_does_not_inherit_video_one_frame_margin(self):
        from orchestrator.application import cli
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);audio=root/'voice.wav';samples=53615
            with wave.open(str(audio),'wb') as stream:
                stream.setparams((1,2,48000,0,'NONE','uncompressed'))
                stream.writeframes(b'\0\0'*samples)
            engine=OrchestrationEngine({},root,root/'report',capacity_guard=False)
            engine.ffprobe=shutil.which('ffprobe')
            engine.ffmpeg=shutil.which('ffmpeg')
            stat=audio.stat()
            # A real old conservative audio entry must not survive the new ruler.
            engine._probe_cache[str(audio.resolve())]={'duration':1.083625,
                'probe_kind':'material_us_v1',
                'signature':[str(audio.resolve()),stat.st_size,stat.st_mtime_ns,'material_us_v1']}
            cli._PROBE_CACHE.clear()
            self.addCleanup(cli._PROBE_CACHE.clear)
            with patch('orchestrator.runtime.platform_env.find_ffmpeg',return_value=(engine.ffmpeg,engine.ffprobe)):
                planned=cli._audio_probe()(str(audio))
            written=engine.probe(audio)
            expected=round(samples/48000*1_000_000)
            self.assertEqual(round(planned['duration']*1_000_000),expected)
            self.assertEqual(round(written['duration']*1_000_000),expected)
            self.assertEqual(written['duration_confidence'],'HIGH')

    def test_default_writer_cannot_report_success_without_bgm(self):
        with patch.object(headless_production, 'native_backend') as native:
            result=headless_production.write_draft(SimpleNamespace(),[],None)
        self.assertEqual(result['status'],'BGM_REQUIRED')
        self.assertIsNone(result['draft_path'])
        native.assert_not_called()

    def test_cli_checks_native_readiness_before_paid_stages(self):
        import argparse
        from orchestrator.application import cli
        from orchestrator.selection import sentence_planner
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {}, clear=True):
            root=Path(raw)
            manifest=root/'manifest.json'
            manifest.write_text(json.dumps({'task_id':'readiness-test','full_script':'test',
                'temporary_material_analysis':True,'selection_mode':'rule_then_ai'}),encoding='utf-8')
            args=argparse.Namespace(input=str(manifest),report_dir=str(root/'report'),
                                    trigger_phrase='',delivery_mode='formal')
            with patch.object(cli,'environment_report',return_value={'ready':True}), \
                    patch.object(cli,'_manifest_sources',return_value=[]), \
                    patch.object(sentence_planner,'plan') as plan, \
                    patch.object(cli,'_pre_synthesize_narration') as tts, \
                    patch.object(cli,'_temporary_match') as vision:
                self.assertEqual(cli.cmd_build(args),1)
                for paid in (plan,tts,vision):
                    paid.assert_not_called()
            saved=json.loads((root/'report/last_result.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['status'],'ENV_ERROR')
            self.assertEqual(saved['reason'],'HEADLESS_ROOT_REQUIRED')

    def test_default_route_is_headless_without_legacy_fallback(self):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = {}
        with patch.dict(os.environ, {}, clear=True), patch.object(headless_production, 'write_draft',
                return_value={'status': 'HEADLESS_BUILD_BLOCKED'}) as native, \
                patch.object(engine, '_write_legacy_draft') as legacy:
            self.assertEqual(engine.write_draft([], None)['status'], 'HEADLESS_BUILD_BLOCKED')
            native.assert_called_once_with(engine, [], None)
            legacy.assert_not_called()

    def test_empty_bgm_runs_automatically_without_music_preview_confirmation(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for bgm in (None, '', '  ', {}):
                engine = OrchestrationEngine({'voice': 'female_sweet', 'bgm': bgm,
                    'audio_selection_confirmed': True, 'full_script': 'test'}, root, root/'report', capacity_guard=False)
                engine.total_us, engine.ffmpeg, engine.ffprobe = 1_116_959, None, None
                with patch.object(final_bgm, 'prepare_task_bgm', return_value={'path': str(root/'music.wav'),
                    'measured_lufs':-28, 'selected':{'music_id':'selected'}}) as prepare, \
                    patch('orchestrator.application.engine.bgm_selector.prepare_preview_tracks') as preview:
                    self.assertIsNone(engine.audio_selection_gate([]))
                    engine.prepare_bgm()
                    first = prepare.call_args.kwargs['output_dir']
                    engine.prepare_bgm()
                    self.assertNotEqual(first, prepare.call_args.kwargs['output_dir'])
                    self.assertEqual(prepare.call_args.args[0], {'auto':True})
                    self.assertEqual(prepare.call_args.kwargs['duration_us'], 1_116_959)
                    preview.assert_not_called()

    def test_explicit_music_requests_are_preserved(self):
        for value, expected in [('song-id',{'music_id':'song-id'}), ('Official title',{'music_id':'Official title'}),
                (r'C:\music.wav',{'path':r'C:\music.wav'}), ({'path':'a.wav','start':2},{'path':'a.wav','start':2})]:
            self.assertEqual(normalize(value), expected)

    def test_native_read_failure_never_falls_back_to_old_plaintext(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root/'headless-production.json').write_text('{}')
            (root/'draft_info.json').write_text(json.dumps({'id':'stale-root','tracks':[]}))
            with patch.object(draft_documents, 'native_backend') as backend:
                backend.return_value.inspect.side_effect = ValueError('MIRROR_DRIFT')
                with self.assertRaisesRegex(ValueError, 'MIRROR_DRIFT'):
                    draft_documents.timeline(root)

    def test_plan_preserves_native_audio_brightness_slice_and_bgm(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); font=root/'font.ttf'; font.write_bytes(b'fixture')
            engine=SimpleNamespace(m={"brightness":None}, _fps=lambda:30, ffprobe=None, total_us=1_200_001,
                                   _audio_slice_us=lambda c:c.start_us)
            first=Clip(str(root/'video.mp4'),0,.2,text='测试。',audio=str(root/'voice.wav'),
                       audio_mode='file',parent_index=0,multi_shot=True)
            second=Clip(str(root/'video.mp4'),1,1.000001,text='测试。',audio=str(root/'voice.wav'),
                       audio_mode='file',video_speed=2,start_us=200_000,parent_index=0,multi_shot=True)
            with patch.object(headless_production,'probe_media',return_value={'duration_us':1_200_001}):
                plan,built,_=headless_production.make_plan(engine,[first,second],str(root/'bgm.wav'),{},
                    [{'text':'测试。','start_us':0,'end_us':1_200_001}], 'case',font)
            video=plan['tracks'][0]['segments'][1]
            self.assertEqual((video['source_start_us'],video['source_duration_us'],video['duration_us']),
                             (1_000_000,2_000_002,1_000_001))
            self.assertEqual(video['brightness'],.08)
            self.assertEqual(video['volume'],0)
            narration=next(t for t in plan['tracks'] if t['name']=='Narration')['segments'][1]
            self.assertEqual((narration['source_start_us'],narration['duration_us']),(200_000,1_000_001))
            bgm=next(t for t in plan['tracks'] if t['name']=='BGM')['segments'][0]
            self.assertEqual(bgm['duration_us'],1_200_001)
            self.assertEqual(plan['tracks'][-1]['segments'][0]['text'],'测试')
            self.assertTrue(plan['tracks'][-1]['segments'][0]['subtitle'])

    def test_one_frame_tolerance_cannot_silently_cut_twenty_ms_of_speech(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);font=root/'font.ttf';font.write_bytes(b'fixture')
            engine=SimpleNamespace(m={},_fps=lambda:30,ffprobe=None,total_us=1_000_000,
                                   _audio_slice_us=lambda c:0)
            clip=Clip(str(root/'video.mp4'),0,1,audio=str(root/'voice.wav'),audio_mode='file')
            with patch.object(headless_production,'probe_media',return_value={'duration_us':1_020_000}):
                with self.assertRaisesRegex(ValueError,'NARRATION_DURATION_CONFLICT'):
                    headless_production.make_plan(engine,[clip],str(root/'bgm.wav'),{},[],'case',font)

    def test_unknown_transition_is_blocked_without_dropping_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); font=root/'font.ttf'; font.write_bytes(b'fixture')
            engine=SimpleNamespace(m={},_fps=lambda:30,total_us=1_000_000)
            clip=Clip('video.mp4',0,1,transition={'type':'unknown','duration':.2})
            with self.assertRaisesRegex(ValueError,'TRANSITION_UNSUPPORTED'):
                headless_production.make_plan(engine,[clip],None,{},[],'case',font)


if __name__=='__main__':
    unittest.main()
