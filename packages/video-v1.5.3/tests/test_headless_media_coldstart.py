"""A new interpreter/package/profile must process real rotated media and 48k speech."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from test_runtime_resources import ROOT, source_files


COLD_MEDIA = r'''
import contextlib, io, json, os, pathlib, shutil, socket, subprocess, sys
from unittest.mock import patch
root = pathlib.Path.cwd().resolve()
sys.path.insert(0, str(root / 'doubao-jianying-orchestrator'))
def forbidden(*a, **k): raise AssertionError('cold media validation cannot call cloud services or TTS')
socket.socket.connect = forbidden
from orchestrator.application import headless_production as writer, recovery_audio
from orchestrator.application.engine import OrchestrationEngine
from orchestrator.audio import voice_tts
from orchestrator.media import source_normalization as media
from orchestrator.media.probe import probe_media
from orchestrator.quality.ai_editor_qc import source_matches_receipt
assert pathlib.Path(writer.__file__).resolve().is_relative_to(root)
ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
work = root.parent / 'media-work'; work.mkdir()
def run(*args):
    return subprocess.run([ffmpeg,'-y','-hide_banner','-loglevel','error',*map(str,args)],
        capture_output=True,text=True,check=True,timeout=60)
voice, plain, rotated = work/'voice.wav', work/'plain.mp4', work/'rotated.mp4'
run('-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-af','atrim=end_sample=85246','-c:a','pcm_s16le',voice)
run('-f','lavfi','-i','testsrc2=size=96x64:rate=25:duration=3','-c:v','libx264',plain)
run('-display_rotation','-90','-i',plain,'-c','copy',rotated)
before = media.identity(rotated)
timing = work/'timing.json'
timing.write_text(json.dumps({'rows':[{'key':'s1','text':'真实采样测试。','tts_path':str(voice),
    'audio_duration_us':1775958,'timing_certainty':'audio'}]}),encoding='utf-8')
manifest = recovery_audio.bind({'segments':[{'video':str(rotated),'text':'真实采样测试。',
    'duration':1.775958,'source_start':0}],'narration_timing_file':str(timing)})
engine = OrchestrationEngine(manifest,work,work/'report',capacity_guard=False)
engine.ffmpeg, engine.ffprobe, engine.total_us = ffmpeg, ffprobe, 1775958
clips = engine.build_clips()
from PIL import ImageFont
font_candidates = [pathlib.Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'/name
                   for name in ('msyhbd.ttc','simhei.ttf','arial.ttf')]
font_source = next((str(p) for p in font_candidates if p.is_file()), 'DejaVuSans.ttf')
font = work/'fixture.ttf'
shutil.copyfile(ImageFont.truetype(font_source,16).path,font)
os.environ['JY_HEADLESS_FONT'] = str(font)
library = work/'drafts'; library.mkdir()
os.environ['JY_DRAFTS_ROOT'] = str(library)
captured = {}
def build(path, output, drafts):
    plan = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    captured.update(plan)
    target = pathlib.Path(drafts)/plan['name']; target.mkdir()
    return {'draft_path':str(target)}
with patch.object(voice_tts,'generate_batch',side_effect=forbidden) as tts, \
     patch.object(writer,'native_backend') as backend, \
     patch.object(writer.draft_safety,'post_write_validate',return_value=[]), \
     patch.object(engine,'_invariant_gate',return_value=None), \
     patch.object(engine,'_draft_result',return_value={'status':'SUCCESS'}), \
     contextlib.redirect_stdout(io.StringIO()):
    backend.return_value.build.side_effect = build
    engine.synthesize(clips,None)
    result = writer.write_draft(engine,clips,str(voice))
    tts.assert_not_called()
assert result['status']=='SUCCESS', result
video = captured['tracks'][0]['segments'][0]
facts = probe_media(video['source'],ffprobe=ffprobe)
assert (facts['width'],facts['height'],media.rotation(facts)) == (64,96,0), facts
narration = next(t for t in captured['tracks'] if t['name']=='Narration')['segments'][0]
audio = probe_media(narration['source'],ffprobe=ffprobe)
stream = next(s for s in audio['streams'] if s['codec_type']=='audio')
assert stream['sample_rate']=='48000' and stream['codec_name']=='pcm_s16le'
assert audio['duration_us']==narration['duration_us']==1775958
draft = next(library.iterdir()); prepared = media.read_prepared_sources(draft)
assert source_matches_receipt(video['source'],before,prepared)
assert media.identity(rotated)==before
assert captured['tracks'][-1]['segments'][0]['text']=='真实采样测试'
print(json.dumps({'status':'COLD_MEDIA_READY','rate':48000,'duration_us':audio['duration_us'],
    'tts_calls':0,'native_backend':'mocked-in-ci','source_package':str(root)}))
'''


class ColdHeadlessMediaTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_tracked_or_unpacked_sources_with_empty_profile_process_real_media(self):
        mode, files = source_files()
        with tempfile.TemporaryDirectory(prefix="video-media-cold-") as raw:
            root = Path(raw).resolve() / "package"
            for relative in files:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
            profile = root.parent / "empty-profile"
            profile.mkdir()
            allowed = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATHEXT"}
            env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
            env.update({k: str(profile) for k in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")})
            env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1", JY_GPU_DISABLE="1")
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", COLD_MEDIA], cwd=root,
                env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
        self.assertEqual(result.returncode, 0, mode + "\n" + result.stdout + result.stderr)
        report = json.loads(result.stdout.splitlines()[-1])
        self.assertEqual((report["status"], report["rate"], report["tts_calls"]), ("COLD_MEDIA_READY", 48000, 0))
