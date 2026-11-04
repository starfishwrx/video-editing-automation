"""Real preflight boundary: a missing native host profile must fail before production."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.runtime import headless


class HostInitializationTests(unittest.TestCase):
    def test_missing_profile_is_named_and_recoverable_without_writing_home_index(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            doctor = Mock(side_effect=[ValueError('剪映默认本地草稿目录尚未初始化'),
                {'runtime_profile': 'test-native', 'install_dir': str(root / 'install')}])
            native = SimpleNamespace(TEXT_LAYOUT_SCHEMA='centered-safe-subtitles/v1',
                __file__=str(root / 'backend/engine/production_library.py'),
                j=SimpleNamespace(nd=SimpleNamespace(DRAFT_ROOT=root / 'home', doctor=doctor),
                                  fonts=SimpleNamespace(collect_plan=Mock())))
            with patch.dict(os.environ, {'JY_DRAFTS_ROOT': str(root / 'output'),
                    'JY_HEADLESS_FONT': str(root / 'font.ttf')}, clear=True), \
                    patch.object(headless, 'backend', return_value=native), \
                    patch.object(headless.shutil, 'which', return_value='media-tool'):
                failed = headless.production_environment_report({'ready': True})
                self.assertFalse(failed['ready'])
                reason = failed['draft_backend_check']['reason']
                self.assertTrue(reason.startswith('HEADLESS_HOST_NOT_INITIALIZED:'))
                self.assertIn('剪映默认本地草稿目录尚未初始化', reason)
                self.assertIn('同一 Windows 用户', reason)
                self.assertFalse((root / 'home').exists())
                self.assertTrue(headless.production_environment_report({'ready': True})['ready'])

    def test_other_doctor_error_is_not_reclassified(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            native = SimpleNamespace(TEXT_LAYOUT_SCHEMA='centered-safe-subtitles/v1',
                j=SimpleNamespace(nd=SimpleNamespace(DRAFT_ROOT=root / 'home',
                    doctor=Mock(side_effect=ValueError('NATIVE_CODEC_MISMATCH'))),
                    fonts=SimpleNamespace(collect_plan=Mock())))
            with patch.dict(os.environ, {'JY_DRAFTS_ROOT': str(root / 'output'),
                    'JY_HEADLESS_FONT': str(root / 'font.ttf')}, clear=True), \
                    patch.object(headless, 'backend', return_value=native), \
                    patch.object(headless.shutil, 'which', return_value='media-tool'):
                report = headless.production_environment_report({'ready': True})
                self.assertEqual(report['draft_backend_check']['reason'], 'NATIVE_CODEC_MISMATCH')
