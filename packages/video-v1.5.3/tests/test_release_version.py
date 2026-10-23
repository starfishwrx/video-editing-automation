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
    def test_local_synced_voice_and_music_tables_stay_outside_the_release(self):
        path = Path(__file__).resolve().parents[1] / "build_manifest.py"
        spec = importlib.util.spec_from_file_location("release_inventory_fixture", path)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            cache = root / "jianying-editor/data/cloud_music_library.csv"
            cache.parent.mkdir(parents=True)
            cache.write_text("local cached catalogue\n", encoding="utf-8")
            with mock.patch.object(builder, "ROOT", root), redirect_stdout(io.StringIO()):
                builder.main()
            result = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("jianying-editor/data/cloud_music_library.csv",
                             [row["path"] for row in result["files"]])
            self.assertEqual(cache.read_text(encoding="utf-8"), "local cached catalogue\n")

    def test_manifest_identity_comes_from_the_package_version_file(self):
        path = Path(__file__).resolve().parents[1] / "build_manifest.py"
        spec = importlib.util.spec_from_file_location("release_version_fixture", path)
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "VERSION").write_text("9.8.7\n", encoding="utf-8")
            with mock.patch.object(builder, "ROOT", root), redirect_stdout(io.StringIO()):
                builder.main()
            manifest = json.loads((root / "release-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], "9.8.7")
        self.assertEqual(manifest["release_id"], "video-v9.8.7")


if __name__ == "__main__":
    unittest.main()
