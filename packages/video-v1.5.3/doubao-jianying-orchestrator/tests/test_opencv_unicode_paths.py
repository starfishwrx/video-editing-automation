# -*- coding: utf-8 -*-
"""Windows path helpers must survive spaces and non-ASCII directory names."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import sys

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import vision_analyzer as va


@unittest.skipUnless(os.name == "nt", "Windows path helpers")
class OpenCVUnicodePathTests(unittest.TestCase):
    def test_imread_imwrite_roundtrip_non_ascii_and_space(self) -> None:
        try:
            import cv2  # noqa: F401
        except Exception:
            self.skipTest("opencv unavailable")

        root = Path(tempfile.mkdtemp(prefix="jy_opencv_unicode_"))
        nested = root / "中文目录 with space"
        nested.mkdir(parents=True, exist_ok=True)
        target = nested / "帧_01.jpg"
        canvas = np.zeros((24, 32, 3), dtype=np.uint8)
        canvas[:, :] = (12, 64, 200)
        self.assertTrue(va._cv2_imwrite(target, canvas))
        self.assertTrue(target.is_file())
        loaded = va._cv2_imread(target)
        self.assertIsNotNone(loaded)
        self.assertEqual(tuple(loaded.shape), (24, 32, 3))


if __name__ == "__main__":
    unittest.main()
