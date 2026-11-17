"""Portable pair evidence must retain its visual and source boundaries."""
import copy
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

import test_experiment_pairs as fixture
from orchestrator import ai_editor_contract as identity
from orchestrator.selection import experiment_contract
from orchestrator.vision import evidence_store, experiment_evidence


class ExperimentEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.PairApplicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.base.root
        self.manifest = self.fixture.base.manifest
        self.pair = self.fixture.evidence
        self.env = patch.dict(os.environ, {"JY_MATERIAL_EVIDENCE_DB": str(self.root / "library" / "evidence.sqlite")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.protocol = patch.object(evidence_store, "protocol", return_value="test-model-policy")
        self.protocol.start()
        self.addCleanup(self.protocol.stop)
        self.samples = [m["samples"] for m in self.pair["members"]]
        self.windows = [m["window_frame_ids"] for m in self.pair["members"]]
        self.key = experiment_evidence.pair_key(self.manifest, self.samples, self.windows)

    def relocated(self):
        result = []
        for i, sample in enumerate(self.samples):
            folder = self.root / "moved" / str(i)
            folder.mkdir(parents=True)
            moved = copy.deepcopy(sample)
            for role in ("original", "source", "video"):
                target = folder / (role + ".mp4")
                shutil.copyfile(sample[role]["path"], target)
                moved[role] = identity.source_identity(target)
            (folder / "samples.json").write_text(json.dumps(moved), encoding="utf-8")
            result.append(moved)
        return result

    def test_rebinds_only_same_content_windows_and_product(self):
        experiment_evidence.remember_pair(self.manifest, self.key, self.pair)
        moved = self.relocated()
        target = {**self.manifest, "task_id": "next-day", "material_sources":
                  [s["original"]["path"] for s in moved]}
        self.assertEqual(self.key, experiment_evidence.pair_key(target, moved, self.windows))
        pair = experiment_evidence.reuse_pair(target, self.key, moved, self.root / "next")
        self.assertIsNotNone(pair)
        experiment_contract.validate_pair_evidence(target, pair, require_disjoint_windows=False)
        self.assertEqual(pair["task_id"], "next-day")
        self.assertFalse(pair["formal_delivery_allowed"])
        self.assertNotEqual(self.key, experiment_evidence.pair_key({**target, "product": "other"}, moved, self.windows))
        self.assertNotEqual(self.key, experiment_evidence.pair_key(
            {**target, "revision_feedback": "画面中的验证关系需重新核对"}, moved, self.windows))
        self.assertNotEqual(self.key, experiment_evidence.pair_key(target, moved, [[1, 20], self.windows[1]]))
        self.assertNotEqual(self.key, experiment_evidence.pair_key(target, moved[::-1], self.windows[::-1]))
        Path(moved[0]["original"]["path"]).write_bytes(b"different footage")
        self.assertNotEqual(self.key, experiment_evidence.pair_key(target, moved, self.windows))

    def test_unmatched_and_conflicting_reviews_never_reused(self):
        other = copy.deepcopy(self.pair)
        other["observation"]["verdict"] = "mismatch"
        other["observation"]["comparisons"]["setup"]["status"] = "conflict"
        experiment_evidence.remember_pair(self.manifest, self.key, other)
        self.assertIsNone(experiment_evidence.reuse_pair(self.manifest, self.key, self.samples, self.root / "none"))
        experiment_evidence.remember_pair(self.manifest, self.key, self.pair)
        self.assertIsNone(experiment_evidence.reuse_pair(self.manifest, self.key, self.samples, self.root / "conflict"))

    def test_missing_or_changed_visual_proof_cannot_be_reused(self):
        experiment_evidence.remember_pair(self.manifest, self.key, self.pair)
        with evidence_store.connect() as db:
            payload = json.loads(db.execute("SELECT payload FROM experiment_observations").fetchone()[0])
        Path(payload["sheet"]["path"]).write_bytes(b"changed pixels")
        self.assertIsNone(experiment_evidence.reuse_pair(self.manifest, self.key, self.samples, self.root / "unsafe"))

    def test_preferred_pair_is_only_a_scoped_ready_source_hint(self):
        experiment_evidence.remember_pair(self.manifest, self.key, self.pair)
        shots = {str(i): {"shot_id": str(i), "video": sample["original"]["path"],
                           "source_start": 0, "source_end": 3,
                           "source_sha256": evidence_store.identity(sample["original"]["path"])}
                 for i, sample in enumerate(self.samples)}
        self.assertEqual(experiment_evidence.preferred_pairs(self.manifest, shots), [("0", "1")])
        restricted = {**self.manifest, "material_sources": [self.samples[0]["original"]["path"]]}
        self.assertEqual(experiment_evidence.preferred_pairs(restricted, shots), [])
        self.assertEqual(experiment_evidence.preferred_pairs({**self.manifest, "product": "other"}, shots), [])
        shots["1"]["source_start"] = 1
        self.assertEqual(experiment_evidence.preferred_pairs(self.manifest, shots), [])


if __name__ == "__main__":
    unittest.main()
