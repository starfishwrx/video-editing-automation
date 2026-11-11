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

from .. import ai_editor_contract as contract
from ..runtime.file_safety import atomic_write_text, file_lock

DURATION_CONTRACT = "video-stream-two-writers-v1"


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


def _refresh_duration(prepared: dict, source: Path, signature: dict, *, ffprobe, ffmpeg) -> dict | None:
    """Refresh only the media-duration ruler; keep cached pixel observations.

    A sub-millisecond legacy rounding difference does not alter any cached
    representative frame. The solver receives the exact source limit and must
    re-review evidence that crossed it. A larger discrepancy invalidates this
    source's preparation, rather than silently trusting stale scene geometry.
    """
    from ..selection import timing_contract
    if prepared.get("duration_contract") == DURATION_CONTRACT or not ffprobe:
        return prepared
    probe = timing_contract.probe_material_duration_us(source, ffprobe=ffprobe, ffmpeg=ffmpeg)
    duration_us = int(probe.get("duration_us") or 0)
    if duration_us <= 0:
        raise ValueError("MATERIAL_DURATION_PROBE_FAILED")
    if probe.get("confidence") != timing_contract.PROBE_CONFIDENCE_HIGH:
        raise ValueError("MATERIAL_DURATION_BOUND_UNCONFIRMED")
    if signature != contract.source_identity(source):
        raise ValueError("MATERIAL_SOURCE_CHANGED_DURING_PREPARATION")
    rows = prepared.get("analysis", {}).get("shots", [])
    if any(round(float(row.get("source_end") or 0) * 1_000_000) > duration_us + 1000 for row in rows):
        return None
    refreshed = copy.deepcopy(prepared)
    for row in refreshed["analysis"].get("shots", []):
        row["material_duration_us"] = duration_us
        row["material_duration_source"] = probe.get("source")
        row["material_duration_confidence"] = probe.get("confidence")
    refreshed["duration_contract"] = DURATION_CONTRACT
    return refreshed


def refresh_external_analysis(analysis: dict, base_dir: Path, *, ffprobe, ffmpeg) -> dict:
    """Recheck supplied/recovery candidates without revisiting visual observations."""
    shots = analysis.get("shots") or []
    if not shots:
        return analysis
    if not ffprobe:
        raise ValueError("MATERIAL_DURATION_PROBE_REQUIRED")
    refreshed = copy.deepcopy(analysis)
    groups = {}
    for row in refreshed["shots"]:
        if not isinstance(row, dict) or not str(row.get("video") or "").strip():
            raise ValueError("MATERIAL_SOURCE_MISSING")
        source = Path(str(row["video"])).expanduser()
        source = (source if source.is_absolute() else base_dir / source).resolve()
        row["video"] = str(source)
        groups.setdefault(source, []).append(row)
    for source, rows in groups.items():
        signature = contract.source_identity(source)
        result = _refresh_duration({"analysis": {"shots": rows}}, source, signature,
                                   ffprobe=ffprobe, ffmpeg=ffmpeg)
        if result is None:
            raise ValueError(f"MATERIAL_GEOMETRY_REQUIRES_REANALYSIS:{source}")
        for row, updated in zip(rows, result["analysis"]["shots"]):
            row.update({key: updated[key] for key in ("material_duration_us", "material_duration_source",
                                                       "material_duration_confidence")})
    return refreshed


def _restore_product_observations(manifest, result, prior):
    """Seed unchanged shot facts when a rerun has only its product report."""
    from . import vision_analyzer as vision
    if not isinstance(prior, dict):
        return
    previous = {row.get("shot_id"): row for row in prior.get("shots") or [] if isinstance(row, dict)}
    exclusions = vision.logged_pool_exclusions(prior)
    warnings = {w.get("shot_id"): w for w in prior.get("warnings") or []
                if isinstance(w, dict) and w.get("type") == vision.UNAUDITABLE_WARNING}
    for shot in result["shots"]:
        sid = shot["shot_id"]
        old = previous.get(sid)
        if not old:
            continue
        key = vision._vision_cache_key(manifest, shot)
        if not key:
            continue
        if sid in exclusions and old.get("exclusion_cache_key") == key:
            for field in ("excluded_from_pool", "exclusion_cache_key", "exclusion_reason"):
                shot[field] = old.get(field)
            result["warnings"].append(warnings[sid])
        elif old.get("analysis_cache_key") == key:
            facts = vision._cache_projection(old)
            if facts:
                vision._apply_cached_visual(shot, facts, key)


