"""Fixed narration follows the normal queue without paid voice generation."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "doubao-jianying-orchestrator"))

import batch_worker as worker
from orchestrator.application import cli, task_inputs
from orchestrator.runtime import platform_env, task_log
from tools import batch_dispatch
from tools.workflow import scheduled
from orchestrator.runtime_policy import RuntimePolicy


class FixedNarrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.audio = self.root / "first.wav"
        with wave.open(str(self.audio), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(48000)
            stream.writeframes(b"\0\0" * 24000)
        self.task = {"record_id": "fixed", "title": "draft", "script": "第一句。第二句。",
                     "material_dir": str(self.root), "delivery_mode": "preview",
                     "manifest": {"segments": [{"text": "第一句。", "audio": str(self.audio)},
                                               {"text": "第二句。", "audio": str(self.audio)}]}}

    def test_queue_segments_cannot_import_old_selection_or_alter_script(self):
        rows = task_inputs.fixed_narration_segments(self.task)
        self.assertEqual([r["audio_mode"] for r in rows], ["file", "file"])
        for rows, reason in (([{"text": "第一句。", "audio": str(self.audio), "video": "old.mp4"}],
                              "FIXED_NARRATION_FIELDS_INVALID"),
                             ([{"text": "被改写。", "audio": str(self.audio)}],
                              "FIXED_NARRATION_SCRIPT_MISMATCH"),
                             ([{"text": "第一句。第二句。", "audio": str(self.root / "missing.wav")}],
                              "FIXED_NARRATION_AUDIO_MISSING")):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                task_inputs.fixed_narration_segments({**self.task, "manifest": {"segments": rows}})

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe needed to verify real audio")
    def test_preflight_blocks_bad_audio_before_product_preparation(self):
        bad = self.root / "corrupt.wav"
        bad.write_bytes(b"not an audio file")
        task = {**self.task, "manifest": {"segments": [
            {"text": "第一句。第二句。", "audio": str(bad)}]}}
        policy = RuntimePolicy(task_workers=1, prep_workers=1, slot_dir=str(self.root / "slots"))
        with patch.object(platform_env, "environment_report", return_value={"ready": True, "ffprobe": shutil.which("ffprobe")}), \
             patch.object(cli, "_production_environment_report", return_value={"ready": True}), \
             patch.object(task_log, "runtime_facts", return_value={}), \
             patch.object(batch_dispatch, "_voice_error", return_value=None):
            result = batch_dispatch.dispatch([task], work=self.root / "work", policy=policy,
                preparer=lambda *a: self.fail("must not prepare or call vision"),
                runner=lambda *a: self.fail("must not start worker"))
        self.assertEqual(result[0]["stage"], "preflight")
        self.assertIn("FIXED_NARRATION_AUDIO_INVALID:S1", result[0]["error"])

    def test_explicit_source_inventory_rejects_missing_or_conflicting_files(self):
        source = self.root / "one.mp4"
        source.write_bytes(b"fixture")
        task = {**self.task, "material_sources": [str(source)]}
        self.assertEqual(worker.task_video_files(task), [str(source.resolve())])
        for values in ([], [str(source), str(source)], [str(self.root / "missing.mp4")]):
            with self.subTest(values=values), self.assertRaises(ValueError):
                worker.task_video_files({**task, "material_sources": values})
        with self.assertRaisesRegex(ValueError, "MATERIAL_SOURCE_LIST_CONFLICT"):
            worker.task_video_files({**task, "manifest": {"material_sources": ["other.mp4"]}})

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe needed to verify real audio")
    def test_missing_music_or_explicit_source_blocks_before_material_preparation(self):
        tasks = [{**self.task, "bgm": {"path": str(self.root / "missing.wav")}},
                 {**self.task, "material_sources": [str(self.root / "missing.mp4")]}]
        with patch.object(platform_env, "environment_report", return_value={"ready": True, "ffprobe": shutil.which("ffprobe")}), \
             patch.object(cli, "_production_environment_report", return_value={"ready": True}), \
             patch.object(task_log, "runtime_facts", return_value={}):
            results = [batch_dispatch._production_preflight(task) for task in tasks]
        self.assertEqual([r["ready"] for r in results], [False, False])
        self.assertEqual(results[0]["reason"], "BGM_SOURCE_AUDIO_INVALID")
        self.assertIn("MATERIAL_SOURCE_LIST_INVALID", results[1]["reason"])

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe needed to verify real audio")
    def test_relative_queue_bgm_uses_the_same_root_at_preflight_and_worker(self):
        task = {**self.task, "bgm": "first.wav"}
        with patch.object(batch_dispatch, "ROOT", self.root), \
             patch.object(platform_env, "environment_report", return_value={"ready": True, "ffprobe": shutil.which("ffprobe")}), \
             patch.object(cli, "_production_environment_report", return_value={"ready": True}), \
             patch.object(task_log, "runtime_facts", return_value={}):
            admission = batch_dispatch._production_preflight(task)
        self.assertTrue(admission["ready"], admission)
        self.assertEqual(worker._meeting_bgm_spec(task["bgm"], base_dir=self.root)["path"], str(self.audio))

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe needed to verify real audio")
    def test_readable_files_supply_actual_duration_without_tts(self):
        manifest = {"segments": task_inputs.fixed_narration_segments(self.task)}
        cli._PROBE_CACHE.clear()
        self.addCleanup(cli._PROBE_CACHE.clear)
        with patch.object(cli.voice_tts, "generate_batch", side_effect=AssertionError("unexpected TTS")):
            timing = cli._pre_synthesize_narration(manifest, self.root, None)
        self.assertEqual((timing["tts_attempted"], timing["audio_count"], timing["estimated_count"]), (0, 2, 0))
        self.assertEqual([r["audio_duration_us"] for r in timing["rows"]], [500000, 500000])

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe needed to verify real audio")
    def test_relative_audio_is_resolved_against_manifest(self):
        original = [{"text": "一句", "audio": "first.wav"}]
        normalized = task_inputs.validate_explicit_audio(original, self.root, ffprobe=shutil.which("ffprobe"))
        self.assertEqual(normalized[0]["audio"], str(self.audio))
        self.assertEqual(original[0]["audio"], "first.wav")

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe needed to verify real audio")
    def test_direct_build_rejects_bad_audio_before_voice_or_selection(self):
        bad = self.root / "corrupt.wav"
        bad.write_bytes(b"not an audio file")
        manifest = {"task_id": "fixture", "mode": "standard", "full_script": "一句。",
                    "temporary_material_analysis": True,
                    "segments": [{"text": "一句。", "audio": "corrupt.wav"}]}
        source = self.root / "manifest.json"
        source.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        args = SimpleNamespace(input=str(source), report_dir=str(self.root / "report"),
                               delivery_mode="preview", trigger_phrase="", strict_script=False)
        with patch.object(cli, "environment_report", return_value={"ready": True, "ffprobe": shutil.which("ffprobe")}), \
             patch.object(cli, "_production_environment_report", return_value={"ready": True}), \
             patch.object(cli, "_vision_configuration_gate", return_value=None), \
             patch.object(cli, "_lock_voice_once", side_effect=AssertionError("voice started")), \
             patch.object(cli, "_temporary_match", side_effect=AssertionError("selection started")), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            code = cli.cmd_build(args)
        self.assertEqual(code, 1)
        self.assertIn("FIXED_NARRATION_AUDIO_INVALID:S1", output.getvalue())

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe needed to verify real audio")
    def test_direct_build_blocks_changed_or_unbound_audio_before_engine(self):
        for failure in ("changed", "unbound"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                audio = root / "audio.wav"
                shutil.copyfile(self.audio, audio)
                manifest = {"task_id": "fixture", "mode": "standard", "full_script": "一句。",
                            "temporary_material_analysis": True,
                            "segments": [{"text": "一句。", "audio": str(audio), "audio_mode": "file"}]}
                source = root / "manifest.json"
                source.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                args = SimpleNamespace(input=str(source), report_dir=str(root / "report"),
                                       delivery_mode="preview", trigger_phrase="", strict_script=False)

                def matching(current, *_args, **_kwargs):
                    if failure == "changed":
                        data = bytearray(audio.read_bytes())
                        data[-1] ^= 1
                        before = audio.stat()
                        audio.write_bytes(data)
                        import os
                        os.utime(audio, ns=(before.st_atime_ns, before.st_mtime_ns))
                        return current, None
                    return {**current, "segments": [{"text": "一句。", "video": "fixture.mp4"}]}, None

                cli._PROBE_CACHE.clear()
                with patch.object(cli, "environment_report", return_value={"ready": True, "ffprobe": shutil.which("ffprobe")}), \
                     patch.object(cli, "_production_environment_report", return_value={"ready": True}), \
                     patch.object(cli, "_vision_configuration_gate", return_value=None), \
                     patch.object(cli, "_lock_voice_once", return_value=(None, None)), \
                     patch.object(cli, "_temporary_match", side_effect=matching), \
                     patch.object(cli, "OrchestrationEngine") as engine, \
                     patch.object(cli.voice_tts, "generate_batch") as tts, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    code = cli.cmd_build(args)
                self.assertEqual(code, 1)
                self.assertIn("SELECTION_AUDIO_CHANGED" if failure == "changed" else
                              "FIXED_NARRATION_BINDING_CHANGED:S1", output.getvalue())
                engine.assert_not_called()
                tts.assert_not_called()
        cli._PROBE_CACHE.clear()

    @unittest.skipUnless(shutil.which("ffprobe"), "ffprobe needed to verify real audio")
    def test_engine_cannot_publish_success_if_fixed_audio_changes_during_build(self):
        import os
        for changed in ("narration", "bgm"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                audio, bgm = root / "audio.wav", root / "bgm.wav"
                shutil.copyfile(self.audio, audio)
                shutil.copyfile(self.audio, bgm)
                manifest = {"task_id": "fixture", "mode": "standard", "full_script": "一句。",
                            "temporary_material_analysis": True, "bgm": {"path": str(bgm)},
                            "segments": [{"text": "一句。", "audio": str(audio), "audio_mode": "file"}]}
                source = root / "manifest.json"
                source.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                args = SimpleNamespace(input=str(source), report_dir=str(root / "report"),
                                       delivery_mode="preview", trigger_phrase="", strict_script=False)

                def build():
                    target = audio if changed == "narration" else bgm
                    before = target.stat()
                    data = bytearray(target.read_bytes())
                    data[-1] ^= 1
                    target.write_bytes(data)
                    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
                    return {"status": "SUCCESS", "draft_path": str(root / "draft")}

                cli._PROBE_CACHE.clear()
                with patch.object(cli, "environment_report", return_value={"ready": True, "ffprobe": shutil.which("ffprobe")}), \
                     patch.object(cli, "_production_environment_report", return_value={"ready": True}), \
                     patch.object(cli, "_vision_configuration_gate", return_value=None), \
                     patch.object(cli, "_lock_voice_once", return_value=(None, None)), \
                     patch.object(cli, "_temporary_match", side_effect=lambda current, *_a, **_k: (current, None)), \
                     patch.object(cli, "OrchestrationEngine", return_value=SimpleNamespace(run=build)) as engine, \
                     patch.object(cli.voice_tts, "generate_batch") as tts, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    code = cli.cmd_build(args)
                self.assertEqual(code, 1)
                self.assertIn("FIXED_NARRATION_CHANGED_DURING_BUILD" if changed == "narration" else
                              "BGM_CHANGED_DURING_BUILD", output.getvalue())
                self.assertEqual(json.loads(output.getvalue())["draft_path"], str(root / "draft"))
                engine.assert_called_once()
                tts.assert_not_called()
        cli._PROBE_CACHE.clear()

    def test_qc_preflight_does_not_require_original_narration_files(self):
        bad_task = {**self.task, "manifest": {"segments": [
            {"text": "第一句。第二句。", "audio": str(self.root / "missing.wav")} ]}}
        with patch.object(platform_env, "environment_report", return_value={"ready": True}), \
             patch.object(cli, "_production_environment_report", return_value={"ready": True}), \
             patch.object(task_log, "runtime_facts", return_value={}):
            build = batch_dispatch._production_preflight(bad_task)
            qc = batch_dispatch._production_preflight(bad_task, check_narration=False)
        self.assertFalse(build["ready"])
        self.assertTrue(qc["ready"])

    def test_scheduled_queue_reaches_worker_with_fixed_audio(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"offline source fixture")
        self.task["material_sources"] = [str(source)]
        self.task.update(title="卫生巾体验", product="test product", sku="SKU-1", brand="test brand", bgm="first.wav")
        self.task["manifest"]["product_id"] = "product-1"
        work = self.root / "work"
        drafts = self.root / "drafts"
        drafts.mkdir()
        (work / "fixed" / "eval").mkdir(parents=True)
        queue = self.root / "queue.json"
        queue.write_text(json.dumps([self.task], ensure_ascii=False), encoding="utf-8")
        captured, prepared = [], []
        blocked = {"returncode": 1, "stdout": json.dumps({"status": "ENV_ERROR",
                   "failed_stage": "preflight", "failure_class": "environment",
                   "failure_code": "ENVIRONMENT_PRECHECK_FAILED", "retryable": True}), "stderr": ""}

        def runner(task, stage, env, task_work):
            self.assertEqual(stage, "build")
            worker.process_task(task, admitted=True)
            return {"record_id": "fixed", "status": "BLOCKED", "stage": "preflight", "retryable": True}

        def prepare(task, directory, env):
            def fake_process(command, **_kwargs):
                path = Path(command[command.index("--input") + 1])
                prepared.append(json.loads(path.read_text(encoding="utf-8")))
                output = Path(command[command.index("--report-dir") + 1]) / "shot_candidates.json"
                output.write_text(json.dumps({"material_cache": {"ready": True}}), encoding="utf-8")
                return SimpleNamespace(returncode=0, stderr="", stdout="")
            with patch.object(batch_dispatch.subprocess, "run", side_effect=fake_process):
                return batch_dispatch.prepare_product(task, directory, env)

        def invoke(tasks, **kwargs):
            return batch_dispatch.dispatch(tasks, **kwargs,
                policy=RuntimePolicy(task_workers=1, prep_workers=1, slot_dir=str(self.root / "slots")),
                preparer=prepare, runner=runner)

        def build(manifest, *args, **kwargs):
            captured.append(manifest)
            return blocked

        with patch.object(worker, "WORK", work), patch.object(worker, "DRAFTS", drafts), \
             patch.object(worker, "PIPE_ROOT", self.root), \
             patch.object(worker, "_runtime_facts", return_value={}), \
             patch.object(worker, "video_files", side_effect=AssertionError("explicit inventory rescanned")), \
             patch.object(worker, "_save_frozen_task"), \
             patch.object(worker, "restore_qc_rejections", return_value=({}, {})), \
             patch.object(worker, "build_progress_inputs", return_value={"fixture": True}), \
             patch.object(worker, "publish_failure_notification", return_value={}), \
             patch.object(worker, "progress"), patch.object(worker, "log"), \
             patch.object(worker.production_eval, "runtime_identity", return_value={}), \
             patch.object(worker.production_eval, "freeze", return_value={"mode": "enforce"}), \
             patch.object(worker.production_eval, "write_once"), \
             patch.object(worker, "run_build", side_effect=build), \
             patch.object(batch_dispatch, "_production_preflight", return_value={"ready": True}), \
             patch.object(batch_dispatch, "_voice_error", return_value=None), \
             patch.object(batch_dispatch, "_bgm_error", return_value=None):
            result = scheduled.run_once(queue, work, dispatcher=invoke, record_id="fixed")
        self.assertEqual(len(captured), 1)
        self.assertEqual(result["tasks"][0]["stage"], "build")
        self.assertEqual(captured[0]["segments"], task_inputs.fixed_narration_segments(self.task))
        self.assertEqual(captured[0]["material_sources"], [str(source)])
        self.assertEqual(captured[0]["bgm"]["path"], str(self.audio))
        self.assertEqual(captured[0]["delivery_mode"], "preview")
        from orchestrator.vision import vision_analyzer
        from orchestrator import rulepack
        shot = {"video": str(source), "source_start": 0, "source_end": 1}
        self.assertEqual(vision_analyzer._vision_cache_key(prepared[0], shot),
                         vision_analyzer._vision_cache_key(captured[0], shot))
        self.assertEqual(rulepack.detect_category(prepared[0])[0], "weipin")
        self.assertEqual(rulepack.detect_category(prepared[0])[0], rulepack.detect_category(captured[0])[0])
        self.assertNotEqual(batch_dispatch.product_key(self.task),
                            batch_dispatch.product_key({**self.task, "title": "普通体验"}))
        self.assertEqual([captured[0][k] for k in ("product_id", "brand", "sku")],
                         ["product-1", "test brand", "SKU-1"])

    def test_preview_pass_remains_local_without_a_skip_ship_switch(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"fixture")
        work, drafts = self.root / "work", self.root / "drafts"
        drafts.mkdir()
        (work / "fixed" / "eval").mkdir(parents=True)
        with patch.object(worker, "WORK", work), patch.object(worker, "DRAFTS", drafts), \
             patch.object(worker, "SKIP_SHIP", False), patch.object(worker, "_runtime_facts", return_value={}), \
             patch.object(worker, "video_files", return_value=[str(source)]), \
             patch.object(worker, "_save_frozen_task"), patch.object(worker, "restore_qc_rejections", return_value=({}, {})), \
             patch.object(worker, "build_progress_inputs", return_value={"fixture": True}), \
             patch.object(worker, "progress"), patch.object(worker, "log"), \
             patch.object(worker.production_eval, "runtime_identity", return_value={}), \
             patch.object(worker.production_eval, "freeze", return_value={"mode": "enforce"}), \
             patch.object(worker.production_eval, "write_once"), \
             patch.object(worker, "run_build", return_value={"rc": 0, "stdout": "", "stderr": ""}), \
             patch.object(worker, "validate_visual_match", return_value=(True, "ok")), \
             patch.object(worker, "run_post_draft_qc", return_value={"status": "PASS", "output": "qc.json"}), \
             patch.object(worker, "validate_draft_package", return_value={"ok": True}), \
             patch.object(worker, "evaluate_task", return_value={}), \
             patch.object(worker, "finalize_closed_loop") as delivery, \
             patch.object(worker, "publish_failure_notification") as notification:
            worker.process_task(self.task, admitted=True)
        done = json.loads((work / "fixed" / "done.json").read_text(encoding="utf-8"))
        self.assertEqual((done["status"], done["delivery_mode"], done["distribution_status"]),
                         ("PREVIEW_READY", "preview", "LOCAL_ONLY"))
        self.assertEqual(done["qc_reports"][0]["status"], "PASS")
        delivery.assert_not_called()
        notification.assert_not_called()

    def test_unconfirmed_material_duration_stops_worker_even_when_candidate_cache_appears(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"fixture")
        work, drafts = self.root / "work", self.root / "drafts"
        drafts.mkdir()
        (work / "fixed" / "eval").mkdir(parents=True)
        calls = []
        def build(manifest, report, *args, **kwargs):
            calls.append(kwargs.get("reuse_analysis"))
            report.mkdir(parents=True, exist_ok=True)
            (report / "shot_candidates.json").write_text(json.dumps({"shots": [{"description": "observed product",
                "visual_tags": ["product"], "evidence_tags": ["product"], "analysis_confidence": 0.9}]}), encoding="utf-8")
            return {"rc": 1, "stdout": "", "stderr": "MATERIAL_DURATION_BOUND_UNCONFIRMED",
                    "combined": "MATERIAL_DURATION_BOUND_UNCONFIRMED"}
        with patch.object(worker, "WORK", work), patch.object(worker, "DRAFTS", drafts), \
             patch.object(worker, "PIPE_ROOT", self.root), patch.object(worker, "_runtime_facts", return_value={}), \
             patch.object(worker, "video_files", return_value=[str(source)]), \
             patch.object(worker, "_save_frozen_task"), patch.object(worker, "restore_qc_rejections", return_value=({}, {})), \
             patch.object(worker, "build_progress_inputs", side_effect=lambda _m, cache, _r: {"cached": bool(cache)}), \
             patch.object(worker, "progress"), patch.object(worker, "log"), \
             patch.object(worker, "publish_failure_notification", return_value={}), \
             patch.object(worker.production_eval, "runtime_identity", return_value={}), \
             patch.object(worker.production_eval, "freeze", return_value={"mode": "enforce"}), \
             patch.object(worker.production_eval, "write_once"), \
             patch.object(worker, "run_build", side_effect=build), \
             patch.object(worker, "run_post_draft_qc") as qc:
            worker.process_task(self.task, admitted=True)
        self.assertEqual(calls, [None])
        qc.assert_not_called()
        done = json.loads((work / "fixed" / "done.json").read_text(encoding="utf-8"))
        self.assertEqual((done["terminal_state"], done["failure_class"], done["failure_code"]),
                         ("BLOCKED", "environment", "MATERIAL_DURATION_UNAVAILABLE"))
        self.assertFalse(done["retryable"])


if __name__ == "__main__":
    unittest.main()
