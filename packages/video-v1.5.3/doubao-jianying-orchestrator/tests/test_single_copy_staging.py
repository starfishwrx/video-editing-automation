"""Draft staging retains independent bytes without an intermediate upload copy."""
from pathlib import Path
import shutil
import os
import threading
from concurrent.futures import ThreadPoolExecutor
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.engine import OrchestrationEngine, Clip


class SingleCopyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.source = self.root / 'source.mov'
        self.source.write_bytes(b'original synthetic media')
        self.engine = OrchestrationEngine({}, self.root, self.root / 'report', capacity_guard=False)
        self.engine._drafts_root = self.root
        self.media = self.root / 'draft' / 'media'

    def admit(self):
        clip = Clip(video=str(self.source), source_start=0, duration=1, audio_mode='mute')
        return self.engine._upload_videos_and_check_capacity([clip, clip])

    def test_one_copy_and_draft_survives_original_removal(self):
        with mock.patch('orchestrator.engine.shutil.copy2', wraps=shutil.copy2) as copy:
            self.assertIsNone(self.admit())
            copy.assert_not_called()
            staged = Path(self.engine._stage(self.source, self.media))
            self.assertEqual(self.engine._stage(self.source, self.media), str(staged))
            self.assertEqual(copy.call_count, 1)
        self.assertFalse((self.root / 'report' / 'uploads').exists())
        self.source.unlink()
        self.assertEqual(staged.read_bytes(), b'original synthetic media')

    def test_changed_source_after_admission_is_blocked(self):
        self.admit()
        self.source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'CHANGED_AFTER_ADMISSION'):
            self.engine._stage(self.source, self.media)
        self.assertFalse(list(self.media.glob('*.mov')))

    def test_source_changed_during_copy_never_publishes_or_certifies(self):
        original = shutil.copy2
        def mutate(src, dst):
            result = original(src, dst)
            Path(src).write_bytes(b'changed during copy')
            return result
        with mock.patch('orchestrator.engine.shutil.copy2', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'CHANGED_DURING_STAGING'):
                self.engine._stage(self.source, self.media)
        self.assertEqual(list(self.media.iterdir()), [])

    def test_interrupted_copy_cleans_temporary_file(self):
        def interrupted(src, dst):
            Path(dst).write_bytes(b'partial')
            raise OSError('disk full')
        with mock.patch('orchestrator.engine.shutil.copy2', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.engine._stage(self.source, self.media)
        self.assertEqual(list(self.media.iterdir()), [])

    def test_same_named_sources_remain_distinct_and_original_not_linked(self):
        other = self.root / 'other' / self.source.name
        other.parent.mkdir()
        other.write_bytes(b'other source')
        one = Path(self.engine._stage(self.source, self.media))
        two = Path(self.engine._stage(other, self.media))
        self.assertNotEqual(one, two)
        one.write_bytes(b'edited draft media')
        self.assertEqual(self.source.read_bytes(), b'original synthetic media')
        self.assertEqual(two.read_bytes(), b'other source')

    def test_concurrent_same_names_cannot_alias(self):
        other = self.root / 'other' / self.source.name
        other.parent.mkdir()
        other.write_bytes(b'other source')
        barrier = threading.Barrier(2)
        original = shutil.copy2
        def synchronized(src, dst):
            barrier.wait(timeout=2)
            return original(src, dst)
        with mock.patch('orchestrator.engine.shutil.copy2', side_effect=synchronized), \
                ThreadPoolExecutor(max_workers=2) as pool:
            paths = list(pool.map(lambda src: Path(self.engine._stage(src, self.media)),
                                  [self.source, other]))
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual(paths[0].read_bytes(), self.source.read_bytes())
        self.assertEqual(paths[1].read_bytes(), other.read_bytes())

    def test_same_size_modified_cached_copy_is_not_reused(self):
        one = Path(self.engine._stage(self.source, self.media))
        old = one.stat().st_mtime_ns
        one.write_bytes(b'x' * self.source.stat().st_size)
        os.utime(one, ns=(old + 1000000000, old + 1000000000))
        recovered = Path(self.engine._stage(self.source, self.media))
        self.assertEqual(recovered.read_bytes(), self.source.read_bytes())

    def test_truncated_cached_copy_is_replaced(self):
        one = Path(self.engine._stage(self.source, self.media))
        one.write_bytes(b'bad')
        recovered = Path(self.engine._stage(self.source, self.media))
        self.assertEqual(recovered.read_bytes(), self.source.read_bytes())
