"""Bounded media work preserves requested evidence, including real VFR input."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import shot_analyzer, ai_editor, material_cache, platform_env


class BoundedBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {"JY_FRAME_BATCH": "1", "JY_GPU_DISABLE": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_late_seek_restores_source_time_and_reuses_duplicate(self):
        requests = [(90.2, self.root / 'a.jpg'), (90.8, self.root / 'b.jpg'),
                    (90.2, self.root / 'c.jpg')]
        def run(cmd, **kw):
            self.assertLess(cmd.index('-ss'), cmd.index('-i'))
            seek = float(cmd[cmd.index('-ss') + 1])
            self.assertAlmostEqual(seek, 89.95)
            self.assertLess(float(cmd[cmd.index('-t') + 1]), 2)
            self.assertEqual(cmd[cmd.index('-frames:v') + 1], '2')
            work = Path(cmd[-1]).parent
            (work / 'f_0001.jpg').write_bytes(b'a')
            (work / 'f_0002.jpg').write_bytes(b'b')
            return subprocess.CompletedProcess(cmd, 0, '', 'pts_time:0.25\npts_time:0.85\n')
        with mock.patch.object(shot_analyzer, 'run_ffmpeg_decode', side_effect=run), \
             mock.patch.object(shot_analyzer, 'extract_frame') as single:
            self.assertEqual(shot_analyzer.extract_frames_batch(Path('video'), requests), [True]*3)
            single.assert_not_called()
        self.assertEqual(requests[0][1].read_bytes(), requests[2][1].read_bytes())
        self.assertFalse(list(self.root.glob('.batch_*')))

    def test_earlier_or_missing_frame_falls_back_to_exact_request(self):
        requests = [(8.0, self.root / 'a.jpg'), (8.2, self.root / 'b.jpg')]
        def run(cmd, **kw):
            (Path(cmd[-1]).parent / 'f_0001.jpg').write_bytes(b'earlier')
            return subprocess.CompletedProcess(cmd, 0, '', 'pts_time:0.20\n')
        with mock.patch.object(shot_analyzer, 'run_ffmpeg_decode', side_effect=run), \
             mock.patch.object(shot_analyzer, 'extract_frame', return_value=True) as single:
            self.assertEqual(shot_analyzer.extract_frames_batch(Path('video'), requests), [True, True])
            self.assertEqual([c.args[1] for c in single.call_args_list], [8.0, 8.2])

    def test_missing_encoded_frame_does_not_shift_evidence_timestamps(self):
        requests = [(4.0, self.root / 'a.jpg'), (4.2, self.root / 'b.jpg')]
        def run(cmd, **kw):
            (Path(cmd[-1]).parent / 'f_0002.jpg').write_bytes(b'second')
            return subprocess.CompletedProcess(cmd, 1, '', 'pts_time:0.25\npts_time:0.45\n')
        with mock.patch.object(shot_analyzer, 'run_ffmpeg_decode', side_effect=run), \
             mock.patch.object(shot_analyzer, 'extract_frame', return_value=True) as single:
            self.assertEqual(shot_analyzer.extract_frames_batch(Path('video'), requests), [True, True])
            self.assertEqual(single.call_count, 2)

    def test_timeout_fallback_cleans_temporary_outputs(self):
        requests = [(4.0, self.root / 'a.jpg'), (4.2, self.root / 'b.jpg')]
        def run(cmd, **kw):
            (Path(cmd[-1]).parent / 'f_0001.jpg').write_bytes(b'partial')
            raise subprocess.TimeoutExpired(cmd, 1)
        with mock.patch.object(shot_analyzer, 'run_ffmpeg_decode', side_effect=run), \
             mock.patch.object(shot_analyzer, 'extract_frame', return_value=False):
            self.assertEqual(shot_analyzer.extract_frames_batch(Path('video'), requests), [False, False])
        self.assertFalse(list(self.root.glob('.batch_*')))

    def test_real_cfr_and_vfr_pixels_match_single_seek(self):
        for vfr, offset in [(False, 0), (True, 0), (True, 5)]:
            video = self.root / f'clip_{vfr}_{offset}.mp4'
            cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                   '-i', 'testsrc2=s=96x64:r=25:d=12']
            if vfr:
                cmd += ['-vf', "select='not(mod(n,3))'", '-fps_mode', 'vfr']
            cmd += ['-c:v', 'libx264', '-g', '100', '-pix_fmt', 'yuv420p',
                    '-output_ts_offset', str(offset), str(video)]
            try:
                subprocess.run(cmd, check=True, capture_output=True)
            except (OSError, subprocess.SubprocessError) as exc:
                self.skipTest(f'synthetic ffmpeg unavailable: {exc}')
            for label, times in [('zero', [0, .01, .01, .2]), ('late', [9.21, 9.23, 9.6])]:
                requests = [(t, self.root / f'{vfr}_{label}_{n}.jpg') for n,t in enumerate(times)]
                real = shot_analyzer.run_ffmpeg_decode
                with mock.patch.object(shot_analyzer, 'run_ffmpeg_decode', wraps=real) as decode:
                    self.assertTrue(all(shot_analyzer.extract_frames_batch(video, requests)))
                    self.assertEqual(decode.call_count, 1)
                from PIL import Image, ImageChops, ImageStat
                for n, (t, output) in enumerate(requests):
                    single = self.root / f'single_{n}.jpg'
                    self.assertTrue(shot_analyzer.extract_frame(video, t, single))
                    with Image.open(output) as a, Image.open(single) as b:
                        delta = ImageStat.Stat(ImageChops.difference(a, b)).mean
                    self.assertLess(max(delta), 1.0, (vfr, t, delta))

    def test_candidate_keeps_all_eight_or_sixteen_samples(self):
        source = self.root / 'source.mp4'
        source.write_bytes(b'video')
        shot = {'video': str(source), 'source_start': 10.0, 'source_end': 12.0}
        for dense, count in [(False, 8), (True, 16)]:
            def frames(shot, directory, prepare, **kw):
                return prepare(self.root)
            def batch(path, requests, **kw):
                self.assertEqual(len(requests), count)
                self.assertEqual(requests[0][0], 10.0)
                self.assertAlmostEqual(requests[-1][0], 11.96)
                for _, out in requests:
                    out.write_bytes(b'jpg')
                return [True]*count
            with mock.patch.object(material_cache, 'cached_frames', side_effect=frames), \
                 mock.patch.object(material_cache, 'cached_sheet', return_value=self.root/'sheet.jpg'), \
                 mock.patch.object(platform_env, 'find_ffmpeg', return_value=('ffmpeg', None)), \
                 mock.patch.object(shot_analyzer, 'extract_frames_batch', side_effect=batch) as call:
                ai_editor._review_media(shot, self.root, dense=dense)
                self.assertEqual(call.call_count, 1)
