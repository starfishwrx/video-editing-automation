import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orchestrator import cli, draft_safety, engine, task_log


class CaptionIntegrationTests(unittest.TestCase):
    def test_long_narration_is_not_rejected_as_one_legacy_subtitle(self):
        instance = engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
        instance.m = {"selection_mode": "ai_editor"}
        clip = engine.Clip("unused.mp4", 0, 12, text="一段需要按实际语音分成多条字幕的较长口播" * 3)
        self.assertIsNone(instance._script_segment_length_gate([clip]))

    def _engine(self, root: Path, *, selection_mode="ai_editor"):
        instance = engine.OrchestrationEngine.__new__(engine.OrchestrationEngine)
        instance.editor_root = root
        instance.report_dir = root / "report"
        instance.report_dir.mkdir(parents=True, exist_ok=True)
        instance.ffmpeg = None
        instance.m = {
            "draft_name": "caption_test",
            "caption_source": "audio",
            "selection_mode": selection_mode,
            "brightness": 0,
        }
        return instance

    def test_asr_failure_blocks_before_jyproject_and_never_uses_legacy_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instance = self._engine(root)
            fake_wrapper = types.ModuleType("jy_wrapper")
            fake_wrapper.JyProject = Mock(name="JyProject")
            fake_wrapper.draft = SimpleNamespace()
            verdict = SimpleNamespace(ok=True, status="OK", message="")
            clips = [engine.Clip("video.mp4", 0, 1, text="legacy clip text", audio_mode="mute")]
            failed = {"ok": False, "issues": ["ASR timeout"]}
            with patch.dict(sys.modules, {"jy_wrapper": fake_wrapper}), \
                 patch.object(engine, "find_drafts_root", return_value=root / "drafts"), \
                 patch.object(draft_safety, "pre_write_check", return_value=verdict), \
                 patch.object(engine.OrchestrationEngine, "_normalize_narrations", return_value={}), \
                 patch("orchestrator.audio_captions.build_captions", return_value=failed), \
                 patch.object(engine.OrchestrationEngine, "_background_pressure", return_value=None), \
                 patch.object(engine.OrchestrationEngine, "_event") as event:
                result = instance.write_draft(clips, None)

            self.assertEqual(result["status"], "CAPTION_ASR_UNCERTIFIED")
            self.assertFalse(result["formal_delivery_allowed"])
            self.assertEqual(result["issues"], ["ASR timeout"])
            fake_wrapper.JyProject.assert_not_called()
            event.assert_not_called()

    def test_success_adds_one_caption_batch_without_per_clip_legacy_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            drafts_root = root / "drafts"
            draft_dir = drafts_root / "caption_test"
            project = Mock()
            project.draft_dir = str(draft_dir)
            project.add_media_safe.side_effect = [
                SimpleNamespace(target_timerange=SimpleNamespace(duration=1_000_000)),
                SimpleNamespace(target_timerange=SimpleNamespace(duration=1_000_000)),
            ]
            project.get_track_duration.return_value = 2_000_000
            project.save.side_effect = lambda: draft_dir.mkdir(parents=True, exist_ok=True)
            fake_wrapper = types.ModuleType("jy_wrapper")
            fake_wrapper.JyProject = Mock(return_value=project)
            fake_wrapper.draft = SimpleNamespace()
            cues = [{"start_us": 100_000, "end_us": 800_000, "text": "ASR连续字幕"}]
            instance = self._engine(root)
            instance.profile = SimpleNamespace(staging_workers=1, name="test")
            instance.total_us = 2_000_000
            instance.delivery_mode = "formal"
            instance.preview_uncertified_reasons = []
            instance.warnings = []
            instance.pending = []
            instance.env = None
            instance._global_voice = "male_energetic"
            clips = [
                engine.Clip("video-a.mp4", 0, 1, text="legacy A", audio_mode="mute"),
                engine.Clip("video-b.mp4", 0, 1, text="legacy B", audio_mode="mute"),
            ]
            with patch.dict(sys.modules, {"jy_wrapper": fake_wrapper}), \
                 patch.object(engine, "find_drafts_root", return_value=drafts_root), \
                 patch.object(draft_safety, "pre_write_check",
                              return_value=SimpleNamespace(ok=True, status="OK", message="")), \
                 patch.object(engine.OrchestrationEngine, "_normalize_narrations", return_value={}), \
                 patch("orchestrator.audio_captions.build_captions",
                       return_value={"ok": True, "cues": cues, "identity": "caption-id"}), \
                 patch.object(engine.OrchestrationEngine, "_background_pressure", return_value=None), \
                 patch.object(engine.OrchestrationEngine, "_stage", side_effect=lambda source, media: str(source)), \
                 patch.object(engine.OrchestrationEngine, "_uncertified_coverage", return_value=None), \
                 patch.object(engine.OrchestrationEngine, "_invariant_gate", return_value=None), \
                 patch.object(engine.OrchestrationEngine, "_invariant_items", return_value=[]), \
                 patch.object(engine.OrchestrationEngine, "_fps", return_value=30), \
                 patch.object(engine.timing_contract, "check_invariants", return_value=[]), \
                 patch.object(engine.draft_safety, "post_write_validate", return_value=[]), \
                 patch.object(engine.draft_safety, "sync_root_meta"), \
                 patch.object(engine.OrchestrationEngine, "_coverage_report", return_value={}):
                result = instance.write_draft(clips, None)

            self.assertEqual(result["status"], "SUCCESS")
            project.add_caption_cues.assert_called_once_with(cues, track_name="Subtitles")
            project.add_text_simple.assert_not_called()

    def test_maintained_jyproject_writes_one_native_subtitle_track_and_roundtrips(self):
        editor_root = Path(__file__).resolve().parents[2] / "jianying-editor"
        scripts = str(editor_root / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from jy_wrapper import JyProject

        cue = {"start_us": 120_000, "end_us": 920_000, "text": "整句ASR字幕"}
        expected = {
            "video_segments": 0, "narration_segments": 0, "subtitle_segments": 1,
            "bgm_required": False, "bgm_segments": 0,
            "subtitle_style_required": True, "subtitle_font_size": 8.0,
            "subtitle_bold": True, "subtitle_transform_x": 0.0,
            "subtitle_transform_y": -0.55, "subtitle_strip_punctuation": True,
            "native_subtitles_required": True, "subtitle_cues": [cue],
        }
        with tempfile.TemporaryDirectory() as tmp:
            project = JyProject("caption_roundtrip", drafts_root=tmp, overwrite=True)
            project.add_caption_cues([cue], track_name="Subtitles")
            project.save()
            draft_dir = Path(project.draft_dir)
            document = json.loads((draft_dir / "draft_info.json").read_text(encoding="utf-8"))
            text_tracks = [track for track in document["tracks"] if track.get("type") == "text"]
            self.assertEqual([track["name"] for track in text_tracks], ["Subtitles"])
            self.assertEqual(len(text_tracks[0]["segments"]), 1)
            segment = text_tracks[0]["segments"][0]
            material = next(item for item in document["materials"]["texts"]
                            if item["id"] == segment["material_id"])
            self.assertEqual(material["type"], "subtitle")
            self.assertEqual(json.loads(material["content"])["text"], cue["text"])
            self.assertEqual(segment["target_timerange"], {
                "start": cue["start_us"], "duration": cue["end_us"] - cue["start_us"]})
            self.assertEqual(draft_safety.post_write_validate(draft_dir, expected_tracks=expected), [])

            for mutation, expected_issue in (
                (lambda data, mat, seg: mat.update(type="text"), "未写为原生 subtitle"),
                (lambda data, mat, seg: seg["target_timerange"].update(start=cue["start_us"] + 1),
                 "与音频识别时间不一致"),
                (lambda data, mat, seg: json.loads(mat["content"]).update(text="错误文本"),
                 "与音频识别文本不一致"),
            ):
                corrupted = copy.deepcopy(document)
                track = next(t for t in corrupted["tracks"] if t.get("name") == "Subtitles")
                segment = track["segments"][0]
                material = next(item for item in corrupted["materials"]["texts"]
                                if item["id"] == segment["material_id"])
                if "文本" in expected_issue:
                    content = json.loads(material["content"])
                    content["text"] = "错误文本"
                    material["content"] = json.dumps(content, ensure_ascii=False)
                else:
                    mutation(corrupted, material, segment)
                (draft_dir / "draft_info.json").write_text(
                    json.dumps(corrupted, ensure_ascii=False), encoding="utf-8")
                issues = draft_safety.post_write_validate(draft_dir, expected_tracks=expected)
                self.assertTrue(any(expected_issue in issue for issue in issues), issues)

    def test_asr_uncertified_is_terminal_and_cli_keeps_evidence(self):
        issue = "ASR transcript/timing was not certified"
        contract = task_log.result_contract(
            {"status": "CAPTION_ASR_UNCERTIFIED", "issues": [issue],
             "caption_report": "report/captions/audio_captions.json"},
            task_id="task-1", run_id="run-1", task_log="report/task_log.json")
        self.assertEqual(contract["terminal_state"], "UNCERTIFIED")
        self.assertEqual(contract["failure_class"], "semantic")
        self.assertEqual(contract["failure_code"], "CAPTION_ASR_UNCERTIFIED")
        compact = cli._compact_build_result(contract)
        self.assertEqual(compact["issues"], [issue])
        self.assertEqual(compact["caption_report"], "report/captions/audio_captions.json")


if __name__ == "__main__":
    unittest.main()
