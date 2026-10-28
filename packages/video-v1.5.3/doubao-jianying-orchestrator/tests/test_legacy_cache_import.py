import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import legacy_cache_import, vision_analyzer as vision


class LegacyCacheImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"source")
        self.cache = self.root / "cache.json"
        self.task = {"record_id": "old-record", "product": "product"}
        self.prior = {"record_id": "old-record", "material_sources": [str(self.video)]}
        self.current = {"product": "product", "material_sources": [str(self.video)]}
        self.row = {"shot_id": "shot", "video": str(self.video), "source_start": 0,
                    "source_end": 2, "status": "ready_for_matching", "action_complete": True,
                    "description": "observed", "text_pass": "disabled"}
        self.row["analysis_cache_key"] = vision._vision_cache_key(self.prior, self.row, "volc")

    def run_import(self):
        return legacy_cache_import.import_productless_observations(
            self.task, self.prior, self.current, {"shots": [self.row]}, self.cache)

    def test_same_source_protocol_and_proven_product_import_without_text_promotion(self):
        result = self.run_import()
        self.assertEqual(result["added"], 1)
        cache = json.loads(self.cache.read_text(encoding="utf-8"))
        key = vision._vision_cache_key(self.current, self.row, "volc")
        self.assertEqual(cache[key]["text_pass"], "disabled")
        self.assertTrue(vision._needs_text_backfill(cache[key]))
        self.assertEqual(self.run_import()["existing"], 1)

    def test_wrong_product_or_task_never_imports(self):
        self.task["product"] = "different"
        with self.assertRaisesRegex(ValueError, "PRODUCT_IDENTITY"):
            self.run_import()
        self.task["product"] = "product"
        self.prior["record_id"] = "different"
        with self.assertRaisesRegex(ValueError, "TASK_IDENTITY"):
            self.run_import()
        self.assertFalse(self.cache.exists())

    def test_changed_source_window_or_protocol_never_imports(self):
        self.video.write_bytes(b"changed source bytes")
        self.assertEqual(self.run_import()["rejected"], 1)
        self.row["analysis_cache_key"] = vision._vision_cache_key(self.prior, self.row, "volc")
        self.current["sku"] = "different-sku"
        self.assertEqual(self.run_import()["rejected"], 1)

    def test_source_must_also_belong_to_prior_task(self):
        other = self.root / "other.mp4"
        other.write_bytes(b"other")
        self.prior["material_sources"] = [str(other)]
        self.assertEqual(self.run_import()["rejected"], 1)

    def test_relative_sources_use_each_manifest_directory(self):
        prior_dir = self.root / "prior"
        current_dir = self.root / "current"
        prior_dir.mkdir()
        current_dir.mkdir()
        self.prior["material_sources"] = ["../video.mp4"]
        self.current["material_sources"] = ["../video.mp4"]
        result = legacy_cache_import.import_productless_observations(
            self.task, self.prior, self.current, {"shots": [self.row]}, self.cache,
            prior_base_dir=prior_dir, current_base_dir=current_dir)
        self.assertEqual(result["added"], 1)
        wrong = current_dir / "video.mp4"
        wrong.write_bytes(b"different inventory")
        self.current["material_sources"] = ["video.mp4"]
        result = legacy_cache_import.import_productless_observations(
            self.task, self.prior, self.current, {"shots": [self.row]}, self.cache,
            prior_base_dir=prior_dir, current_base_dir=current_dir)
        self.assertEqual(result["rejected"], 1)

    def test_existing_current_result_wins(self):
        key = vision._vision_cache_key(self.current, self.row, "volc")
        self.cache.write_text(json.dumps({key: {"text_pass": "hires"}}), encoding="utf-8")
        self.assertEqual(self.run_import()["existing"], 1)
        self.assertEqual(json.loads(self.cache.read_text(encoding="utf-8"))[key]["text_pass"], "hires")


if __name__ == "__main__":
    unittest.main()
