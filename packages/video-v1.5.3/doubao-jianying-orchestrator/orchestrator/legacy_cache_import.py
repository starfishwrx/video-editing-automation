"""Explicit, provenance-checked import of old productless visual observations."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from . import vision_analyzer as vision
from .file_safety import locked_json_update


def import_productless_observations(prior_task: dict[str, Any], prior_manifest: dict[str, Any],
                                    current_manifest: dict[str, Any], analysis: dict[str, Any],
                                    cache_path: Path, *, profile: str = "volc",
                                    prior_base_dir: Path | None = None,
                                    current_base_dir: Path | None = None) -> dict[str, int]:
    """Import only exact-source/protocol observations with proven task identity.

    This is a one-time operator action, not implicit cross-product cache lookup.
    Existing keys win, and disabled/missing text stays unverified.
    """
    prior_product = str(prior_task.get("product") or "").strip()
    current_product = str(current_manifest.get("product_id") or current_manifest.get("product")
                          or current_manifest.get("brand") or "").strip()
    record = str(prior_task.get("record_id") or "").strip()
    if not prior_product or prior_product != current_product:
        raise ValueError("LEGACY_PRODUCT_IDENTITY_MISMATCH")
    if not record or record != str(prior_manifest.get("record_id") or "").strip():
        raise ValueError("LEGACY_TASK_IDENTITY_MISMATCH")
    if any(str(prior_manifest.get(k) or "").strip() for k in ("product_id", "product", "brand", "sku")):
        raise ValueError("LEGACY_MANIFEST_NOT_PRODUCTLESS")
    from .shot_analyzer import manifest_sources
    sources = {str(path).casefold() for path in
               manifest_sources(current_manifest, current_base_dir or Path.cwd())}
    prior_sources = {str(path).casefold() for path in
                     manifest_sources(prior_manifest, prior_base_dir or Path.cwd())}
    if not sources or not prior_sources:
        raise ValueError("MATERIAL_SOURCES_REQUIRED")
    without_product = dict(current_manifest, product_id="", product="", brand="")
    candidates: dict[str, dict[str, Any]] = {}
    rejected = 0
    for row in analysis.get("shots", []):
        if not isinstance(row, dict) or not str(row.get("video") or "").strip():
            rejected += 1
            continue
        source = str(Path(str(row["video"])).expanduser().resolve()).casefold()
        old_key = str(row.get("analysis_cache_key") or "")
        # Both identities must reproduce the original cache key. That proves
        # source stat, window, rules, sampling protocol, backend and model agree.
        if (source not in sources or source not in prior_sources or not old_key
                or vision._vision_cache_key(prior_manifest, row, profile) != old_key
                or vision._vision_cache_key(without_product, row, profile) != old_key):
            rejected += 1
            continue
        projection = vision._cache_projection(row)
        new_key = vision._vision_cache_key(current_manifest, row, profile)
        if projection is None or not new_key:
            rejected += 1
            continue
        candidates[new_key] = projection
    added = 0
    def merge(disk: Any) -> dict[str, Any]:
        nonlocal added
        if not isinstance(disk, dict):
            raise ValueError("INVALID_SHARED_VISION_CACHE")
        for key, value in candidates.items():
            if key not in disk:
                disk[key] = value
                added += 1
        return disk
    locked_json_update(cache_path, merge, default={}, timeout=120)
    return {"candidates": len(candidates), "added": added,
            "existing": len(candidates) - added, "rejected": rejected}


def main() -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior-task", required=True, type=Path)
    parser.add_argument("--prior-manifest", required=True, type=Path)
    parser.add_argument("--current-manifest", required=True, type=Path)
    parser.add_argument("--prior-analysis", required=True, type=Path)
    parser.add_argument("--cache", required=True, type=Path)
    parser.add_argument("--profile", default="volc")
    args = parser.parse_args()
    def read(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("JSON_OBJECT_REQUIRED")
        return value
    result = import_productless_observations(read(args.prior_task), read(args.prior_manifest),
        read(args.current_manifest), read(args.prior_analysis), args.cache, profile=args.profile,
        prior_base_dir=args.prior_manifest.resolve().parent,
        current_base_dir=args.current_manifest.resolve().parent)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
