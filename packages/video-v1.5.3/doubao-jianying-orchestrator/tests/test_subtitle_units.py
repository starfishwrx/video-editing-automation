import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.subtitle_units import text_units


class SubtitleUnitTests(unittest.TestCase):
    def test_kept_word_counts_as_one(self):
        self.assertEqual(text_units("HelloKitty"), 1)
        self.assertEqual(text_units("hellokitty"), 1)
        self.assertEqual(text_units("与HelloKitty联名的云感抽纸"), 6)
        self.assertEqual(text_units("还是心相印和hellokitty联名的湿厕纸"), 8)

    def test_brand_names_count_as_one(self):
        self.assertEqual(text_units("心相印"), 1)
        self.assertEqual(text_units("七度空间"), 1)
        # 心相印(1) + 和 + 七度空间(1) + 都是品牌 = 1+1+1+4 = 7
        self.assertEqual(text_units("心相印和七度空间都是品牌"), 7)

    def test_plain_characters_still_count(self):
        self.assertEqual(text_units("不会像其他的一擦就抠破了弄一手"), 15)

    def test_length_gate_uses_units_not_raw_chars(self):
        """含整词的短句 raw 可能 >15，但 units ≤30 时不应被硬闸拦住。"""
        from orchestrator.engine import Clip, OrchestrationEngine

        root = Path(tempfile.mkdtemp(prefix="subtitle_gate_"))
        eng = OrchestrationEngine({}, root, root / "report", capacity_guard=False)
        # raw len=18，units=6（HelloKitty/联名/云感/抽纸）
        ok = Clip(video="a.mov", source_start=0.0, duration=2.0,
                  text="与HelloKitty联名的云感抽纸")
        self.assertIsNone(eng._script_segment_length_gate([ok]))
        # 无整词、31 字 → 硬闸
        long = Clip(video="a.mov", source_start=0.0, duration=2.0,
                    text="一" * 31)
        blocked = eng._script_segment_length_gate([long])
        self.assertEqual(blocked["status"], "SCRIPT_SEGMENT_TOO_LONG")
        self.assertEqual(blocked["max_chars"], 30)


if __name__ == "__main__":
    unittest.main()
