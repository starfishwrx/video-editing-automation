"""Offline shortlist tests: no model download and no GPU or remote calls."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.local_retrieval import LocalRetriever, Siglip2Encoder, _pooled
from orchestrator.material_store import MaterialStore


class FakeEncoder:
    metadata = {"model_id": "offline-test", "resolved_revision": "frozen-commit",
                "dimension": 2, "preprocess_protocol": "test", "dtype": "float32"}

    def __init__(self):
        self.image_calls = self.text_calls = 0

    def encode_images(self, paths, *, batch_size):
        self.image_calls += 1
        return np.asarray([[1, 0] if "a.jpg" in p else [0, 1] for p in paths], dtype=np.float32)

    def encode_texts(self, texts):
        self.text_calls += 1
        return np.asarray([[1, 0] for text in texts], dtype=np.float32)


class LocalRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MaterialStore(self.root / "index")
        self.materials = []
        self.frames = []
        for index in range(3):
            video = self.root / f"{index}.mp4"
            video.write_bytes(f"video {index}".encode())
            product = "sku-a" if index < 2 else "sku-b"
            material_id = self.store.ingest_sources([video], product)[0]["material_id"]
            self.materials.append(material_id)
            image = self.root / f"frame-{index}.jpg"
            image.write_bytes(b"image data")
            self.frames.append(self.store.save_frame(material_id, 500_000, image))
        self.encoder = FakeEncoder()
        self.retriever = LocalRetriever(self.store, self.root / "weights", product_id="sku-a", encoder=self.encoder)

    def test_warm_index_uses_vectors_and_does_not_call_image_encoder(self):
        first = self.retriever.index_frames(self.frames[:2])
        self.assertEqual(first["encoded"], 2)
        self.encoder.encode_images = mock.Mock(side_effect=AssertionError("cached vectors must not encode"))
        second = self.retriever.index_frames(self.frames[:2])
        self.assertEqual(second["cache_hits"], 2)
        self.assertEqual(second["encoded"], 0)

    def test_retrieval_filters_both_sku_and_current_inventory(self):
        self.retriever.index_frames(self.frames[:2])
        other = LocalRetriever(self.store, self.root / "weights", product_id="sku-b", encoder=self.encoder)
        other.index_frames([self.frames[2]])
        result = self.retriever.retrieve("擦拭", [self.materials[1], self.materials[2]])
        self.assertEqual([row["material_id"] for row in result], [self.materials[1]])
        self.assertEqual(result[0]["verification_state"], "coarse_candidate")
        self.assertEqual(self.retriever.retrieve("擦拭", []), [])
        with self.assertRaises(ValueError):
            self.retriever.index_frames([self.frames[2]])

    def test_weight_revision_change_does_not_reuse_old_vectors(self):
        self.retriever.index_frames(self.frames[:2])
        updated = FakeEncoder()
        updated.metadata = {**updated.metadata, "resolved_revision": "different-commit"}
        retriever = LocalRetriever(self.store, self.root / "weights", product_id="sku-a", encoder=updated)
        stats = retriever.index_frames(self.frames[:2])
        self.assertEqual(stats["encoded"], 2)
        self.assertEqual(stats["cache_hits"], 0)

    def test_retrieval_ranks_by_normalized_similarity_and_caches_query(self):
        signature = self.retriever.model_signature
        for frame, vector in zip(self.frames[:2], ([5, 0], [0, 7])):
            self.store.save_vector(frame["frame_id"], signature["model_key"], vector,
                                   metadata=signature)
        result = self.retriever.retrieve("擦拭", self.materials[:2], topk=1)
        self.assertEqual(result[0]["material_id"], self.materials[0])
        self.assertAlmostEqual(result[0]["score"], 1.0)
        self.retriever.retrieve("擦拭", self.materials[:2], topk=2)
        self.assertEqual(self.encoder.text_calls, 1)

    def test_changed_frame_invalidates_only_its_vectors(self):
        self.retriever.index_frames(self.frames[:2])
        replacement = self.root / "replacement.jpg"
        replacement.write_bytes(b"new image data")
        self.store.save_frame(self.materials[0], 500_000, replacement)
        stats = self.retriever.index_frames(self.store.frames(self.materials[:2]))
        self.assertEqual(stats["cache_hits"], 1)
        self.assertEqual(stats["encoded"], 1)

    def test_pinned_metadata_does_not_load_model_to_check_existing_vectors(self):
        encoder = Siglip2Encoder(self.store, self.root / "weights")
        metadata = {**FakeEncoder.metadata, "model_id": encoder.model_id}
        self.store.save_model_signature(encoder._request_key, metadata)
        reloaded = Siglip2Encoder(self.store, self.root / "weights")
        with mock.patch.object(reloaded, "_load", side_effect=AssertionError("must not load")):
            self.assertEqual(reloaded.metadata["resolved_revision"], "frozen-commit")

    def test_transformers_feature_return_compatibility(self):
        tensor = object()
        pooled = type("Pooled", (), {"pooler_output": tensor})()
        self.assertIs(_pooled(tensor), tensor)
        self.assertIs(_pooled(pooled), tensor)


if __name__ == "__main__":
    unittest.main()
