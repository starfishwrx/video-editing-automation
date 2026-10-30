"""Supported provider identity and invalid-configuration failure receipts."""
from pathlib import Path
import argparse
import contextlib
import io
import json
import os
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "doubao-jianying-orchestrator"))
from orchestrator.runtime import task_log, vision_config
from orchestrator.application import cli
from orchestrator.quality import diagnostics
import batch_worker as worker


class VisionRuntimeConfigTests(unittest.TestCase):
    def test_cli_build_and_generation_recovery_stop_before_paid_preparation(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"JY_VISION_TRANSPORT": "removed-provider"}), \
             patch.object(cli, "environment_report") as environment, \
             patch.object(cli, "_pre_synthesize_narration") as narration, \
             patch.object(cli, "_temporary_match") as matching, \
             contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"temporary_material_analysis": True,
                "segments": [{"video": "source.mp4", "text": "fixture", "duration": 1}]}), encoding="utf-8")
            for stage in ("build", "preflight", "semantic_match", "draft_write"):
                with self.subTest(stage=stage):
                    report = root / stage
                    args = argparse.Namespace(input=str(manifest), report_dir=str(report),
                        stage=stage, draft="", strict_script=False, delivery_mode="preview", trigger_phrase="")
                    code = cli.cmd_build(args) if stage == "build" else cli.cmd_recover(args)
                    self.assertEqual(code, 1)
                    saved = json.loads((report / "last_result.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved["terminal_state"], "BLOCKED")
                    self.assertEqual(saved["failure_code"], "ENV_ERROR")
                    self.assertIn("UNSUPPORTED_VISION_TRANSPORT", saved["error"])
                    self.assertFalse(saved["safe_to_continue"])
                    self.assertFalse((report / ".task_run.lock").exists())
            environment.assert_not_called()
            narration.assert_not_called()
            matching.assert_not_called()

    def test_invalid_provider_does_not_prevent_readonly_draft_diagnosis(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"JY_VISION_TRANSPORT": "removed-provider"}), \
             patch.object(diagnostics, "diagnose_timeline", return_value={"status": "DIAGNOSIS_OK"}) as diagnosis, \
             contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"segments": [{"video": "source.mp4"}]}), encoding="utf-8")
            code = cli.cmd_recover(argparse.Namespace(input=str(manifest), report_dir=str(root / "report"),
                                                      stage="ui_acceptance", draft=str(root / "draft")))
            self.assertEqual(code, 0)
            diagnosis.assert_called_once()
            saved = json.loads((root / "report/last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "UI_ACCEPTANCE_PENDING")

    def test_build_does_not_choose_a_model_from_a_retired_profile(self):
        for route, expected in (("volc_glm", "glm-5.3-flash"), ("qwen", "explicit-qwen")):
            with self.subTest(route=route), tempfile.TemporaryDirectory() as directory, \
                 patch.dict(os.environ, {
                     "JY_VISION_TRANSPORT": route, "JY_VISION_MODEL": "",
                     "JY_VISION_PROFILE": "aijws", "JY_QWEN_MODEL": "explicit-qwen",
                     "JY_TASK_DEADLINE_TS": "", "JY_TASK_DEADLINE_EPOCH": "",
                 }), patch.object(worker, "log"), \
                 patch.object(worker.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as build:
                result = worker.run_build({}, Path(directory) / "report", "fixture", timeout=10)
                self.assertEqual(result["rc"], 0)
                child_env = build.call_args.kwargs["env"]
                self.assertEqual(child_env["JY_VISION_MODEL"], "")
                with patch.dict(os.environ, child_env, clear=True):
                    self.assertEqual(vision_config.model(), expected)

    def test_default_and_qwen_identity_ignore_obsolete_profile(self):
        for transport, model in (("volc", "glm-5.3-flash"), ("qwen_compatible", "test-qwen")):
            with self.subTest(transport=transport), patch.dict(os.environ, {
                "JY_VISION_TRANSPORT": transport, "JY_VISION_MODEL": "",
                "JY_QWEN_MODEL": "test-qwen", "JY_VISION_PROFILE": "obsolete",
            }):
                self.assertEqual(task_log.runtime_facts()["vision_model"], model)
                self.assertEqual(worker._runtime_facts()["vision_model"], model)
                self.assertNotIn("vision_profile", worker._runtime_facts())

    def test_invalid_transport_remains_diagnosable_and_queue_is_blocked(self):
        with patch.dict(os.environ, {"JY_VISION_TRANSPORT": "removed-provider"}):
            with self.assertRaisesRegex(ValueError, "UNSUPPORTED_VISION_TRANSPORT"):
                vision_config.transport()
            for facts in (task_log.runtime_facts(), worker._runtime_facts()):
                self.assertIn("UNSUPPORTED_VISION_TRANSPORT", facts["vision_configuration_error"])
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                skill = base / "skill"
                skill.mkdir()
                (skill / "VERSION").write_text("test\n", encoding="utf-8")
                sources = base / "sources"
                sources.mkdir()
                (sources / "fixture.mp4").write_bytes(b"fixture")
                with patch.object(worker, "SKILL", skill), patch.object(worker, "DRAFTS", base / "drafts"), \
                     patch.object(worker, "require_ship_dependencies"), patch.object(worker, "_pipeline_version", return_value="test"):
                    result = worker._queue_preflight({"record_id": "r1", "script": "test", "material_dir": str(sources)})
                self.assertFalse(result["ok"])
                self.assertTrue(any("UNSUPPORTED_VISION_TRANSPORT" in issue for issue in result["issues"]))


if __name__ == "__main__":
    unittest.main()
