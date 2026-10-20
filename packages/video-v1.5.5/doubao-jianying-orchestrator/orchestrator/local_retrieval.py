"""Local SigLIP 2 shortlist retrieval; scores never certify visual claims.

Transformers API reference:
https://huggingface.co/docs/transformers/model_doc/siglip2
Supports both Tensor feature returns (4.x) and pooled model outputs (5.x).
Heavy dependencies and model weights load only when missing work requires them.
"""
from __future__ import annotations

import hashlib
import contextlib
import json
import time
from pathlib import Path
from typing import Any, Iterable

from .material_store import MaterialStore


DEFAULT_MODEL = "google/siglip2-base-patch16-224"
PREPROCESS_PROTOCOL = "siglip2-fixres224-lowercase-maxlength64-v1"


def _key(metadata: dict[str, Any]) -> str:
    fields = {name: metadata[name] for name in (
        "model_id", "resolved_revision", "dimension", "preprocess_protocol", "dtype")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


def _normalized(value: Any):
    import numpy as np
    result = np.asarray(value, dtype=np.float32)
    if result.ndim == 1:
        result = result[None, :]
    norm = np.linalg.norm(result, axis=1, keepdims=True)
    if not np.isfinite(result).all() or (norm <= 0).any():
        raise ValueError("encoder produced non-finite or zero embeddings")
    return result / norm


def _pooled(features: Any):
    if hasattr(features, "pooler_output"):
        return features.pooler_output
    return features


class Siglip2Encoder:
    def __init__(self, store: MaterialStore, model_cache_dir: str | Path, *,
                 model_id: str = DEFAULT_MODEL, revision: str = "main",
                 device: str | None = None):
        self.store = store
        self.model_cache_dir = Path(model_cache_dir).expanduser().resolve()
        self.model_id = model_id
        self.revision = revision
        self.requested_device = device
        self._request_key = json.dumps([model_id, revision, PREPROCESS_PROTOCOL, device], sort_keys=True)
        self._metadata = store.get_model_signature(self._request_key)
        self._model = self._processor = self._torch = None
        self.last_batch_size = 0
        self.load_seconds = 0.0

    @property
    def metadata(self) -> dict[str, Any]:
        if not self._metadata:
            self._load()
        return dict(self._metadata)

    def _load(self):
        if self._model is not None:
            return
        started = time.perf_counter()
        try:
            import torch
            from transformers import AutoModel, AutoProcessor
        except ImportError as exc:
            raise RuntimeError("local retrieval requires the UV indexed dependency group (torch, transformers, sentencepiece)") from exc
        self.model_cache_dir.mkdir(parents=True, exist_ok=True)
        device = self.requested_device or ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = torch.float16 if device.startswith("cuda") else torch.float32
        pinned = (self._metadata or {}).get("resolved_revision") or self.revision
        from huggingface_hub import try_to_load_from_cache
        local_weights = try_to_load_from_cache(self.model_id, "model.safetensors",
                                               revision=pinned, cache_dir=str(self.model_cache_dir))
        cached = isinstance(local_weights, str) and Path(local_weights).is_file()
        # The model commit, processor commit and cached vectors must agree.
        model = AutoModel.from_pretrained(self.model_id, revision=pinned,
                                          cache_dir=str(self.model_cache_dir),
                                          torch_dtype=dtype, attn_implementation="sdpa", local_files_only=cached)
        resolved = str(getattr(model.config, "_commit_hash", "") or "")
        if not resolved:
            if len(pinned) == 40 and all(c in "0123456789abcdef" for c in pinned.lower()):
                resolved = pinned
            else:
                raise RuntimeError("model revision could not be pinned; refusing mutable vector identity")
        if self._metadata and resolved != self._metadata["resolved_revision"]:
            raise RuntimeError("loaded model differs from the indexed model revision")
        processor = AutoProcessor.from_pretrained(self.model_id, revision=resolved,
                                                   cache_dir=str(self.model_cache_dir), local_files_only=cached,
                                                   use_fast=False)
        model = model.to(device).eval()
        dimension = int(model.config.text_config.hidden_size)
        metadata = {"model_id": self.model_id, "requested_revision": self.revision,
                    "resolved_revision": resolved, "dimension": dimension,
                    "preprocess_protocol": PREPROCESS_PROTOCOL,
                    "device": device, "dtype": str(dtype).replace("torch.", ""),
                    "transformers_version": __import__("transformers").__version__}
        if self._metadata and _key(metadata) != _key(self._metadata):
            raise RuntimeError("model/device precision signature changed; use a fresh index or explicit device")
        self._metadata = metadata
        self.store.save_model_signature(self._request_key, metadata)
        self._model, self._processor, self._torch = model, processor, torch
        self.load_seconds += time.perf_counter() - started

    def _inputs_to_device(self, inputs):
        torch = self._torch
        return {name: value.to(device=self._model.device,
                               dtype=self._model.dtype if torch.is_floating_point(value) else value.dtype)
                for name, value in inputs.items()}

    def encode_images(self, paths: list[str], *, batch_size: int = 8):
        self._load()
        from PIL import Image
        import numpy as np
        torch = self._torch
        effective = max(1, int(batch_size))
        outputs = []
        offset = 0
        while offset < len(paths):
            images = []
            try:
                for path in paths[offset:offset + effective]:
                    with Image.open(path) as source:
                        images.append(source.convert("RGB"))
                inputs = self._processor(images=images, return_tensors="pt")
                inputs = self._inputs_to_device(inputs)
                with torch.inference_mode():
                    features = _pooled(self._model.get_image_features(**inputs))
                result = features.detach().float().cpu().numpy()
                if result.shape[1] != self.metadata["dimension"]:
                    raise RuntimeError("image encoder dimension differs from model signature")
                outputs.append(_normalized(result))
                offset += len(images)
                self.last_batch_size = effective
            except RuntimeError as exc:
                oom = isinstance(exc, torch.cuda.OutOfMemoryError) or "out of memory" in str(exc).lower()
                if not oom or not str(self._model.device).startswith("cuda") or effective == 1:
                    raise
                effective = max(1, effective // 2)
                # Drop the failed batch before retrying a smaller batch.
                inputs = None
                features = None
                torch.cuda.empty_cache()
            finally:
                for image in images:
                    image.close()
        return np.concatenate(outputs, axis=0) if outputs else np.empty((0, self.metadata["dimension"]), dtype=np.float32)

    def encode_texts(self, texts: list[str]):
        self._load()
        # Older processors do not enforce all of these training-time defaults.
        inputs = self._processor(text=[str(text).lower() for text in texts],
                                 padding="max_length", truncation=True, max_length=64,
                                 return_tensors="pt")
        inputs = self._inputs_to_device(inputs)
        with self._torch.inference_mode():
            features = _pooled(self._model.get_text_features(**inputs))
        result = features.detach().float().cpu().numpy()
        if result.shape[1] != self.metadata["dimension"]:
            raise RuntimeError("text encoder dimension differs from model signature")
        return _normalized(result)


class LocalRetriever:
    def __init__(self, store: MaterialStore, model_cache_dir: str | Path, *,
                 product_id: str, revision: str = "main", batch_size: int = 8,
                 device: str | None = None, encoder: Any = None):
        if not product_id:
            raise ValueError("product_id is required for retrieval")
        self.store = store
        self.product_id = product_id
        self.batch_size = max(1, int(batch_size))
        self.encoder = encoder or Siglip2Encoder(store, model_cache_dir, revision=revision, device=device)
        self._text_cache: dict[str, Any] = {}
        self._ranking_cache: dict[Any, Any] = {}

    @property
    def metadata(self) -> dict[str, Any]:
        return self.encoder.metadata

    @property
    def model_signature(self) -> dict[str, Any]:
        return {**self.metadata, "model_key": _key(self.metadata)}

    def text_encoder(self, texts: list[str]):
        return self.encoder.encode_texts(texts)

    def index_frames(self, frame_rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
        self._ranking_cache.clear()
        started = time.perf_counter()
        rows = list(frame_rows)
        scoped = set(self.store.scoped_ids(self.product_id, [row["material_id"] for row in rows]))
        if any(row["material_id"] not in scoped for row in rows):
            raise ValueError("frame is outside the product scope")
        metadata = self.metadata
        model_key = _key(metadata)
        stats = {"frames": len(rows), "cache_hits": 0, "encoded": 0,
                 "model": {**metadata, "model_key": model_key}}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["material_id"], []).append(row)
        material_ids = sorted(grouped)
        for offset in range(0, len(material_ids), self.batch_size):
            # Canonical lock order avoids deadlock across tasks with overlapping
            # inventories, while batching frames across short source files.
            chunk_ids = material_ids[offset:offset + self.batch_size]
            with contextlib.ExitStack() as leases:
                for material_id in chunk_ids:
                    leases.enter_context(self.store.lease(material_id, f"embedding:{model_key}"))
                material_rows = [row for material_id in chunk_ids for row in grouped[material_id]]
                missing = []
                seen = set()
                with self.store.batch():
                    for row in material_rows:
                        if row["frame_id"] in seen:
                            continue
                        seen.add(row["frame_id"])
                        vector = self.store.get_vector(row["frame_id"], model_key)
                        if vector is not None and vector.shape == (metadata["dimension"],):
                            stats["cache_hits"] += 1
                        else:
                            missing.append(row)
                if missing:
                    values = self.encoder.encode_images([row["path"] for row in missing], batch_size=self.batch_size)
                    if len(values) != len(missing):
                        raise RuntimeError("encoder returned an incomplete frame batch")
                    with self.store.batch():
                        for row, vector in zip(missing, values):
                            if vector.shape != (metadata["dimension"],):
                                raise RuntimeError("encoder returned a mismatched vector dimension")
                            self.store.save_vector(row["frame_id"], model_key, vector, metadata=metadata)
                            stats["encoded"] += 1
        stats["elapsed_s"] = round(time.perf_counter() - started, 6)
        stats["effective_batch_size"] = getattr(self.encoder, "last_batch_size", self.batch_size)
        stats["model_load_s"] = getattr(self.encoder, "load_seconds", 0.0)
        return stats

    def retrieve(self, query: str, allowed_material_ids: Iterable[str], topk: int = 8) -> list[dict[str, Any]]:
        import numpy as np
        if topk <= 0:
            return []
        allowed = self.store.scoped_ids(self.product_id, allowed_material_ids)
        if not allowed:
            return []
        model_key = _key(self.metadata)
        cache_key = f"{model_key}:{query}"
        if cache_key not in self._text_cache:
            self._text_cache[cache_key] = _normalized(self.text_encoder([query]))[0]
        query_vector = self._text_cache[cache_key]
        snapshot_key = (model_key, tuple(allowed))
        snapshot = self._ranking_cache.get(snapshot_key)
        if snapshot:
            rows, matrix, signatures = snapshot
            try:
                current = [(Path(row["path"]).stat().st_mtime_ns, Path(row["path"]).stat().st_size) for row in rows]
            except OSError:
                current = None
            if current != signatures:
                snapshot = None
        if not snapshot:
            rows, vectors = [], []
            with self.store.batch():
                for row in self.store.frames(allowed):
                    vector = self.store.get_vector(row["frame_id"], model_key)
                    if vector is not None and vector.shape == query_vector.shape:
                        rows.append(row)
                        vectors.append(vector)
            matrix = np.stack(vectors) if vectors else np.empty((0, len(query_vector)), dtype=np.float32)
            signatures = [(Path(row["path"]).stat().st_mtime_ns, Path(row["path"]).stat().st_size) for row in rows]
            self._ranking_cache[snapshot_key] = (rows, matrix, signatures)
        best: dict[str, dict[str, Any]] = {}
        for row, score_value in zip(rows, matrix @ query_vector):
            score = float(score_value)
            candidate = {**row, "score": score, "model_key": model_key,
                         "verification_state": "coarse_candidate"}
            if row["material_id"] not in best or score > best[row["material_id"]]["score"]:
                best[row["material_id"]] = candidate
        return sorted(best.values(), key=lambda row: (-row["score"], row["material_id"]))[:topk]
