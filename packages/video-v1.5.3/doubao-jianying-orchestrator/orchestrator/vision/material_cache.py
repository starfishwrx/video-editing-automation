"""Reusable media preparation for a product, before any model requests.

Entries use normal source stat metadata and the preparation parameters. They
are accelerators, not quality certificates; visual and text gates still run.
"""
from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import copy
import json
import os
from functools import partial
from itertools import islice
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
        from . import evidence_store
        signature = contract.source_identity(source)
        source_sha = evidence_store.identity(source)
        result = _refresh_duration({"analysis": {"shots": rows}}, source, signature,
                                   ffprobe=ffprobe, ffmpeg=ffmpeg)
        if result is None:
            raise ValueError(f"MATERIAL_GEOMETRY_REQUIRES_REANALYSIS:{source}")
        for row, updated in zip(rows, result["analysis"]["shots"]):
            row["prepared_source_sha256"] = source_sha
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
                if isinstance(w, dict) and w.get("type") in vision.EXCLUSION_WARNINGS}
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


def _prepare_source(source: Path, *, manifest: dict, base_dir: Path, root: Path,
                    ffmpeg, ffprobe, threshold: float, min_shot_s: float):
    from ..selection import shot_analyzer
    from ..runtime.runtime_policy import media_slot

    try:
        signature = contract.source_identity(source)
    except OSError:
        return None, {"issues": [{"type": "source_missing", "video": str(source)}]}, False
    identity = {"source": signature, "threshold": threshold, "min_shot_s": min_shot_s,
                "protocol": "scene-representatives-us-v1"}
    key = contract.digest(identity)
    source_dir = root / "sources" / key
    path = source_dir / "prepared.json"
    from . import evidence_store
    content = evidence_store.identity(source)
    shared = evidence_store.database()
    prep_policy = contract.digest({k:v for k,v in identity.items() if k != "source"})
    lock = root / "locks" / contract.digest([content, prep_policy]) if shared else path
    with file_lock(lock, timeout=7200):
        prepared = _read(path)
        if prepared and prepared.get("source_sha256") != content:
            prepared = None
        if shared and not prepared:
            old_path = evidence_store.preparation(content, prep_policy)
            prior = _read(old_path) if old_path else None
            if (prior and prior.get("source_sha256") == content and prior.get("analysis", {}).get("ok")
                    and {k:v for k,v in prior["identity"].items() if k != "source"}
                        == {k:v for k,v in identity.items() if k != "source"}):
                prepared = copy.deepcopy(prior)
                prepared["identity"] = identity
                prepared["analysis"]["sources"] = [str(source)]
                for row in prepared["analysis"].get("shots", []):
                    row["video"] = str(source)
                _save(path, prepared)
        reusable = (isinstance(prepared, dict) and prepared.get("identity") == identity
                    and prepared.get("analysis", {}).get("ok")
                    and _present(p for row in prepared["analysis"].get("shots", [])
                                 for p in row.get("frame_paths", [])))
        if reusable and (not ffprobe or prepared.get("duration_contract") == DURATION_CONTRACT):
            if shared:
                evidence_store.preparation(content, prep_policy, path)
            return signature, {**prepared["analysis"], "prepared_source_sha256": content}, True
        with media_slot(timeout=600, cold_source=True) as handle:
            if not handle.acquired:
                raise TimeoutError("MEDIA_PREPARE_SLOT_TIMEOUT")
            if reusable:
                refreshed = _refresh_duration(prepared, source, signature, ffprobe=ffprobe, ffmpeg=ffmpeg)
                if refreshed is not None:
                    _save(path, refreshed)
                    return signature, {**refreshed["analysis"], "prepared_source_sha256": content}, True
            result = shot_analyzer.analyze_manifest(
                {**manifest, "material_sources": [str(source)], "shot_analysis": None},
                base_dir, source_dir, ffmpeg=ffmpeg, ffprobe=ffprobe,
                threshold=threshold, min_shot_s=min_shot_s)
        if signature != contract.source_identity(source) or evidence_store.identity(source) != content:
            raise ValueError("MATERIAL_SOURCE_CHANGED_DURING_PREPARATION")
        if ffprobe and any(row.get("material_duration_confidence") != "HIGH"
                           for row in result.get("shots", [])):
            raise ValueError("MATERIAL_DURATION_BOUND_UNCONFIRMED")
        if result.get("ok"):
            _save(path, {"identity": identity, "analysis": result, "source_sha256": content,
                         **({"duration_contract": DURATION_CONTRACT} if ffprobe else {})})
            if shared:
                evidence_store.preparation(content, prep_policy, path)
    return signature, {**result, "prepared_source_sha256": content}, False


def _prepare_sources(sources, prepare, workers):
    """Observe failures as jobs finish, but return rows in manifest order."""
    if workers <= 1:
        return [prepare(source) for source in sources]
    jobs = iter(enumerate(sources))
    results = [None] * len(sources)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = {pool.submit(prepare, source): i for i, source in islice(jobs, workers)}
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                results[pending.pop(future)] = future.result()
            for i, source in islice(jobs, len(done)):
                pending[pool.submit(prepare, source)] = i
    return results


