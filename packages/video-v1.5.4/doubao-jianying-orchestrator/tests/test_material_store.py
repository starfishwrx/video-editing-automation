"""Offline identity, provenance and cross-task exclusion regressions."""
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.material_store import MaterialStore, MaterialLeaseTimeout


class MaterialStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MaterialStore(self.root / "index")
        self.source = self.root / "source.mp4"
        self.source.write_bytes(b"frozen media bytes")
        self.material_id = self.store.ingest_sources([self.source], "sku-a")[0]["material_id"]

    def test_copied_bytes_share_identity_and_unchanged_stat_skips_hash(self):
        duplicate = self.root / "other-name.mp4"
        shutil.copyfile(self.source, duplicate)
        row = self.store.ingest_sources([duplicate], "sku-a")[0]
        self.assertEqual(row["material_id"], self.material_id)
        with mock.patch("pathlib.Path.open", side_effect=AssertionError("must not read media bytes")):
            self.assertTrue(self.store.ingest_sources([duplicate], "sku-a")[0]["hash_reused"])

    def test_changed_path_does_not_reuse_old_identity(self):
        self.source.write_bytes(b"replaced media, different contents")
        self.assertIsNone(self.store.material_for_path(self.source, product_id="sku-a"))
        updated = self.store.ingest_sources([self.source], "sku-a")[0]
        self.assertFalse(updated["hash_reused"])
        self.assertNotEqual(updated["material_id"], self.material_id)

    def test_scope_requires_product_and_explicit_current_inventory(self):
        second = self.root / "second.mp4"
        second.write_bytes(b"different bytes")
        second_id = self.store.ingest_sources([second], "sku-b")[0]["material_id"]
        self.assertEqual(self.store.scoped_ids("sku-a", [self.material_id, second_id]), [self.material_id])
        self.assertEqual(self.store.scoped_ids("sku-a", []), [])
        with self.assertRaises(ValueError):
            self.store.get_observations(second_id, product_id="sku-a")

    def test_legacy_import_preserves_observation_not_stamped_rules_or_approval(self):
        image = self.root / "mid.jpg"
        image.write_bytes(b"representative jpeg")
        shot = {"video": str(self.source), "shot_id": "s1", "source_start": 0,
                "source_end": 4, "frame_path": str(image), "frame_time": 2,
                "description": "手擦去番茄酱，最后展示干净手掌", "action_complete": True,
                "action_segments": [{"start": 0.5, "end": 3.7, "action": "擦拭"}],
                "visual_tags": ["擦拭"], "rule_checks": ["new-unseen-rule"],
                "certification": "PASS", "human_review": "approved"}
        stats = self.store.import_analysis({"shots": [shot]}, {"product_id": "sku-a"})
        self.assertEqual(stats["imported"], 1)
        stored = self.store.get_observations(self.material_id, product_id="sku-a",
                                             requirements=["new-unseen-rule"])[0]
        self.assertFalse(stored["requirements_satisfied"])
        self.assertEqual(stored["verified_requirements"], [])
        self.assertNotIn("rule_checks", stored["observation"])
        self.assertNotIn("certification", stored["observation"])
        self.assertEqual(stored["observation"]["action_segments"], shot["action_segments"])
        self.assertEqual(self.store.get_derived_labels(stored["observation_id"])["legacy-import"]["visual_tags"], ["擦拭"])
        image.unlink()
        self.assertTrue(Path(stored["observation"]["frame_path"]).exists())

    def test_legacy_cache_without_source_attribution_is_not_imported(self):
        import json
        cache = self.root / "legacy.json"
        cache.write_text(json.dumps({"old-key": {"description": "擦拭", "source_end": 4}}), encoding="utf-8")
        self.assertEqual(self.store.import_legacy_cache(cache, {"product_id": "sku-a"})["imported"], 0)

    def test_import_keeps_viewed_sequence_and_does_not_invent_unknown_frame_times(self):
        paths = []
        for index in range(5):
            image = self.root / f"sequence_{index}.jpg"
            image.write_bytes(f"frame {index}".encode())
            paths.append(str(image))
        shot = {"video": str(self.source), "source_start": 0, "source_end": 4,
                "description": "full wipe sequence", "frame_path": paths[0], "frame_time": 2,
                "frame_paths": paths[:3],
                "vision_frames": [{"path": paths[3], "timestamp": 0.25},
                                  {"path": paths[4], "timestamp_us": 3_750_000}]}
        stats = self.store.import_analysis({"shots": [shot]}, {"product_id": "sku-a"})
        self.assertEqual(stats["frames"], 3)
        frames = self.store.get_frames(self.material_id)
        self.assertEqual([f["timestamp_us"] for f in frames], [250_000, 2_000_000, 3_750_000])
        observed = self.store.get_observations(self.material_id, product_id="sku-a")[0]
        self.assertEqual(len(observed["observation"]["vision_frames"]), 3)
        for original in paths:
            Path(original).unlink()
        self.assertEqual(len(self.store.get_frames(self.material_id)), 3)

    def test_new_requirement_only_passes_with_evidence_and_matching_protocol(self):
        with self.assertRaises(ValueError):
            self.store.save_observation(self.material_id, {"description": "擦拭"}, product_id="sku-a",
                                        end_us=4_000_000, verified_requirements=["clean-result"])
        saved = self.store.save_observation(self.material_id, {"description": "完整擦拭"},
            product_id="sku-a", end_us=4_000_000, state="verified",
            provenance={"kind": "visual-model", "evidence": ["frame-1", "frame-2"]},
            verified_requirements=["clean-result"])
        self.assertEqual(saved["state"], "verified")
        observation = self.store.get_observations(self.material_id, product_id="sku-a",
                                                   requirements=["clean-result", "opening"])[0]
        self.assertEqual(observation["missing_requirements"], ["opening"])
        self.assertEqual(self.store.get_observations(self.material_id, product_id="sku-a", protocol="other-v2"), [])

    def test_negative_observation_is_durable_and_not_forced_ready(self):
        self.store.save_observation(self.material_id, {"description": "只有包装，没有擦拭动作"},
                                    product_id="sku-a", end_us=4_000_000, state="unusable")
        other_process_store = MaterialStore(self.root / "index")
        self.assertEqual(other_process_store.get_observations(self.material_id, product_id="sku-a")[0]["state"], "unusable")

    def test_shared_lease_blocks_another_connection_and_is_released(self):
        other = MaterialStore(self.root / "index")
        with self.store.lease(self.material_id, timeout_s=0):
            with self.assertRaises(MaterialLeaseTimeout):
                with other.lease(self.material_id, timeout_s=0.01):
                    self.fail("lease was bypassed")
        with other.lease(self.material_id, timeout_s=0):
            pass

    def test_lease_heartbeat_prevents_takeover_after_original_expiry(self):
        other = MaterialStore(self.root / "index")
        with self.store.lease(self.material_id, timeout_s=0, lease_s=0.15):
            time.sleep(0.25)
            with self.assertRaises(MaterialLeaseTimeout):
                with other.lease(self.material_id, timeout_s=0):
                    self.fail("live lease expired")

    def test_lease_excludes_a_separate_python_process(self):
        code = "\n".join([
            "import sys",
            "from orchestrator.material_store import MaterialStore, MaterialLeaseTimeout",
            "store = MaterialStore(sys.argv[1])",
            "try:",
            "    with store.lease(sys.argv[2], timeout_s=0): pass",
            "except MaterialLeaseTimeout:",
            "    sys.exit(7)",
        ])
        with self.store.lease(self.material_id, timeout_s=0):
            result = subprocess.run([sys.executable, "-B", "-c", code,
                                     str(self.root / "index"), self.material_id],
                                    cwd=Path(__file__).resolve().parents[1],
                                    capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 7, result.stderr)


if __name__ == "__main__":
    unittest.main()
