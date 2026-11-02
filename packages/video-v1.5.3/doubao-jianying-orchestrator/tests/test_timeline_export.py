from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.domain.timeline import (Asset, AudioEvent, Canvas, EditTimeline, Sentence,
                                         SubtitleEvent, TimelineError, VideoEvent, VideoLayout)
from orchestrator.delivery.jianying_export import headless_plan, prepare_headless_export
from orchestrator.quality.timeline_export import compare_headless_documents


def example():
    duration = 350417
    return EditTimeline("synthetic-export", 1, Canvas(1080, 1920), duration * 2,
        (Asset("v", "source.mp4", "video", 2_000_000, 108, 192, 60),
         Asset("a", "voice.wav", "audio", duration)),
        (Sentence("s1", "重复字幕", 0, duration, "a1"),
         Sentence("s2", "重复字幕", duration, duration, "a2")),
        (VideoEvent("v1", "v", ("s1", "s2"), 0, duration * 2, 0, 841001, 6, 5),),
        (AudioEvent("a1", "a", "narration", 0, duration, sentence_ids=("s1",)),
         AudioEvent("a2", "a", "narration", duration, duration, sentence_ids=("s2",))),
        (SubtitleEvent("t1", "s1", "重复字幕", 0, duration),
         SubtitleEvent("t2", "s2", "重复字幕", duration, duration)),
        unresolved=({"sentence_id": "s1", "reason": "unverified numeric claim"},))


class HeadlessPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.font = self.root / "local.ttf"
        self.font.write_bytes(b"font parsing is the headless backend's responsibility")
        self.t = example()

    def tearDown(self):
        self.temp.cleanup()

    def plan(self, t=None, **kwargs):
        return headless_plan(t or self.t, "review", self.font, review_only=True, **kwargs)

    def test_cross_sentence_video_keeps_independent_exact_audio_and_subtitles(self):
        p = self.plan()
        self.assertEqual([len(t["segments"]) for t in p["tracks"]], [1, 2, 2])
        self.assertEqual(p["tracks"][0]["segments"][0]["source_duration_us"], 841001)
        self.assertEqual(p["tracks"][0]["segments"][0]["speed"], 1.2)
        self.assertEqual([e["duration_us"] for e in p["tracks"][1]["segments"]], [350417, 350417])
        self.assertEqual(p["tracks"][1]["segments"][1]["start_us"], 350417)
        self.assertEqual([e["text"] for e in p["tracks"][2]["segments"]], ["重复字幕", "重复字幕"])
        self.assertTrue(all(e["bold"] is True for e in p["tracks"][2]["segments"]))
        self.assertEqual(len(self.t.unresolved), 1)

    def test_formal_cannot_borrow_review_export(self):
        with self.assertRaisesRegex(TimelineError, "FORMAL_EXPORT_NOT_IMPLEMENTED"):
            headless_plan(self.t, "review", self.font)

    def test_layout_is_not_silently_dropped(self):
        t = replace(self.t, videos=(replace(self.t.videos[0], layout=VideoLayout(scale=1.1)),))
        with self.assertRaisesRegex(TimelineError, "LAYOUT"):
            self.plan(t)

    def test_unsupported_audio_processing_is_not_silently_dropped(self):
        t = replace(self.t, audios=(replace(self.t.audios[0], fade_in_us=10000), *self.t.audios[1:]))
        with self.assertRaisesRegex(TimelineError, "AUDIO_CAPABILITY"):
            self.plan(t)

    def test_empty_subtitles_do_not_create_an_unexpected_track(self):
        self.assertEqual([t["name"] for t in self.plan(replace(self.t, subtitles=()))["tracks"]],
                         ["Video_BRoll", "Narration"])

    def test_unsafe_names_fail_before_output_creation(self):
        for name in (".old", " old", "a..b", "a  b", "a\tb", "../other"):
            with self.subTest(name=name), self.assertRaisesRegex(TimelineError, "SINGLE_DRAFT_NAME"):
                headless_plan(self.t, name, self.font, review_only=True)

    def test_existing_report_is_preserved(self):
        target = self.root / "report"
        target.mkdir()
        with self.assertRaisesRegex(TimelineError, "DESTINATION_EXISTS"):
            prepare_headless_export(self.t, render_dir=self.root / "missing", name="review",
                font_path=self.font, report_dir=target, review_only=True)
        self.assertEqual(list(target.iterdir()), [])

    def test_missing_render_cannot_create_export_receipt(self):
        with self.assertRaisesRegex(TimelineError, "RENDER_EVIDENCE_INVALID"):
            prepare_headless_export(self.t, render_dir=self.root / "missing", name="review",
                font_path=self.font, report_dir=self.root / "out", review_only=True)
        self.assertFalse((self.root / "out").exists())

    def test_readback_detects_submillisecond_target_and_speed_material_drift(self):
        plan = self.plan()
        target = self.root / "review"
        assets = [{"source": "source.mp4", "relative": "Resources/v.mp4"},
                  {"source": "voice.wav", "relative": "Resources/a.wav"}]
        doc = {"duration": 700834, "fps": 30, "canvas_config": {"width": 1080, "height": 1920},
               "materials": {"videos": [], "audios": [], "texts": [], "speeds": []}, "tracks": []}
        for track in plan["tracks"]:
            tr = {"name": track["name"], "type": track["type"], "segments": []}
            for i, spec in enumerate(track["segments"]):
                mid = track["name"] + str(i)
                seg = {"material_id": mid, "target_timerange": {"start": spec["start_us"], "duration": spec["duration_us"]}}
                if track["type"] == "text":
                    doc["materials"]["texts"].append({"id": mid, "content": json.dumps({"text": spec["text"], "styles": [{"size": 8, "bold": True}]})})
                    seg["clip"] = {"transform": {"x": 0, "y": -.55}}
                else:
                    asset = next(a for a in assets if a["source"] == spec["source"])
                    doc["materials"]["videos" if track["type"] == "video" else "audios"].append(
                        {"id": mid, "path": str(target / asset["relative"])})
                    doc["materials"]["speeds"].append({"id": mid + "speed", "type": "speed", "speed": spec["speed"]})
                    seg.update(source_timerange={"start": spec["source_start_us"], "duration": spec["source_duration_us"]},
                               speed=spec["speed"], volume=spec["volume"], extra_material_refs=[mid + "speed"])
                tr["segments"].append(seg)
            doc["tracks"].append(tr)
        meta = {"draft_fold_path": str(target), "draft_root_path": str(target.parent)}
        record = {"target": str(target), "assets": assets}
        self.assertEqual(compare_headless_documents(self.t, plan, doc, meta, record)["issues"], [])
        bad = deepcopy(doc)
        bad["tracks"][1]["segments"][0]["target_timerange"]["duration"] -= 417
        self.assertIn("EXPORT_TARGET_MISMATCH:Narration:0",
                      compare_headless_documents(self.t, plan, bad, meta, record)["issues"])
        bad = deepcopy(doc)
        bad["materials"]["speeds"][0]["speed"] = 2
        self.assertIn("EXPORT_SPEED_MATERIAL_MISMATCH:Video_BRoll:0",
                      compare_headless_documents(self.t, plan, bad, meta, record)["issues"])
        bad = deepcopy(doc)
        content = json.loads(bad["materials"]["texts"][0]["content"])
        content["styles"][0]["bold"] = False
        bad["materials"]["texts"][0]["content"] = json.dumps(content)
        self.assertIn("EXPORT_SUBTITLE_BOLD_MISMATCH:Subtitles:0",
                      compare_headless_documents(self.t, plan, bad, meta, record)["issues"])


if __name__ == "__main__":
    unittest.main()
