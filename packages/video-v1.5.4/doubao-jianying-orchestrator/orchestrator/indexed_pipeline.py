"""Content-first indexing and bounded, on-demand whole-script production.

Coarse vectors are retrieval hints, never evidence of a product claim. Original
observations survive selection-policy changes; new checks have their own facts.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import indexed_planner, shot_analyzer
from .material_store import MaterialStore, product_identity, OBSERVATION_PROTOCOL
from .local_retrieval import LocalRetriever

US = 1_000_000


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def locations(manifest, report_dir):
    index = Path(manifest.get("material_index_dir") or os.environ.get("JY_MATERIAL_INDEX_DIR")
                 or Path(report_dir).parent / "material_index").expanduser().resolve()
    models = Path(manifest.get("vision_model_cache_dir") or os.environ.get("JY_VISION_MODEL_DIR")
                  or index.parent / "vision_models").expanduser().resolve()
    return index, models


def index_materials(manifest, base_dir, report_dir, *, env=None, retriever_factory=LocalRetriever):
    """Hash/import before probing or decoding. This entry never calls remote AI."""
    started = time.perf_counter()
    base, report = Path(base_dir), Path(report_dir)
    report.mkdir(parents=True, exist_ok=True)
    index, models = locations(manifest, report)
    store, product = MaterialStore(index), product_identity(manifest)
    rows = store.ingest_sources(shot_analyzer.manifest_sources(manifest, base), product)
    if not rows:
        raise ValueError("material inventory is empty")
    stats = {"sources": len(rows), "hash_cache_hits": sum(r["hash_reused"] for r in rows),
             "remote_requests": 0, "decoded_frames": 0, "duration_probes": 0}
    analysis_file = manifest.get("shot_analysis_file")
    if analysis_file:
        path = Path(analysis_file)
        path = path if path.is_absolute() else base / path
        raw = path.read_bytes()
        identity = hashlib.sha256(raw + json.dumps([product, sorted(r["material_id"] for r in rows)]).encode()).hexdigest()
        receipt = index / "imports" / (identity + ".json")
        if receipt.is_file():
            stats["legacy_import"] = {**json.loads(receipt.read_text(encoding="utf-8")), "cache_hit": True}
        else:
            analysis = json.loads(raw.decode("utf-8-sig"))
            stats["legacy_import"] = store.import_analysis(analysis, manifest)
            _write(receipt, stats["legacy_import"])
    for row in rows:
        mid = row["material_id"]
        if store.get_frames(mid):
            continue
        with store.lease(mid, "representative-frames"):
            if store.get_frames(mid):
                continue
            env = env or {}
            duration = shot_analyzer.probe_duration(Path(row["path"]), ffmpeg=env.get("ffmpeg"), ffprobe=env.get("ffprobe"))
            stats["duration_probes"] += 1
            if duration <= 0:
                raise ValueError(f"unreadable material duration: {row['path']}")
            _write(index / "durations" / (mid + ".json"), {"duration_us": int(duration * US)})
            for stamp in sorted(set([min(.05, duration / 10), duration / 2, max(0, duration - .15)])):
                output = report / "frames" / f"{mid}_{int(stamp * US)}.jpg"
                if not shot_analyzer.extract_frame(Path(row["path"]), stamp, output, ffmpeg=env.get("ffmpeg")):
                    raise ValueError(f"cannot decode representative frame: {row['path']}")
                store.save_frame(mid, int(stamp * US), output)
                stats["decoded_frames"] += 1
    ids = sorted(set(r["material_id"] for r in rows))
    retriever = retriever_factory(store, models, product_id=product,
                                  revision=manifest.get("siglip_revision", "main"), batch_size=8)
    stats["local_index"] = retriever.index_frames(store.frames(ids))
    stats.update(index_dir=str(index), model_cache_dir=str(models), elapsed_s=time.perf_counter() - started,
                 pending_materials=sum(not store.get_observations(mid, product_id=product) for mid in ids))
    _write(report / "material_index_receipt.json", stats)
    return store, retriever, rows, stats


OBSERVE_PROMPT = """只观察给定原素材按时间排列的帧。素材标签与检索分数都不是事实。
描述商品、实际动作、可见结果及相反/缺失的证据。具体要求只有确实看见才列入verified_requirements；不确定保留缺证。
完整动作给出真实source微秒起止action_segments，不能把定格包装当功能展示。只输出JSON:
{\"description\":\"实际观察\",\"action_segments\":[{\"start_us\":0,\"end_us\":1000000,\"action\":\"...\"}],\"verified_requirements\":[],\"uncertain_requirements\":[],\"usable\":true}。"""


def _candidates(store, mid, product, path, required, group_id):
    frames = store.get_frames(mid)
    result = []
    for rec in store.get_observations(mid, product_id=product, requirements=required):
        raw = rec["observation"]
        evidence = [f for f in frames if rec["start_us"] <= f["timestamp_us"] <= rec["end_us"]]
        if (rec["state"] not in {"observed", "verified"} or not evidence
                or not rec["requirements_satisfied"] or raw.get("usable") is False):
            continue
        result.append({**raw, "candidate_id": rec["observation_id"], "material_id": mid,
                       "video": path, "video_sha256": mid,
                       "source_start_us": rec["start_us"], "source_end_us": rec["end_us"],
                       "description": raw.get("description") or raw.get("visual_description"),
                       "verification_status": "verified", "provenance": rec["provenance"],
                       "verified_requirements": rec["verified_requirements"],
                       "evidence": evidence, "group_ids": [group_id]})
    return result


def prepare(manifest, base_dir, report_dir, *, env=None, model=None, retriever_factory=LocalRetriever):
    """Normal build coordinator. Failures never fall through to legacy matching."""
    from .indexed_remote import RemoteModel
    report = Path(report_dir) / "indexed"
    report.mkdir(parents=True, exist_ok=True)
    manifest = copy.deepcopy(manifest)
    model = model or RemoteModel(profile=manifest.get("vision_profile"), report_dir=report)
    stats = {"material_understanding_calls": 0, "candidate_expansions": 0, "failed_materials": []}
    try:
        store, retriever, sources, index_stats = index_materials(manifest, base_dir, report, env=env,
                                                               retriever_factory=retriever_factory)
        stats["index"] = index_stats
        product = product_identity(manifest)
        paths = {r["material_id"]: r["path"] for r in sources}
        manifest["indexed_available_observations"] = [
            {"material_id": mid, "description": str(rec["observation"].get("description") or "")[:160],
             "source_span_us": rec["end_us"] - rec["start_us"]}
            for mid in paths for rec in store.get_observations(mid, product_id=product)
            if rec["state"] in {"observed", "verified"}]
        groups = indexed_planner.group_script(manifest, report, model, base_dir=Path(base_dir))
        manifest["indexed_groups"] = groups
        pools = {}

        def verify(hit, group):
            mid, required = hit["material_id"], group.get("required_requirements", [])
            existing = _candidates(store, mid, product, paths[mid], required, group["id"])
            if existing:
                return existing
            with store.lease(mid, "visual-observation", timeout_s=120, lease_s=300):
                existing = _candidates(store, mid, product, paths[mid], required, group["id"])
                if existing:
                    return existing
                observations = store.get_observations(mid, product_id=product)
                if any(r["provenance"].get("kind") == "candidate-vision"
                       and set(required) <= set(r["provenance"].get("evaluated_requirements", []))
                       and r["protocol"] == OBSERVATION_PROTOCOL for r in observations):
                    return []
                duration_file = store.index_dir / "durations" / (mid + ".json")
                if duration_file.is_file():
                    end = json.loads(duration_file.read_text(encoding="utf-8"))["duration_us"]
                else:
                    known = [r["observation"].get("material_duration_us", 0) for r in observations
                             if str(r["observation"].get("material_duration_confidence", "")).upper() == "HIGH"]
                    end = max(known, default=0) or int(shot_analyzer.probe_duration(Path(paths[mid]), **{k: v for k,v in (env or {}).items() if k in {"ffmpeg", "ffprobe"}}) * US)
                    _write(duration_file, {"duration_us": end})
                if end <= 0:
                    raise ValueError("material duration unavailable")
                frames = store.get_frames(mid)
                if group.get("require_complete_action") or required:
                    # A few extra ordered frames for this candidate only; never all-library decoding.
                    for n in range(9):
                        stamp = min(end - 1, int((end - 150000) * n / 8))
                        if any(abs(f["timestamp_us"] - stamp) < 100000 for f in frames):
                            continue
                        output = report / "action_frames" / f"{mid}_{stamp}.jpg"
                        if shot_analyzer.extract_frame(Path(paths[mid]), stamp / US, output, ffmpeg=(env or {}).get("ffmpeg")):
                            frames.append(store.save_frame(mid, stamp, output, protocol="action-sequence-v1"))
                frames.sort(key=lambda r: r["timestamp_us"])
                request = {"stage": "material_observation", "prompt": OBSERVE_PROMPT,
                           "payload": {"product": product, "duration_us": end, "required_requirements": required},
                           "images": frames, "output": str(report / "material" / f"{mid}_{hashlib.sha256(json.dumps(required).encode()).hexdigest()[:16]}.json")}
                answer = model(request)
                # Persist even negative observations so a hot run does not repeat a failed question.
                verified = set(answer.get("verified_requirements", [])) & set(required)
                store.save_observation(mid, answer, product_id=product, protocol=OBSERVATION_PROTOCOL,
                                       start_us=0, end_us=end, state="observed",
                                       verified_requirements=verified,
                                       provenance={"kind": "candidate-vision", "evidence": [f["frame_id"] for f in frames],
                                                   "evaluated_requirements": list(required),
                                                   "request_file": request["output"]})
                stats["material_understanding_calls"] += 1
                return _candidates(store, mid, product, paths[mid], required, group["id"])

        retrieved = {}
        def pool_for(group, topk):
            pool = list(pools.get(group["id"], []))
            seen = retrieved.setdefault(group["id"], set())
            hits = retriever.retrieve(group.get("retrieval_query") or group["query"], paths.keys(), topk=topk)
            fresh = [h for h in hits if h["material_id"] not in seen]
            seen.update(h["material_id"] for h in fresh)
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [(h, executor.submit(verify, h, group)) for h in fresh]
                for hit, future in futures:
                    try:
                        pool.extend(future.result())
                    except Exception as exc:
                        stats["failed_materials"].append({"material_id": hit["material_id"], "group_id": group["id"], "error": str(exc)})
            pools[group["id"]] = pool
            return pool
        for group in groups:
            pool_for(group, 8)
        def expand(group, current_pool):
            stats["candidate_expansions"] += 1
            return pool_for(group, 16)
        merged = {}
        for pool in pools.values():
            for row in pool:
                if row["candidate_id"] in merged:
                    merged[row["candidate_id"]]["group_ids"] += row["group_ids"]
                else:
                    merged[row["candidate_id"]] = row
        _write(report / "retrieved_candidates.json", {"groups": groups, "candidates": list(merged.values()), "stats": stats})
        result = indexed_planner.prepare(manifest, list(merged.values()), report, base_dir=Path(base_dir), model=model,
                                         max_repairs=2, candidate_expander=expand)
    except Exception as exc:
        issue = exc.as_dict() if callable(getattr(exc, "as_dict", None)) else {"type": type(exc).__name__, "message": str(exc)}
        result = {"ok": False, "status": "INDEXED_CONTEXTUAL_BLOCKED", "issues": [issue]}
    stats["remote"] = getattr(model, "stats", {})
    result["index_stats"] = stats
    result["formal_delivery_allowed"] = False
    result["human_review"] = "PENDING"
    _write(report / "indexed_result.json", {k: v for k, v in result.items() if k != "contextual_plan"})
    return result