def analyze_product(manifest: dict, base_dir: Path, report_dir: Path, *, ffmpeg=None,
                    ffprobe=None, threshold=.30, min_shot_s=.45, vision=True) -> dict:
    """Serialize one product's cold dependency, while reusing each unchanged source.

    The lock covers media preparation and observations. A waiter never starts
    cutting the same product while its producer is already calling the API.
    """
    from ..selection import shot_analyzer
    from . import vision_analyzer
    from ..runtime.runtime_policy import media_slot
    sources = shot_analyzer.manifest_sources(manifest, base_dir)
    root = cache_root(report_dir)
    product = product_key(manifest, sources)
    report_dir.mkdir(parents=True, exist_ok=True)
    with file_lock(root / "products" / product, timeout=float(os.environ.get("JY_VISION_CACHE_WAIT_TIMEOUT", "7200"))):
        shots, issues, inventory, warnings = [], [], [], []
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
                reusable = (isinstance(prepared, dict) and prepared.get("identity") == identity
                            and prepared.get("analysis", {}).get("ok")
                            and _present(p for row in prepared["analysis"].get("shots", [])
                                         for p in row.get("frame_paths", [])))
                if reusable and ffprobe:
                    refreshed = _refresh_duration(prepared, source, signature, ffprobe=ffprobe, ffmpeg=ffmpeg)
                    if refreshed is None:
                        reusable = False
                    elif refreshed is not prepared:
                        prepared = refreshed
                        _save(path, prepared)
                if reusable:
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
                    if ffprobe and any(row.get("material_duration_confidence") != "HIGH"
                                       for row in result.get("shots", [])):
                        raise ValueError("MATERIAL_DURATION_BOUND_UNCONFIRMED")
                    if result.get("ok"):
                        _save(path, {"identity": identity, "analysis": result,
                                     **({"duration_contract": DURATION_CONTRACT} if ffprobe else {})})
                for number, original in enumerate(result.get("shots", []), 1):
                    row = copy.deepcopy(original)
                    row["shot_id"] = f"source_{index:03d}_shot_{number:03d}"
                    shots.append(row)
                issues.extend(result.get("issues", []))
                warnings.extend(result.get("warnings", []))
            inventory.append(signature)
        result = {"version": shot_analyzer._skill_version(), "scope": "shared_product",
                  "sources": [str(p) for p in sources], "shots": shots, "issues": issues,
                  "pending_items": list(issues), "warnings": list(warnings), "library_write": [],
                  "analysis_prompt": shot_analyzer.build_analysis_prompt(manifest, shots),
                  "ok": bool(shots) and not issues}
        if vision and shots:
            _restore_product_observations(manifest, result, _read(report_dir / "shot_candidates.json"))
            result = vision_analyzer.enrich_with_vision(manifest, result, report_dir, ffmpeg=ffmpeg)
        excluded = vision_analyzer.logged_pool_exclusions(result)
        complete = (bool(shots) and not issues and len(excluded) < len(result["shots"])
                    and len(excluded) <= vision_analyzer._reask_max_shots(len(result["shots"])) and all(
            str(row.get("shot_id")) in excluded or (
                vision_analyzer._reusable_visual_observation(row)
                and not vision_analyzer._needs_text_backfill(row)) for row in result["shots"]))
        result["material_cache"] = {"product_key": product, "source_inventory": inventory,
                                    "sources_hit": hits, "sources_prepared": len(inventory) - hits,
                                    "sources_skipped_not_video": sum(
                                        w.get("type") == "source_skipped_not_video" for w in warnings),
                                    "shots_excluded_unauditable": sorted(excluded),
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
