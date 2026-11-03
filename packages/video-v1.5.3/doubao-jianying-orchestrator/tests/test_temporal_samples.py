import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from orchestrator.media.temporal_samples import prepare,validate
from orchestrator.media.action_frames import identity,probe

FFMPEG=shutil.which('ffmpeg');FFPROBE=shutil.which('ffprobe')
FONT=next((Path(p)for p in ('C:/Windows/Fonts/arial.ttf','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')if Path(p).is_file()),None)


@unittest.skipUnless(FFMPEG and FFPROBE and FONT,'FFmpeg, ffprobe and a fixture font are required')
class TemporalSampleMediaTests(unittest.TestCase):
    def test_owned_proxy_and_numbered_frames_keep_observed_source_pts(self):
        with tempfile.TemporaryDirectory()as tmp:
            root=Path(tmp);original=root/'original.mp4'
            subprocess.run([FFMPEG,'-v','error','-nostdin','-f','lavfi','-i',
                'testsrc2=size=108x192:rate=60:duration=1','-c:v','libx264','-pix_fmt','yuv420p',str(original)],
                check=True,capture_output=True,timeout=30)
            before=identity(original)
            samples=prepare(original,root/'samples',ffmpeg=FFMPEG,ffprobe=FFPROBE,font_path=FONT)
            self.assertEqual(identity(original),before);self.assertEqual(samples['original'],before)
            self.assertNotEqual(samples['source']['path'],str(original))
            self.assertEqual([f['source_frame']for f in samples['frames']],list(range(0,60,6)))
            self.assertEqual([f['pts_us']for f in samples['frames']],list(range(0,1_000_000,100_000)))
            self.assertEqual(len(probe(samples['video']['path'],root,ffprobe=FFPROBE)['frames']),10)
            self.assertEqual(validate(samples),samples)
            with self.assertRaises(FileExistsError):prepare(original,root/'samples',ffmpeg=FFMPEG,ffprobe=FFPROBE,font_path=FONT)
            self.assertEqual(identity(original),before)


if __name__=='__main__':unittest.main()
