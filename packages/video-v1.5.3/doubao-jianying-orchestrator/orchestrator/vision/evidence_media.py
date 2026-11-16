"""Import a product's verified historical media preparation into the selected cache."""
import json
import os
import shutil
from pathlib import Path

from .. import ai_editor_contract as contract
from ..runtime.file_safety import file_lock, locked_json_update
from . import material_cache
from . import vision_analyzer as vision


def seed(manifest, analysis, destination, frozen):
    from . import evidence_store
    frozen = dict(frozen)
    signature = frozen.pop("contract_hash", "")
    if signature != "sha256:" + contract.digest(frozen):
        raise ValueError("FROZEN_MATERIAL_CONTRACT_INVALID")
    hashes = {str(Path(x["path"]).resolve()):x["sha256"] for x in frozen["inputs"]}
    history = Path(analysis["vision_analysis"]["cache_path"])
    target = Path(destination)
    source_root = history.parent / "media" / "sources"
    eligible = {s["path"]:s for s in analysis["material_cache"]["source_inventory"]}
    allowed = {str(Path(p).resolve()) for p in manifest.get("material_sources", [])}
    rows = {}
    for shot in analysis["shots"]:
        signature = contract.source_identity(shot["video"])
        key = vision._vision_cache_key(manifest, shot)
        if (hashes.get(signature["path"]) != evidence_store.identity(signature["path"])
                or signature["path"] not in allowed or eligible.get(signature["path"]) != signature
                or not key or key != shot.get("analysis_cache_key")):
            raise ValueError("HISTORICAL_OBSERVATION_IDENTITY_MISMATCH")
        facts = vision._cache_projection(shot)
        if facts is not None:
            facts["source_sha256"] = hashes[signature["path"]]
            shot["source_sha256"] = facts["source_sha256"]
            rows[key] = facts
    checks = vision._rule_checks(manifest)
    def merge(disk):
        if not isinstance(disk,dict):
            raise ValueError("INVALID_SHARED_VISION_CACHE")
        for key, facts in rows.items():
            prev = disk.get(key)
            if (not prev or (not prev.get("source_sha256") and set(prev.get("rule_checks") or []) < set(facts.get("rule_checks") or []))
                    or prev == {k:v for k,v in facts.items() if k != "source_sha256"}
                    or (vision._resolve_recheck(prev,checks)[1] == "redo"
                             and vision._resolve_recheck(facts,checks)[1] == "keep")):
                disk[key] = facts
        return disk
    count = 0
    policy = {"threshold":.30, "min_shot_s":.45, "protocol":"scene-representatives-us-v1"}
    db = evidence_store.database()
    persistent = db is not None and target.resolve() == db.parent.resolve()
    def register(sha, path):
        if persistent:
            evidence_store.preparation(sha, contract.digest(policy), path, preserve_existing=True)
    for sig in eligible.values():
        # Legacy scene preparation uses this default protocol. Nondefault settings
        # keep their own key and can be produced normally when needed.
        source = sig["path"]
        if (source not in allowed or contract.source_identity(source) != sig
                or hashes.get(source) != evidence_store.identity(source)):
            raise ValueError("HISTORICAL_PREPARATION_IDENTITY_MISMATCH")
        key = contract.digest({"source":sig, **policy})
        base = source_root/key
        path = target/"media"/"sources"/key/"prepared.json"
        with file_lock(path,timeout=600):
            current = material_cache._read(path)
            if current and current.get("identity",{}).get("source")==sig and material_cache._present(
                    f for s in current["analysis"]["shots"] for f in s["frame_paths"]):
                if current.get("source_sha256") == hashes[source]:
                    register(hashes[source], path)
                    continue
                # A weak legacy entry cannot establish a content binding. Recheck
                # its actual representative pixels against the verified report.
                prior = material_cache._read(base/"prepared.json")
                if prior and prior["identity"] == current["identity"]:
                    def frame_hashes(entry):
                        return [(s["source_start"],s["source_end"],[contract.file_sha256(Path(f))
                                 for f in s["frame_paths"]]) for s in entry["analysis"]["shots"]]
                    if frame_hashes(prior) == frame_hashes(current):
                        current["source_sha256"] = hashes[sig["path"]]
                        material_cache._save(path,current)
                        register(hashes[source], path)
                        continue
                raise ValueError("CONFLICTING_LEGACY_MEDIA_PREPARATION")
            data = material_cache._read(base/"prepared.json")
            if (not data or contract.digest(data.get("identity"))!=key or
                    not data["analysis"].get("ok") or data.get("duration_contract")!=material_cache.DURATION_CONTRACT):
                continue
            for shot in data["analysis"]["shots"]:
                images = {}
                for filename in shot["frame_paths"]:
                    old = Path(filename).resolve(strict=True)
                    new = path.parent / old.relative_to(base.resolve())
                    new.parent.mkdir(parents=True,exist_ok=True)
                    if not new.exists():
                        try:
                            os.link(old,new)
                        except OSError:
                            shutil.copy2(old,new)
                    if contract.file_sha256(new) != contract.file_sha256(old):
                        raise ValueError("MATERIAL_REPRESENTATIVE_FRAME_CHANGED")
                    images[filename] = str(new)
                shot["frame_path"] = images[shot["frame_path"]]
                shot["frame_paths"] = list(images.values())
            data["source_sha256"] = hashes[sig["path"]]
            material_cache._save(path,data)
            register(hashes[source], path)
            count+=1
    locked_json_update(target/"material_vision.json",merge,default={},timeout=120)
    return {"sources_imported":count,"validated_visual_keys":len(rows)}
