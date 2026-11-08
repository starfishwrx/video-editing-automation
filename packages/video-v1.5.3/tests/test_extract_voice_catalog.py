import csv
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import extract_voice_catalog as extractor


def panel(*, has_more=False):
    def item(title, voice, effect):
        return {"common_attr": {"title": title, "effect_id": effect,
                "extra": json.dumps({"tonetype": json.dumps({"voice_type": voice})}),
                "tag_list": [{"name": "女"}]}}
    return {"ret": "0", "data": {
        "categories": [{"category_id": 1, "category_name": "女声"},
                       {"category_id": 2, "category_name": "热门"}],
        "category_resources": {
            "1": {"has_more": has_more, "effect_item_list": [
                item("同名", "voice_a", "a"), item("同名", "voice_b", "b")]},
            "2": {"has_more": False, "effect_item_list": [item("同名", "voice_a", "a")]}}}}


class ExtractVoiceCatalogTests(unittest.TestCase):
    def test_preserves_ambiguous_names_and_merges_categories(self):
        rows, summary = extractor.parse_panel(json.dumps(panel(), ensure_ascii=False))
        self.assertEqual(summary["unique_voices"], 2)
        self.assertEqual({row["voice_type"] for row in rows}, {"voice_a", "voice_b"})
        self.assertIn("热门", next(row for row in rows if row["voice_type"] == "voice_a")["tags"])

    def test_incomplete_or_conflicting_data_is_not_exported(self):
        with self.assertRaisesRegex(ValueError, "INCOMPLETE"):
            extractor.parse_panel(json.dumps(panel(has_more=True)))
        data = panel()
        data["data"]["category_resources"]["2"]["effect_item_list"][0]["common_attr"]["effect_id"] = "other"
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            extractor.parse_panel(json.dumps(data))
        data = panel()
        del data["data"]["category_resources"]["2"]
        with self.assertRaisesRegex(ValueError, "CATEGORY_MISSING"):
            extractor.parse_panel(json.dumps(data))

    def test_read_only_latest_complete_snapshot_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "cache"
            db = cache / "fixture" / "rp.db"
            db.parent.mkdir(parents=True)
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TABLE http_cache (url text, timestamp text, response_body text)")
                connection.executemany("INSERT INTO http_cache VALUES (?, ?, ?)", [
                    (extractor.PANEL, "2026-10-27 00:00:00", json.dumps(panel())),
                    (extractor.PANEL, "2026-10-28 00:00:00", json.dumps(panel(has_more=True)))])
                connection.commit()
            original = db.read_bytes()
            rows, summary = extractor.find_latest_panel(cache)
            self.assertEqual(summary["cache_timestamp"], "2026-10-27 00:00:00")
            self.assertEqual(db.read_bytes(), original)
            output = root / "review.csv"
            extractor.write_snapshot(rows, summary, output, cache_root=cache)
            lines = [line for line in output.read_text(encoding="utf-8").splitlines() if not line.startswith("#")]
            self.assertEqual({row["voice_type"] for row in csv.DictReader(lines)}, {"voice_a", "voice_b"})
            with self.assertRaises(FileExistsError):
                extractor.write_snapshot(rows, summary, output, cache_root=cache)
            with self.assertRaisesRegex(ValueError, "OUTPUT_LOCATION_INVALID"):
                extractor.write_snapshot(rows, summary, cache / "new.csv", cache_root=cache)
            with mock.patch.object(extractor, "PACKAGED_CATALOGS", root / "runtime"):
                with self.assertRaisesRegex(ValueError, "OUTPUT_LOCATION_INVALID"):
                    extractor.write_snapshot(rows, summary, root / "runtime" / "missing.csv", cache_root=cache)
            self.assertEqual(db.read_bytes(), original)

    def test_missing_or_invalid_cache_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "COMPLETE_CACHE_NOT_FOUND"):
                extractor.find_latest_panel(Path(tmp))

    def test_snapshot_rejects_spreadsheet_formulas_and_control_characters(self):
        rows, summary = extractor.parse_panel(json.dumps(panel(), ensure_ascii=False))
        summary["cache_timestamp"] = "2026-10-27 00:00:00"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for field in extractor.FIELDS:
                for unsafe in ("=1+1", "  +1", " -1", "\t@cmd", "text\nnext"):
                    with self.subTest(field=field, unsafe=unsafe):
                        changed = [dict(row) for row in rows]
                        changed[0][field] = unsafe
                        output = root / "should-not-exist.csv"
                        with self.assertRaisesRegex(ValueError, "UNSAFE_SPREADSHEET_CELL"):
                            extractor.write_snapshot(changed, summary, output, cache_root=root / "cache")
                        self.assertFalse(output.exists())

    def test_snapshot_quotes_untrusted_timestamp_in_comment(self):
        rows, summary = extractor.parse_panel(json.dumps(panel(), ensure_ascii=False))
        summary["cache_timestamp"] = "now\n=1+1"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "snapshot.csv"
            extractor.write_snapshot(rows, summary, output, cache_root=root / "cache")
            self.assertNotIn("\n=1+1", output.read_text(encoding="utf-8"))

    def test_timestamp_order_and_equal_time_conflicts_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "fixture" / "rp.db"
            db.parent.mkdir()
            newer = panel()
            newer["data"]["category_resources"]["1"]["effect_item_list"][1]["common_attr"]["title"] = "更新"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TABLE http_cache (url text, timestamp text, response_body text)")
                connection.executemany("INSERT INTO http_cache VALUES (?, ?, ?)", [
                    (extractor.PANEL, "9", json.dumps(panel())),
                    (extractor.PANEL, "10", json.dumps(newer))])
                connection.commit()
            rows, meta = extractor.find_latest_panel(root)
            self.assertEqual(meta["cache_timestamp"], "10")
            self.assertIn("更新", {row["title"] for row in rows})
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("INSERT INTO http_cache VALUES (?, ?, ?)",
                                   (extractor.PANEL, "10", json.dumps(panel())))
                connection.commit()
            with self.assertRaisesRegex(ValueError, "TIMESTAMP_CONFLICT"):
                extractor.find_latest_panel(root)

    def test_command_requires_explicit_cache_and_writes_only_selected_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "cache" / "fixture" / "rp.db"
            db.parent.mkdir(parents=True)
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TABLE http_cache (url text, timestamp text, response_body text)")
                connection.execute("INSERT INTO http_cache VALUES (?, ?, ?)", (
                    extractor.PANEL, "2026-10-27 00:00:00", json.dumps(panel(), ensure_ascii=False)))
                connection.commit()
            output = root / "snapshot.csv"
            result = subprocess.run([sys.executable, "-m", "tools.extract_voice_catalog",
                "--cache-root", str(root / "cache"), "--output", str(output)],
                cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["unique_voices"], 2)
            self.assertNotIn("同名", result.stdout)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
