import csv
from contextlib import closing
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import extract_voice_catalog as extractor
from orchestrator import voice_cache, voice_catalog, voice_library


def sample(*, has_more=False):
    def item(title, voice, effect):
        return {"common_attr": {"title": title, "effect_id": effect,
                "extra": json.dumps({"tonetype": json.dumps({"voice_type": voice})}),
                "tag_list": [{"name": "女"}]}}
    return {"ret": "0", "data": {"categories": [{"category_id": 1, "category_name": "女声"},
            {"category_id": 2, "category_name": "热门"}], "category_resources": {
            "1": {"has_more": has_more, "effect_item_list": [item("声音", "voice_a", "a"),
                                                                item("声音", "voice_b", "b")]},
            "2": {"has_more": False, "effect_item_list": [item("声音", "voice_a", "a")]}}}}


class ExtractVoiceCatalogTests(unittest.TestCase):
    def test_keeps_ambiguous_names_and_deduplicates_categories(self):
        rows, meta = extractor.parse_panel(json.dumps(sample(), ensure_ascii=False))
        self.assertEqual(meta["unique_voices"], 2)
        self.assertEqual({row["voice_type"] for row in rows}, {"voice_a", "voice_b"})
        self.assertIn("热门", next(x for x in rows if x["voice_type"] == "voice_a")["tags"])

    def test_rejects_incomplete_or_conflicting_panel(self):
        with self.assertRaisesRegex(ValueError, "INCOMPLETE"):
            extractor.parse_panel(json.dumps(sample(has_more=True)))
        data = sample()
        data["data"]["category_resources"]["2"]["effect_item_list"][0]["common_attr"]["effect_id"] = "different"
        with self.assertRaisesRegex(ValueError, "CONFLICT"):
            extractor.parse_panel(json.dumps(data))

    def test_latest_complete_cache_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "account" / "rp.db"
            db.parent.mkdir()
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("create table http_cache (url text, timestamp text, response_body text)")
                connection.executemany("insert into http_cache values (?, ?, ?)", [
                    (voice_cache.PANEL, "2026-10-27 00:00:00", json.dumps(sample())),
                    (voice_cache.PANEL, "2026-10-28 00:00:00", json.dumps(sample(has_more=True))),
                ])
                connection.commit()
            before = db.read_bytes()
            rows, meta = extractor.find_latest_panel(root)
            self.assertEqual(meta["cache_timestamp"], "2026-10-27 00:00:00")
            self.assertEqual(db.read_bytes(), before)
            out = root / "catalog.csv"
            extractor.write_catalog(rows, meta, out)
            lines = [line for line in out.read_text(encoding="utf8").splitlines() if not line.startswith("#")]
            parsed = list(csv.DictReader(lines))
            self.assertEqual({x["voice_type"] for x in parsed}, {"voice_a", "voice_b"})
            with self.assertRaises(FileExistsError):
                extractor.write_catalog(rows, meta, out)

    def test_missing_csv_uses_complete_local_cache_without_guessing(self):
        rows, meta = extractor.parse_panel(json.dumps(sample()))
        self.addCleanup(voice_library.load_library.cache_clear)
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": "test-local"}), \
             mock.patch.object(voice_library, "_catalog_path", return_value=None), \
             mock.patch.object(voice_library, "_csv_path", return_value=None), \
             mock.patch.object(voice_cache, "find_latest_panel", return_value=(rows, meta)):
            voice_library.load_library.cache_clear()
            self.assertEqual({v.speaker_id for v in voice_library.find_exact_name("声音")}, {"voice_a", "voice_b"})
            with self.assertRaises(voice_catalog.VoiceResolutionError):
                voice_catalog.resolve_voice("声音")
        with mock.patch.dict("os.environ", {"LOCALAPPDATA": "test-local"}), \
             mock.patch.object(voice_library, "_catalog_path", return_value=None), \
             mock.patch.object(voice_library, "_csv_path", return_value=None), \
             mock.patch.object(voice_cache, "find_latest_panel", side_effect=ValueError("incomplete")):
            voice_library.load_library.cache_clear()
            with self.assertRaises(voice_catalog.VoiceResolutionError):
                voice_catalog.resolve_voice("声音")


if __name__ == "__main__":
    unittest.main()
