"""Warm caches and public CLI boundaries must expose broken shipped resources."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.application import cli
from orchestrator.audio import bgm_selector, library_resources, voice_library


class AudioCatalogAdmissionTests(unittest.TestCase):
    def test_warm_catalog_removal_corruption_and_repair_are_visible_to_public_commands(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "catalogs"
            shutil.copytree(library_resources.CATALOGS_ROOT, root)
            with patch.object(library_resources, "CATALOGS_ROOT", root):
                voice_library.load_library()
                bgm_selector.load_library()
                for name in library_resources.CATALOG_FIELDS:
                    path = root / name
                    original = path.read_bytes()
                    for broken, code in ((None, "MISSING"), (b"wrong,columns\n", "INVALID")):
                        with self.subTest(name=name, code=code):
                            if broken is None:
                                path.unlink()
                            else:
                                path.write_bytes(broken)
                            command = "recommend-bgm" if name.startswith("cloud_") else "list-library"
                            with contextlib.redirect_stdout(io.StringIO()) as output:
                                self.assertEqual(cli.main([command]), 1)
                            self.assertEqual(json.loads(output.getvalue()), {
                                "status": "ENV_ERROR", "failure_class": "environment",
                                "reason": f"RUNTIME_CATALOG_{code}:{name}"})
                            with patch.object(cli, "environment_report", return_value={"ready": True}), \
                                    contextlib.redirect_stdout(io.StringIO()) as output:
                                self.assertEqual(cli.main(["env"]), 1)
                            report = json.loads(output.getvalue())
                            self.assertFalse(report["ready"])
                            self.assertEqual(report["reason"], f"RUNTIME_CATALOG_{code}:{name}")
                            if not name.startswith("cloud_"):
                                _, blocker = cli._lock_voice_once({"voice": "TVB女声"})
                                self.assertEqual(blocker["status"], "ENV_ERROR")
                            path.write_bytes(original)
                            self.assertTrue(library_resources.catalog_report()["ready"])
                            self.assertTrue(voice_library.find_exact_name("TVB女声"))
                            self.assertTrue(bgm_selector.load_library())

    def test_build_rejects_missing_catalog_before_narration_and_vision(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "input.json"
            source.write_text(json.dumps({"task_id": "missing-catalog", "full_script": "测试。",
                                          "voice": "TVB女声", "videos": ["fixture.mp4"]}), encoding="utf-8")
            with patch.object(library_resources, "CATALOGS_ROOT", root / "missing"), \
                    patch.object(cli, "environment_report", return_value={"ready": True}), \
                    patch.object(cli, "_manifest_sources", return_value=[]), \
                    patch.object(cli, "_vision_configuration_gate", return_value=None), \
                    patch.object(cli, "_pre_synthesize_narration") as tts, \
                    patch.object(cli, "_temporary_match") as vision, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["build", "--input", str(source), "--report-dir", str(root / "report")]), 1)
            saved = json.loads((root / "report/last_result.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["status"], "ENV_ERROR")
            self.assertEqual(saved["reason"], "RUNTIME_CATALOG_MISSING:jianying_voice_catalog.csv")
            tts.assert_not_called()
            vision.assert_not_called()
