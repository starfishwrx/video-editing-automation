"""Reusable media preparation for a product, before any model requests.

Entries use normal source stat metadata and the preparation parameters. They
are accelerators, not quality certificates; visual and text gates still run.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Callable

from . import ai_editor_contract as contract
from .file_safety import atomic_write_text, file_lock


def cache_root(report_dir: Path) -> Path:
    from . import vision_analyzer as vision
    return vision._vision_cache_path(report_dir).parent / "media"


def _read(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _save(path: Path, value) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def _present(paths) -> bool:
    paths = list(paths)
    return bool(paths) and all(Path(p).is_file() and Path(p).stat().st_size > 0 for p in paths)


def product_key(manifest: dict, sources: list[Path]) -> str:
    identity = {k: manifest.get(k) for k in ("product_key", "product_id", "product", "category", "sku")}
    if not any(identity.values()):
        identity["directories"] = sorted({str(p.parent) for p in sources})
    return contract.digest(identity)


def analyze_product(manifest: dict, base_dir: Path, report_dir: Path, *, ffmpeg=None,
                    ffprobe=None, threshold=.30, min_shot_s=.45, vision=True) -> dict:
    """Serialize one product's cold dependency, while reusing each unchanged source.

    The lock covers media preparation and observations. A waiter never starts
    cutting the same product while its producer is already calling the API.
    """
    from . import shot_analyzer, vision_analyzer
    from .runtime_policy import media_slot
    sources = shot_analyzer.manifest_sources(manifest, base_dir)
    root = cache_root(report_dir)
    product = product_key(manifest, sources)
    report_dir.mkdir(parents=True, exist_ok=True)
    with file_lock(root / "products" / product, timeout=float(os.environ.get("JY_VISION_CACHE_WAIT_TIMEOUT", "7200"))):
        shots, issues, inventory = [], [], []
        hits = 0
        for index, source in enumerate(sources, 1):
            try:
                signature = contract.source_identity(source)
            except OSError:
                issues.append({"type": "source_missing", "video": str(source)})
                continue
            identity = {"source": signature, "threshold": threshold, "min_shot_s": min_shot_s,
                        "protocol": "scene-representatives-us-v1"}
            key = contract.digest(identity)
            source_dir = root / "sources" / key
            path = source_dir / "prepared.json"
            with file_lock(path, timeout=7200):
                prepared = _read(path)
                if (isinstance(prepared, dict) and prepared.get("identity") == identity
                        and prepared.get("analysis", {}).get("ok")
                        and _present(p for row in prepared["analysis"].get("shots", [])
                                     for p in row.get("frame_paths", []))):
                    result = prepared["analysis"]
                    hits += 1
                else:
                    with media_slot(timeout=600) as handle:
                        if not handle.acquired:
                            raise TimeoutError("MEDIA_PREPARE_SLOT_TIMEOUT")
                        result = shot_analyzer.analyze_manifest(
                            {**manifest, "material_sources": [str(source)], "shot_analysis": None},
                            base_dir, source_dir, ffmpeg=ffmpeg, ffprobe=ffprobe,
                            threshold=threshold, min_shot_s=min_shot_s)
                    if signature != contract.source_identity(source):
                        raise ValueError("MATERIAL_SOURCE_CHANGED_DURING_PREPARATION")
                    if result.get("ok"):
                        _save(path, {"identity": identity, "analysis": result})
                for number, original in enumerate(result.get("shots", []), 1):
                    row = copy.deepcopy(original)
                    row["shot_id"] = f"source_{index:03d}_shot_{number:03d}"
                    shots.append(row)
                issues.extend(result.get("issues", []))
            inventory.append(signature)
        result = {"version": shot_analyzer._skill_version(), "scope": "shared_product",
                  "sources": [str(p) for p in sources], "shots": shots, "issues": issues,
                  "pending_items": list(issues), "library_write": [],
                  "analysis_prompt": shot_analyzer.build_analysis_prompt(manifest, shots),
                  "ok": bool(shots) and not issues}
        if vision and shots:
            result = vision_analyzer.enrich_with_vision(manifest, result, report_dir, ffmpeg=ffmpeg)
        complete = bool(shots) and not issues and all(
            vision_analyzer._reusable_visual_observation(row)
            and not vision_analyzer._needs_text_backfill(row) for row in result["shots"])
        result["material_cache"] = {"product_key": product, "source_inventory": inventory,
                                    "sources_hit": hits, "sources_prepared": len(inventory) - hits,
                                    "ready": complete if vision else bool(shots) and not issues}
        _save(report_dir / "shot_candidates.json", result)
        atomic_write_text(report_dir / "shot_analysis_prompt.md", result["analysis_prompt"])
        return result


def cached_frames(shot: dict, report_dir: Path, prepare: Callable[[Path], list[dict]], *,
                  protocol: str) -> list[dict]:
    identity = {"source": contract.source_identity(shot["video"]),
                "window": [shot["source_start"], shot["source_end"]], "protocol": protocol}
    directory = cache_root(report_dir) / "frames" / contract.digest(identity)
    path = directory / "frames.json"
    with file_lock(path, timeout=600):
        frames = _read(path)
        if isinstance(frames, list) and _present([f.get("path", "") for f in frames]):
            return frames
        directory.mkdir(parents=True, exist_ok=True)
        frames = prepare(directory)
        if identity["source"] != contract.source_identity(shot["video"]):
            raise ValueError("MATERIAL_SOURCE_CHANGED_DURING_PREPARATION")
        if frames and _present([f["path"] for f in frames]):
            _save(path, frames)
        return frames


def cached_sheet(items: list[dict], report_dir: Path, *, prefix: str, prepare, parameters: dict) -> Path | None:
    """Reuse the same labelled pixels; callers retain purpose-specific prompts."""
    identity = {"items": [{**row, "source_stat": contract.source_identity(row["path"])} for row in items],
                "parameters": parameters, "protocol": "labelled-sheet-v1"}
    path = cache_root(report_dir) / "sheets" / f"{prefix}_{contract.digest(identity)}.jpg"
    with file_lock(path, timeout=600):
        if _present([path]):
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        if prepare(path):
            return path
    return None
