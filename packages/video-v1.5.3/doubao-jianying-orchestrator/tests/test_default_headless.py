"""Default routing, fixed cuts and failure boundaries; no model or UI calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import shutil
import wave
from PIL import ImageFont
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.media.caption_layout import layout_subtitle
from orchestrator.application.engine import OrchestrationEngine
from orchestrator.application import headless_production
from orchestrator.audio import final_bgm
from orchestrator.audio.bgm_request import normalize
from orchestrator.domain.clips import Clip


class EnvironmentCommandTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'real media tools required')
    def test_explicit_media_paths_are_honored_without_path_entries(self):
        from orchestrator.runtime import headless
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {
                'FFMPEG_BIN': ffmpeg, 'FFPROBE_BIN': ffprobe, 'PATH': raw}):
            selected = headless._prepare_media_tools()
            self.assertEqual(Path(selected['ffmpeg']).resolve(), Path(ffmpeg).resolve())
            self.assertEqual(Path(selected['ffprobe']).resolve(), Path(ffprobe).resolve())
            self.assertEqual(Path(shutil.which('ffmpeg')).resolve(), Path(ffmpeg).resolve())
            self.assertEqual(Path(shutil.which('ffprobe')).resolve(), Path(ffprobe).resolve())
            first_path = os.environ['PATH']
            for _ in range(5):
                headless._prepare_media_tools()
                self.assertEqual(os.environ['PATH'], first_path)

    def test_broken_explicit_media_path_is_not_silently_replaced(self):
        from orchestrator.runtime import headless
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {
                'FFMPEG_BIN': str(Path(raw) / 'missing.exe')}):
            with self.assertRaisesRegex(ValueError, 'HEADLESS_EXECUTABLE_CONFIG_INVALID:FFMPEG_BIN'):
                headless._prepare_media_tools()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'real media tools required')
    def test_relative_media_paths_are_frozen_before_child_directory_changes(self):
        from orchestrator.runtime import headless, platform_env
        ffmpeg, ffprobe = Path(shutil.which('ffmpeg')).resolve(), Path(shutil.which('ffprobe')).resolve()
        old_cwd = Path.cwd()
        try:
            os.chdir(ffmpeg.parent)
            try:
                relative_probe = os.path.relpath(ffprobe, ffmpeg.parent)
            except ValueError:
                relative_probe = str(ffprobe)
            with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {
                    'FFMPEG_BIN': ffmpeg.name, 'FFPROBE_BIN': relative_probe, 'PATH': raw}):
                headless._prepare_media_tools()
                try:
                    os.chdir(raw)
                    selected = platform_env.find_ffmpeg_detailed()
                    self.assertEqual(Path(selected['ffmpeg']).resolve(), ffmpeg)
                    self.assertEqual(Path(selected['ffprobe']).resolve(), ffprobe)
                finally:
                    os.chdir(ffmpeg.parent)
        finally:
            os.chdir(old_cwd)

    def test_build_retains_preview_and_reports_environment_override(self):
        from orchestrator.application import cli
        cases = [({}, [], 'preview'), ({'delivery_mode': 'preview'}, [], 'preview'),
                 ({'delivery_mode': 'preview'}, ['--delivery-mode', 'formal'], 'formal')]
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {
                'JY_SELECTION_MODE': 'legacy', 'JY_CONFIG_SOURCE': 'test-config.ps1'}):
            root = Path(raw)
            for index, (values, extra, expected) in enumerate(cases):
                manifest = root / ('manifest%d.json' % index)
                report = root / ('report%d' % index)
                manifest.write_text(json.dumps(values), encoding='utf-8')
                with patch.object(cli, 'environment_report', return_value={'ready': False}), \
                     patch.object(cli, '_manifest_sources', return_value=[]), \
                     patch.object(cli, '_pre_synthesize_narration') as paid, \
                     contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(io.StringIO()) as errors:
                    code = cli.main(['build', '--input', str(manifest), '--report-dir', str(report), *extra])
                self.assertEqual(code, 1)
                paid.assert_not_called()
                result = json.loads((report / 'last_result.json').read_text(encoding='utf-8'))
                self.assertEqual(result['runtime']['delivery_mode'], expected)
                self.assertEqual(result['runtime']['selection_mode'], 'legacy')
                self.assertEqual(result['runtime']['selection_mode_source'], 'env:JY_SELECTION_MODE')
                self.assertEqual(result['runtime']['configuration_source'], 'test-config.ps1')
                self.assertIn('legacy', errors.getvalue())

    def test_preview_example_has_real_parseable_sentences(self):
        from orchestrator.selection import timing_contract
        example = Path(__file__).resolve().parents[2] / 'examples/manifest.preview.json'
        manifest = json.loads(example.read_text(encoding='utf-8'))
        sentences = timing_contract.split_sentences(manifest)
        self.assertTrue(sentences)
        self.assertTrue(timing_contract.sentence_text(sentences[0]).strip())
        self.assertTrue(manifest['temporary_material_analysis'])
        self.assertEqual(manifest['delivery_mode'], 'preview')

    def test_recovery_rejects_a_formal_cache_for_a_preview_input(self):
        from orchestrator.application import cli
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / 'report'
            report.mkdir()
            original = {'delivery_mode': 'preview', 'segments': [{'text': 'test', 'video': 'a.mp4'}]}
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps(original), encoding='utf-8')
            (report / 'shot_match_report.json').write_text(json.dumps({
                'ok': True, 'input_manifest': original,
                'manifest': {'delivery_mode': 'formal', 'segments': original['segments']}}), encoding='utf-8')
            with patch.object(cli, 'OrchestrationEngine') as writer, \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(['recover', '--input', str(manifest), '--report-dir', str(report),
                                 '--stage', 'draft_write'])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(output.getvalue())['reason'], 'RECOVERY_DELIVERY_MODE_MISMATCH')
            writer.assert_not_called()

    def test_recovery_preflight_failure_preserves_prior_match_evidence(self):
        from orchestrator.application import cli
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            report = root / 'report'
            report.mkdir()
            original = {'delivery_mode': 'preview', 'segments': [{'text': 'test', 'video': 'a.mp4'}]}
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps(original), encoding='utf-8')
            previous = {'ok': True, 'input_manifest': original, 'evidence': {'shot': 'retained'}}
            (report / 'shot_match_report.json').write_text(json.dumps(previous), encoding='utf-8')
            (report / 'shot_candidates.json').write_text('{"shots": []}', encoding='utf-8')
            with patch.object(cli, 'environment_report', return_value={'ready': True}), \
                 patch.object(cli, '_production_environment_report', return_value={
                     'ready': False, 'reason': 'HEADLESS_ROOT_REQUIRED'}), \
                 patch.object(cli, '_temporary_match') as match, \
                 contextlib.redirect_stdout(io.StringIO()):
                code = cli.main(['recover', '--input', str(manifest), '--report-dir', str(report),
                                 '--stage', 'semantic_match'])
            self.assertEqual(code, 1)
            match.assert_not_called()
            self.assertFalse(json.loads((report / 'shot_match_report.json').read_text())['ok'])
            backups = list((report / 'match_history').glob('*.json'))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text()), previous)

    def test_default_env_rejects_a_missing_headless_backend(self):
        from orchestrator.application import cli
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(cli, 'environment_report', return_value={'ready': True}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(['env']), 1)
        report = json.loads(output.getvalue())
        self.assertFalse(report['ready'])
        self.assertTrue(report['base_ready'])
        self.assertEqual(report['draft_backend_check']['reason'], 'HEADLESS_ROOT_REQUIRED')
        self.assertIn('HEADLESS_ROOT_REQUIRED', report['hints'])

    def test_base_profile_is_explicit_and_retains_failure_exit_codes(self):
        from orchestrator.application import cli
        from orchestrator.runtime import headless
        for ready in (True, False):
            with self.subTest(ready=ready), \
                    patch.object(cli, 'environment_report', return_value={'ready': ready}), \
                    patch.object(headless, 'preflight') as native, \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['env', '--profile', 'base']), 0 if ready else 1)
            self.assertEqual(json.loads(output.getvalue())['profile'], 'base')
            native.assert_not_called()

    def test_manifest_backend_override_and_new_configuration_are_rechecked(self):
        from orchestrator.application import cli
        from orchestrator.runtime import headless
        with tempfile.TemporaryDirectory() as raw, patch.dict(os.environ, {'ARK_API_KEY': 'test-key'}, clear=True), \
                patch.object(cli, 'environment_report', return_value={'ready': True}), \
                patch.object(headless, '_prepare_media_tools', return_value={}):
            manifest = Path(raw) / 'manifest.json'
            manifest.write_text(json.dumps({'draft_backend': 'legacy'}), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['env', '--input', str(manifest)]), 0)
            self.assertEqual(json.loads(output.getvalue())['draft_backend_check']['backend'], 'legacy')
            manifest.write_text(json.dumps({'draft_backend': 'headless'}), encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(cli.main(['env', '--input', str(manifest)]), 1)
            self.assertFalse(json.loads(output.getvalue())['ready'])

    def test_font_failure_and_repair_do_not_pollute_or_cache_the_base_report(self):
        from orchestrator.application import cli
        from orchestrator.runtime import headless
        base = {'ready': True, 'hints': ['base hint']}
        with patch.dict(os.environ, {'ARK_API_KEY': 'test-key', 'JY_VISION_TRANSPORT': 'glm', 'JY_AI_EDITOR_QWEN': ''}), \
                patch.object(cli, 'environment_report', return_value=base), \
                patch.object(headless, 'preflight', side_effect=[
                    ValueError('HEADLESS_SUBTITLE_FONT_REQUIRED'), {'status': 'READY', 'backend': 'headless'}]):
            for expected in (False, True):
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(['env']), 0 if expected else 1)
                self.assertEqual(json.loads(output.getvalue())['ready'], expected)
        self.assertEqual(base, {'ready': True, 'hints': ['base hint']})
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

    def test_only_known_media_incompatibility_falls_back_once(self):
        engine = OrchestrationEngine.__new__(OrchestrationEngine)
        engine.m = {"draft_name": "offline"}
        clips = []
        with patch.dict(os.environ, {}, clear=True), patch.object(headless_production, 'write_draft',
                return_value={'status': 'HEADLESS_BUILD_BLOCKED', 'reason': 'SOURCE_ROTATION_HDR_METADATA_UNSUPPORTED'}), \
                patch.object(engine, '_write_legacy_draft', return_value={'status': 'SUCCESS'}) as legacy:
            result = engine.write_draft(clips, 'music.wav')
            self.assertEqual(result['draft_backend'], 'legacy')
            self.assertEqual(result['fallback_reason'], 'SOURCE_ROTATION_HDR_METADATA_UNSUPPORTED')
            legacy.assert_called_once_with(clips, 'music.wav')
            self.assertEqual(engine.m['draft_name'], 'offline')
        for reason in ('FILE_NOT_FOUND', 'DISK_FULL', 'TASK_LOCKED', 'QC_FAILED'):
            with patch.dict(os.environ, {}, clear=True), patch.object(headless_production, 'write_draft',
                    return_value={'status': 'HEADLESS_BUILD_BLOCKED', 'reason': reason}), \
                    patch.object(engine, '_write_legacy_draft') as legacy:
                self.assertEqual(engine.write_draft(clips, 'music.wav')['reason'], reason)
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

    def test_plaintext_desktop_timeline_is_read_and_encrypted_index_is_not(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'draft_info.json').write_text(json.dumps({'id': 'stale-root', 'tracks': []}), encoding='utf-8')
            timeline = root / 'Timelines' / 'LINE1'
            timeline.mkdir(parents=True)
            (root / 'Timelines' / 'project.json').write_text(json.dumps({'main_timeline_id': 'LINE1'}), encoding='utf-8')
            (timeline / 'draft_content.json').write_text(json.dumps({'id': 'LINE1', 'tracks': []}), encoding='utf-8')
            self.assertEqual(draft_documents.timeline(root)['id'], 'LINE1')
            (timeline / 'draft_content.json').write_bytes(b'\x00not-json')
            with self.assertRaisesRegex(ValueError, 'UNOWNED_NATIVE_DRAFT_UNSUPPORTED'):
                draft_documents.timeline(root)

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
            root=Path(raw); font=root/'font.ttf'; shutil.copyfile(ImageFont.truetype(layout_subtitle('x',1080,1920,font_name='Arial')['font_path'],12).path,font)
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
            root=Path(raw);font=root/'font.ttf';shutil.copyfile(ImageFont.truetype(layout_subtitle('x',1080,1920,font_name='Arial')['font_path'],12).path,font)
            engine=SimpleNamespace(m={},_fps=lambda:30,ffprobe=None,total_us=1_000_000,
                                   _audio_slice_us=lambda c:0)
            clip=Clip(str(root/'video.mp4'),0,1,audio=str(root/'voice.wav'),audio_mode='file')
            with patch.object(headless_production,'probe_media',return_value={'duration_us':1_020_000}):
                with self.assertRaisesRegex(ValueError,'NARRATION_DURATION_CONFLICT'):
                    headless_production.make_plan(engine,[clip],str(root/'bgm.wav'),{},[],'case',font)

    def test_unknown_transition_is_blocked_without_dropping_it(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); font=root/'font.ttf'; shutil.copyfile(ImageFont.truetype(layout_subtitle('x',1080,1920,font_name='Arial')['font_path'],12).path,font)
            engine=SimpleNamespace(m={},_fps=lambda:30,total_us=1_000_000)
            clip=Clip('video.mp4',0,1,transition={'type':'unknown','duration':.2})
            with self.assertRaisesRegex(ValueError,'TRANSITION_UNSUPPORTED'):
                headless_production.make_plan(engine,[clip],None,{},[],'case',font)


if __name__=='__main__':
    unittest.main()