def analyze_product(manifest: dict, base_dir: Path, report_dir: Path, *, ffmpeg=None,
                    ffprobe=None, threshold=.30, min_shot_s=.45, vision=True) -> dict:
    """Serialize one product's cold dependency, while reusing each unchanged source.

    The lock covers media preparation and observations. A waiter never starts
    cutting the same product while its producer is already calling the API.
    """
    from ..selection import shot_analyzer
    from . import vision_analyzer
    from ..runtime.runtime_policy import load_policy
    sources = shot_analyzer.manifest_sources(manifest, base_dir)
    root = cache_root(report_dir)
    product = product_key(manifest, sources)
    report_dir.mkdir(parents=True, exist_ok=True)
    with file_lock(root / "products" / product, timeout=float(os.environ.get("JY_VISION_CACHE_WAIT_TIMEOUT", "7200"))):
        shots, issues, inventory, warnings = [], [], [], []
        hits = 0
        workers = max(1, min(len(sources), load_policy().source_prep_slots))
        prepare = partial(_prepare_source, manifest=manifest, base_dir=base_dir, root=root,
                          ffmpeg=ffmpeg, ffprobe=ffprobe, threshold=threshold, min_shot_s=min_shot_s)
        for index, (signature, result, reused) in enumerate(_prepare_sources(sources, prepare, workers), 1):
            if signature is not None:
                inventory.append(signature)
            hits += int(reused)
            for number, original in enumerate(result.get("shots", []), 1):
                row = copy.deepcopy(original)
                row["shot_id"] = f"source_{index:03d}_shot_{number:03d}"
                if result.get("prepared_source_sha256"):
                    row["prepared_source_sha256"] = result["prepared_source_sha256"]
                shots.append(row)
            issues.extend(result.get("issues", []))
            warnings.extend(result.get("warnings", []))
        result = {"version": shot_analyzer._skill_version(), "scope": "shared_product",
                  "sources": [str(p) for p in sources], "shots": shots, "issues": issues,
                  "pending_items": list(issues), "warnings": list(warnings), "library_write": [],
                  "analysis_prompt": shot_analyzer.build_analysis_prompt(manifest, shots),
                  "ok": bool(shots) and not issues}
        if vision and shots:
            _restore_product_observations(manifest, result, _read(report_dir / "shot_candidates.json"))
            result = vision_analyzer.enrich_with_vision(manifest, result, report_dir, ffmpeg=ffmpeg)
        excluded = vision_analyzer.logged_pool_exclusions(result)
        complete = (not issues and not result.get("pending_items")
                    and vision_analyzer.usable_pool(result["shots"], excluded) and all(
            str(row.get("shot_id")) in excluded or (
                vision_analyzer._reusable_visual_observation(row)
                and not vision_analyzer._needs_text_backfill(row)) for row in result["shots"]))
        result["material_cache"] = {"product_key": product, "source_inventory": inventory,
                                    "sources_hit": hits, "sources_prepared": len(inventory) - hits,
                                    "sources_skipped_not_video": sum(
                                        w.get("type") == "source_skipped_not_video" for w in warnings),
                                    "shots_excluded_unauditable": sorted(w["shot_id"] for w in result["warnings"]
                                        if w.get("type") == vision_analyzer.UNAUDITABLE_WARNING and w["shot_id"] in excluded),
                                    "shots_excluded_invalid_format": sorted(w["shot_id"] for w in result["warnings"]
                                        if w.get("type") == vision_analyzer.FORMAT_WARNING and w["shot_id"] in excluded),
                                    "ready": complete if vision else bool(shots) and not issues}
        _save(report_dir / "shot_candidates.json", result)
        if vision:
            from . import evidence_store
            evidence_store.index_analysis(manifest, result, report_dir / "shot_candidates.json")
        atomic_write_text(report_dir / "shot_analysis_prompt.md", result["analysis_prompt"])
        return result


def cached_frames(shot: dict, report_dir: Path, prepare: Callable[[Path], list[dict]], *,
                  protocol: str) -> list[dict]:
    from . import evidence_store
    identity = {"source": contract.source_identity(shot["video"]),
                "window": [shot["source_start"], shot["source_end"]], "protocol": protocol}
    directory = cache_root(report_dir) / "frames" / contract.digest(identity)
    path = directory / "frames.json"
    with file_lock(path, timeout=600):
        sha = evidence_store.identity(shot["video"])
        shot["sampled_source_sha256"] = sha
        record = _read(path)
        if isinstance(record, dict) and record.get("source_sha256") == sha:
            frames = record["frames"]
            if _present([f.get("path", "") for f in frames]):
                return frames
        # Leave all older pixels and their model receipts available. Validate unsigned legacy
        # frames against fresh pixels before retaining their exact paths and call budgets.
        target = directory / sha if record is not None else directory
        target.mkdir(parents=True, exist_ok=True)
        frames = prepare(target)
        if identity["source"] != contract.source_identity(shot["video"]) or sha != evidence_store.identity(shot["video"]):
            raise ValueError("MATERIAL_SOURCE_CHANGED_DURING_PREPARATION")
        if frames and _present([f["path"] for f in frames]):
            if isinstance(record, list) and len(record) == len(frames) and _present([f["path"] for f in record]):
                if all(old.get("timestamp") == new.get("timestamp") and
                       evidence_store.identity(old["path"]) == evidence_store.identity(new["path"])
                       for old, new in zip(record, frames)):
                    frames = record
            _save(path, {"source_sha256": sha, "frames": frames})
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
