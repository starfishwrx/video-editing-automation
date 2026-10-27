import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator.subtitle_units import (
    HARD_SUBTITLE_CHARS,
    split_subtitle_units,
    text_units,
)
from orchestrator import timing_contract


class SubtitleUnitTests(unittest.TestCase):
    def test_kept_word_counts_as_one(self):
        self.assertEqual(text_units("HelloKitty"), 1)
        self.assertEqual(text_units("hellokitty"), 1)
        self.assertEqual(text_units("与HelloKitty联名的云感抽纸"), 6)
        self.assertEqual(text_units("还是心相印和hellokitty联名的湿厕纸"), 8)

    def test_brand_names_count_as_one(self):
        self.assertEqual(text_units("心相印"), 1)
        self.assertEqual(text_units("七度空间"), 1)
        self.assertEqual(text_units("心相印和七度空间都是品牌"), 7)

    def test_plain_characters_still_count(self):
        self.assertEqual(text_units("不会像其他的一擦就抠破了弄一手"), 15)

    def test_length_gate_uses_units_not_raw_chars(self):
        from orchestrator.engine import Clip, OrchestrationEngine

        root = Path(tempfile.mkdtemp(prefix="subtitle_gate_"))
        eng = OrchestrationEngine({}, root, root / "report", capacity_guard=False)
        ok = Clip(video="a.mov", source_start=0.0, duration=2.0,
                  text="与HelloKitty联名的云感抽纸")
        self.assertIsNone(eng._script_segment_length_gate([ok]))
        long = Clip(video="a.mov", source_start=0.0, duration=2.0,
                    text="一" * 31)
        blocked = eng._script_segment_length_gate([long])
        self.assertEqual(blocked["status"], "SCRIPT_SEGMENT_TOO_LONG")
        self.assertEqual(blocked["max_chars"], 30)

    def test_split_on_commas_without_fifteen_pack(self):
        text = "都知道好皮肤是离不开生理盐水的，但是咋没人告诉我心相印出了这种一次性便携装的呀，现在搞活动到手一盒20片才这个价"
        parts = split_subtitle_units(text)
        self.assertEqual(len(parts), 3)
        self.assertEqual("".join(parts), text.replace(" ", ""))
        for part in parts:
            self.assertLessEqual(text_units(part), HARD_SUBTITLE_CHARS)

    def test_split_sentences_splits_periods_and_commas(self):
        script = (
            "都知道好皮肤是离不开生理盐水的，但是咋没人告诉我心相印出了这种一次性便携装的呀。"
            "而且它用处可多了，熬夜长痘或者皮肤敏感的姐妹可以用它清洁脸上的油脂。"
        )
        rows = timing_contract.split_sentences({"full_script": script})
        self.assertGreater(len(rows), 2)
        joined = "".join(r["text"] for r in rows)
        self.assertEqual(joined, script.replace("。", "").replace(" ", ""))
        for row in rows:
            self.assertLessEqual(text_units(row["text"]), HARD_SUBTITLE_CHARS)

    def test_split_sentences_keeps_matched_segments(self):
        rows = timing_contract.split_sentences({
            "segments": [{"text": "一" * 40 + "，还有下文", "video": "a.mov"}]
        })
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["video"], "a.mov")


if __name__ == "__main__":
    unittest.main()
