"""Real lookup metadata and early failure boundaries; no cloud or TTS calls."""
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
from orchestrator.audio import bgm_selector, library_resources, voice_catalog, voice_library
from orchestrator.runtime import headless


class RuntimeCatalogTests(unittest.TestCase):
    def test_real_official_names_preserve_voice_identity_and_tempo(self):
        ordinary = voice_catalog.resolve_voice("TVB女声1.2")
        professional = voice_catalog.resolve_voice("TVB女声Pro")
        self.assertEqual((ordinary.sami, ordinary.speed), ("BV409_streaming", 1.2))
        self.assertEqual(professional.sami, "zh_female_tvb_emo_v2_mars_bigtts")
        self.assertNotEqual(ordinary.sami, professional.sami)
        typed = voice_catalog.resolve_voice("tvb女生1.2")
        self.assertEqual((typed.sami, typed.speed), ("BV409_streaming", 1.2))
        self.assertEqual(voice_catalog.resolve_voice("台湾甜妹1.2").sami,
                         "zh_female_xiaohe_saturn_bigtts")
        self.assertTrue(voice_library.find_exact_name("憨熊"))

    def test_missing_or_invalid_tables_fail_after_warmup_and_repair_without_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "catalogs"
            shutil.copytree(library_resources.CATALOGS_ROOT, root)
            with patch.object(library_resources, "CATALOGS_ROOT", root), \
                    patch.object(cli, "environment_report", return_value={"ready": True}), \
                    patch.object(headless, "preflight", return_value={"status": "READY"}):
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
                            with contextlib.redirect_stdout(io.StringIO()) as output:
                                self.assertEqual(cli.main(["env"]), 1)
                            report = json.loads(output.getvalue())
                            self.assertEqual(report["reason"], f"RUNTIME_CATALOG_{code}:{name}")
                            self.assertEqual(report["runtime_catalogs"]["catalogs"][name]["status"], "ENV_ERROR")
                            command = "recommend-bgm" if name.startswith("cloud_") else "list-library"
                            with contextlib.redirect_stdout(io.StringIO()) as output:
                                self.assertEqual(cli.main([command]), 1)
                            self.assertEqual(json.loads(output.getvalue())["status"], "ENV_ERROR")
                            if not name.startswith("cloud_"):
                                _, blocker = cli._lock_voice_once({"voice": "TVB女声", "full_script": "测试。"})
                                self.assertEqual(blocker["status"], "ENV_ERROR")
                            path.write_bytes(original)
                            self.assertTrue(cli._production_environment_report({"ready": True})["ready"])

    def test_local_bgm_path_does_not_require_a_cloud_music_index(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name in ("jianying_voice_catalog.csv", "tts_speakers.csv"):
                shutil.copyfile(library_resources.CATALOGS_ROOT / name, root / name)
            with patch.object(library_resources, "CATALOGS_ROOT", root), \
                    patch.object(headless, "preflight", return_value={"status": "READY"}):
                for bgm, ready in (({"path": "local.wav"}, True), (None, False), ("music-id", False)):
                    with self.subTest(bgm=bgm):
                        report = cli._production_environment_report({"ready": True}, {"bgm": bgm})
                        self.assertEqual(report["ready"], ready)
                # Generic diagnostics deliberately do not require production metadata.
                with patch.object(cli, "environment_report", return_value={"ready": True}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["env", "--profile", "base"]), 0)

    def test_build_and_recovery_preflight_stop_before_paid_work_when_voice_table_is_missing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            manifest = root / "task.json"
            manifest.write_text(json.dumps({"task_id": "catalog-test", "full_script": "测试。",
                                            "voice": "TVB女声", "videos": ["fixture.mp4"]}), encoding="utf-8")
            with patch.object(library_resources, "CATALOGS_ROOT", root / "missing"), \
                    patch.object(headless, "preflight", return_value={"status": "READY"}), \
                    patch.object(cli, "environment_report", return_value={"ready": True}), \
                    patch.object(cli, "_manifest_sources", return_value=[]), \
                    patch.object(cli, "_vision_configuration_gate", return_value=None), \
                    patch.object(cli, "_pre_synthesize_narration") as tts, \
                    patch.object(cli, "_temporary_match") as vision:
                for command in ("build", "recover"):
                    arguments = [command, "--input", str(manifest), "--report-dir", str(root / command)]
                    if command == "recover":
                        arguments += ["--stage", "preflight"]
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(cli.main(arguments), 1)
                    saved = json.loads((root / command / "last_result.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved["status"], "ENV_ERROR")
                    self.assertEqual(saved["reason"], "RUNTIME_CATALOG_MISSING:jianying_voice_catalog.csv")
                tts.assert_not_called()
                vision.assert_not_called()
