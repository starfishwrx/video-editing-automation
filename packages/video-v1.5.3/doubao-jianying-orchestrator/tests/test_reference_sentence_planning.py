"""Pre-TTS segmentation preserves content and never invents word timing."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import timing_contract


class ReferenceSentencePlanningTests(unittest.TestCase):
    def test_long_script_splits_at_existing_punctuation_before_tts(self):
        script = "这款产品包装精致，纸张厚实柔软，揉搓以后也不掉絮，湿水之后仍然不易破"
        rows = timing_contract.split_sentences({"full_script": script, "selection_mode": "ai_editor"})
        self.assertGreater(len(rows), 1)
        self.assertEqual("".join(row["text"] for row in rows), script)
        self.assertTrue(all("audio_duration_us" not in row for row in rows))

    def test_unpunctuated_long_sentence_is_not_cut_inside_words(self):
        text = "这是一个没有任何自然断句标记而且长度远远超过三十个字符需要人工确认的商品描述"
        rows = timing_contract.split_sentences({"full_script": text, "selection_mode": "ai_editor"})
        self.assertEqual(rows, [{"text": text}])

    def test_whitespace_at_clause_boundaries_is_not_lost(self):
        text = "Here is a rather long sentence, followed by another complete clause, and a final phrase"
        rows = timing_contract.split_sentences({"full_script": text, "selection_mode": "ai_editor"})
        self.assertEqual("".join(row["text"] for row in rows), text)

    def test_numeric_grouping_and_explicit_segments_are_preserved(self):
        text = "这是一段数字和时间需要保留的文案，数量为1,000个，时间是12:30，请确认内容"
        rows = timing_contract.split_sentences({"full_script": text, "selection_mode": "ai_editor"})
        self.assertEqual("".join(row["text"] for row in rows), text)
        self.assertIn("1,000", [row["text"] for row in rows][1])
        self.assertIn("12:30", "".join(row["text"] for row in rows))
        explicit = [{"text": text, "audio_duration_us": 1234567}]
        self.assertEqual(timing_contract.split_sentences({"segments": explicit}), explicit)

    def test_explicit_legacy_keeps_existing_sentence_split(self):
        text = "这是一句保留兼容的长文案，包含自然逗号而无需改变旧任务的分句结构"
        self.assertEqual(timing_contract.split_sentences({"full_script": text, "selection_mode": "legacy"}), [{"text": text}])


if __name__ == "__main__":
    unittest.main()
