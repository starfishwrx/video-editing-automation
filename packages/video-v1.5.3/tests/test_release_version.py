"""A package version upgrade must not regenerate the previous release identity."""
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


class ReleaseVersionTests(unittest.TestCase):
    @staticmethod
    def _resources(root, builder):
        for relative in builder.REQUIRED_RUNTIME_RESOURCES:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")

    def test_public_catalogs_are_shipped_and_private_runtime_directories_are_excluded(self):
        path = Path(__file__).resolve().parents[1] / "build_manifest.py"
        spec = importlib.util.spec_from_file_location("release_inventory_fixture", path)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            self._resources(root, builder)
            private = ("jianying-editor/data/cloud_music_library.csv", "jianying-editor/.cache/cloud/a.json",
                       "logs/a.json", "reports/a.json", ".jy_run/a.json",
                       "doubao-jianying-orchestrator/bin/ffmpeg.exe")
            for relative in private:
                cache = root / relative
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text("private fixture\n", encoding="utf-8")
            with mock.patch.object(builder, "ROOT", root), redirect_stdout(io.StringIO()):
                builder.main()
            result = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
            included = {row["path"] for row in result["files"]}
            self.assertFalse(included.intersection(private))
            self.assertTrue(set(builder.REQUIRED_RUNTIME_RESOURCES).issubset(included))
            self.assertTrue(all((root / relative).is_file() for relative in private))

    def test_manifest_identity_comes_from_the_package_version_file(self):
        path = Path(__file__).resolve().parents[1] / "build_manifest.py"
        spec = importlib.util.spec_from_file_location("release_version_fixture", path)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            self._resources(root, builder)
            with mock.patch.object(builder, "ROOT", root), redirect_stdout(io.StringIO()):
                builder.main()
            manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "9.8.7")
        self.assertEqual(manifest["release_id"], "video-v9.8.7")

    def test_missing_catalog_cannot_be_blessed_by_regenerating_the_manifest(self):
        path = Path(__file__).resolve().parents[1] / "build_manifest.py"
        spec = importlib.util.spec_from_file_location("release_resource_fixture", path)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(builder, "ROOT", Path(tmp)):
            for relative in builder.REQUIRED_RUNTIME_RESOURCES:
                self._resources(builder.ROOT, builder)
                (builder.ROOT / relative).unlink()
                with self.subTest(relative=relative), self.assertRaisesRegex(ValueError, "PACKAGE_RUNTIME_RESOURCE_MISSING"):
                    builder.main()


if __name__ == "__main__":
    unittest.main()
