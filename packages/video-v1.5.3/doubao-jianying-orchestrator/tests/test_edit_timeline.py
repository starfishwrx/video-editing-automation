from dataclasses import replace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.domain.timeline import (Asset, AudioEvent, Canvas, EditTimeline, Sentence,
    SourceInterval, SubtitleEvent, TimelineError, VideoEvent, frame_at_us)


def example():
    return EditTimeline("task", 1, Canvas(160, 240), 2_000_000,
        (Asset("v", str(Path("source.mkv").resolve()), "video", 4_000_000, 160, 240, 60),
         Asset("a", str(Path("voice.wav").resolve()), "audio", 1_000_000)),
        (Sentence("s1", "同一句", 0, 1_000_000, "n1"), Sentence("s2", "同一句", 1_000_000, 1_000_000, "n2")),
        (VideoEvent("v1", "v", ("s1",), 0, 600_000, 0, 600_000),
         VideoEvent("v2", "v", ("s1",), 600_000, 400_000, 1_000_000, 400_000),
         VideoEvent("v3", "v", ("s2",), 1_000_000, 1_000_000, 2_000_000, 1_000_000)),
        (AudioEvent("n1", "a", "narration", 0, 1_000_000, sentence_ids=("s1",)),
         AudioEvent("n2", "a", "narration", 1_000_000, 1_000_000, sentence_ids=("s2",))),
        (SubtitleEvent("c1", "s1", "同一句", 0, 1_000_000), SubtitleEvent("c2", "s2", "同一句", 1_000_000, 1_000_000)))


class TimelineContractTests(unittest.TestCase):
    def test_repeated_text_and_multishot_have_distinct_identity(self):
        value = EditTimeline.from_dict(example().to_dict())
        self.assertEqual(len(value.sentences), 2)
        self.assertEqual(len(value.subtitles), 2)
        self.assertEqual(value.videos[1].sentence_ids, ("s1",))
        self.assertEqual(len(value.audios), 2)

    def test_save_to_new_nested_report_and_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new" / "report" / "timeline.json"
            example().save(path)
            self.assertEqual(EditTimeline.load(path).to_dict(), example().to_dict())

    def test_final_frame_ceil_does_not_cut_narration(self):
        t = example()
        v = VideoEvent("v", "v", (), 0, 1_116_959, 0, 1_116_959)
        t = replace(t, duration_us=1_116_959, sentences=(), videos=(v,), audios=(), subtitles=())
        t.validate()
        self.assertEqual(t.frame_count, 34)
        self.assertEqual(t.video_frame_range(v), (0, 34))
        self.assertEqual(frame_at_us(1_116_959, 30), 33)

    def test_split_boundaries_use_one_cumulative_rule(self):
        t = example()
        v1 = replace(t.videos[0], duration_us=616_667, source_duration_us=616_667)
        v2 = replace(t.videos[1], target_start_us=616_667, duration_us=383_333, source_duration_us=383_333)
        t = replace(t, videos=(v1, v2, t.videos[2])).validate()
        ranges = [t.video_frame_range(v) for v in t.videos]
        self.assertEqual(ranges, [(0, 18), (18, 30), (30, 60)])

    def test_malformed_and_unsupported_data_is_rejected(self):
        cases = []
        def case(path, value):
            d = example().to_dict()
            node = d
            for part in path[:-1]:
                node = node[part]
            node[path[-1]] = value
            cases.append(d)
        case(["revision"], True)
        case(["status"], "PASS")
        case(["videos", 0, "frame_map"], [0, 2])
        case(["videos", 0, "effects"], ["beautify"])
        case(["videos", 0, "speed_num"], 2)
        case(["videos", 0, "target_start_us"], 1)
        case(["videos", 0, "asset_id"], "missing")
        case(["videos", 0, "sentence_ids"], ["missing"])
        case(["videos", 0, "source_start_us"], 4_000_000)
        case(["videos", 0, "layout", "opacity"], .5)
        case(["videos", 0, "layout", "scale"], float("nan"))
        case(["audios", 0, "gain"], float("inf"))
        case(["audios", 0, "duration_us"], 1_000_001)
        case(["subtitles", 0, "sentence_id"], "missing")
        case(["subtitles", 0, "style", "font_name"], "bad,font")
        case(["sentences", 1, "sentence_id"], "s1")
        case(["capabilities"], ["arbitrary_effect"])
        for data in cases:
            with self.subTest(data=data), self.assertRaises(TimelineError):
                EditTimeline.from_dict(data)

    def test_pure_contract_does_not_import_writer_or_engine(self):
        script = ("import sys; from orchestrator.domain.timeline import EditTimeline; "
                  "assert 'jy_wrapper' not in sys.modules; "
                  "assert 'orchestrator.application.engine' not in sys.modules")
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
